"""androidctl: apply declarative Android device state over adb.

The CLI is one subcommand per job. Every command accepts `--serial`; without it
the only attached device is used. A command error prints one `androidctl:` line
on stderr and exits 1, a parse error exits 2, and an interrupted run exits 130.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from . import apps, commands, debloat, profile
from .adb import Adb, AdbError
from .ratings import RatingsError
from .snapshots import SnapshotError

_ERRORS = (
    commands.CommandError,
    AdbError,
    SnapshotError,
    RatingsError,
    apps.AppError,
    profile.ProfileError,
)


class _Parser(argparse.ArgumentParser):
    """Argparse with the CLI's one-line error style instead of a usage block."""

    def error(self, message: str) -> NoReturn:
        print(f"androidctl: {message}", file=sys.stderr)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="androidctl",
        description="Apply declarative Android device state over adb.",
        allow_abbrev=False,
    )
    parser.add_argument("--serial", help="adb serial; defaults to the only attached device.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    def subparser(name: str, help_text: str) -> argparse.ArgumentParser:
        # Every subcommand takes --serial too, so it reads naturally after the
        # command. SUPPRESS keeps the top-level value when the flag is absent.
        child = subcommands.add_parser(name, help=help_text, allow_abbrev=False)
        child.add_argument("--serial", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return child

    reporting = subparser("report", "Every package with the facts that decide a keep or kill call.")
    reporting.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON.")
    reporting.add_argument(
        "--enabled", dest="enabled_only", action="store_true", help="Only enabled packages."
    )
    reporting.add_argument("--state", default=".state", help="Directory for the cached ratings.")

    disabling = subparser("disable", "Disable packages from a file, or stdin when omitted.")
    disabling.add_argument("list", nargs="?", help="File of package ids, one per line.")
    disabling.add_argument(
        "--dry-run", action="store_true", help="Print the command, change nothing."
    )
    disabling.add_argument("--no-snapshot", action="store_true", help="Skip the pre-run snapshot.")
    disabling.add_argument("--state", default=".state", help="Directory for snapshots.")

    restoring = subparser("restore", "Re-enable what the newest snapshot recorded as enabled.")
    restoring.add_argument(
        "--from", dest="from_path", help="Snapshot path; defaults to the newest."
    )
    restoring.add_argument("--state", default=".state", help="Directory holding snapshots.")
    restoring.add_argument(
        "--dry-run", action="store_true", help="Print the commands, change nothing."
    )

    subparser("health", "Check boot, launcher, keyboard, roles and crash buffer.")

    installing = subparser("install", "Install an app from its release, checksum verified.")
    installing.add_argument(
        "app",
        metavar="PACKAGE",
        help="Package id of the app to install, e.g. dev.imranr.obtainium.",
    )
    installing.add_argument("--asset", default="", help="Override the release asset name.")
    installing.add_argument(
        "--dry-run", action="store_true", help="Resolve and verify, install nothing."
    )
    installing.add_argument("--state", default=".state", help="Directory for downloads.")

    bootstrapping = subparser("init", "Install the bootstrap apps and start their services.")
    bootstrapping.add_argument(
        "--dry-run", action="store_true", help="Resolve and verify, install nothing."
    )
    bootstrapping.add_argument("--state", default=".state", help="Directory for downloads.")

    applying = subparser("apply", "Apply profile.conf and apps.conf. Dry run unless --yes.")
    applying.add_argument("--files", action="append", help="Profiles to apply, in order.")
    applying.add_argument("--state", default=".state", help="Directory for the pre-apply snapshot.")
    applying.add_argument("--yes", action="store_true", help="Actually write.")

    verifying = subparser("verify", "Report drift between the device and both profiles.")
    verifying.add_argument("--files", action="append", help="Profiles to check, in order.")

    reverting = subparser("revert", "Restore the values captured before the last apply.")
    reverting.add_argument(
        "--from", dest="from_path", help="Snapshot path; defaults to the newest."
    )
    reverting.add_argument("--state", default=".state", help="Directory holding snapshots.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    status = _run(args)
    # When stdout is a pipe, the write that fails can be this flush, after the
    # command already returned. Catch it here so `| head` ends quietly.
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        status = 0
    return status


def _run(args: argparse.Namespace) -> int:
    try:
        # Local input is validated before the device is opened, so a typo is
        # reported even when no phone is attached.
        if args.command in ("apply", "verify"):
            chosen = _files(args.files)
            commands.load_profiles(
                chosen if chosen is not None else ["policy/profile.conf", "policy/apps.conf"]
            )
        packages = _packages(args.list) if args.command == "disable" else []
        spec = apps.load(args.app) if args.command == "install" else None
        specs = (
            [apps.load(package) for package in _packages(commands.INIT_FILE)]
            if args.command == "init"
            else []
        )
        client = Adb(getattr(args, "serial", None))
        if args.command == "report":
            return commands.report(
                client,
                args.state,
                as_json=args.as_json,
                enabled_only=args.enabled_only,
            )
        if args.command == "health":
            return commands.health(client)
        if args.command == "disable":
            return commands.disable(
                client,
                packages,
                args.state,
                dry_run=args.dry_run,
                no_snapshot=args.no_snapshot,
            )
        if args.command == "restore":
            return commands.restore_packages(
                client, args.state, from_path=args.from_path, dry_run=args.dry_run
            )
        if args.command == "init":
            return commands.init_device(client, specs, args.state, dry_run=args.dry_run)
        if spec is not None:
            return commands.install_app(
                client, spec, args.state, asset=args.asset, dry_run=args.dry_run
            )
        if args.command == "apply":
            return commands.apply(client, _files(args.files), args.state, yes=args.yes)
        if args.command == "verify":
            return commands.verify(client, _files(args.files))
        return commands.revert(client, args.state, from_path=args.from_path)
    except BrokenPipeError:
        # Python flushes stdout again at exit. Point it at devnull, or the
        # closed pipe prints an "Exception ignored" line after a clean exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        print("androidctl: interrupted", file=sys.stderr)
        return 130
    except _ERRORS as error:
        print(f"androidctl: {error}", file=sys.stderr)
        return 1


def _files(values: list[str] | None) -> list[str] | None:
    """Profile paths, splitting the comma-separated form the old CLI accepted."""
    if not values:
        return None
    return [part for value in values for part in value.split(",") if part]


def _packages(path: str | None) -> list[str]:
    """Read package ids from a file or stdin, skipping blanks and comments."""
    try:
        text = sys.stdin.read() if path is None else Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise commands.CommandError(f"reading {path}: not UTF-8: {error}") from error
    except OSError as error:
        raise commands.CommandError(f"reading {path}: {error}") from error
    packages = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for package in packages:
        if not debloat.valid_package(package):
            raise commands.CommandError(f"not a package name: {json.dumps(package)}")
    return packages


if __name__ == "__main__":
    raise SystemExit(main())
