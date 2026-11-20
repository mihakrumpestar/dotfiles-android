"""Command line entry point for the recipe runner.

Every flag arrives here and lands in a `Store` or one `commands` call. Known
failures print one line to stderr and exit 1, so a script can rely on the exit
code alone.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from . import commands
from .artifacts import ArtifactError
from .device import DeviceError
from .recipe import RecipeError
from .runner import StepError
from .split import SplitError
from .store import DEFAULT_APPS, DEFAULT_STATE, Store, StoreError
from .tree import TreeError
from .vault import VaultError

_ERRORS = (
    ArtifactError,
    TreeError,
    DeviceError,
    RecipeError,
    SplitError,
    StepError,
    StoreError,
    VaultError,
    commands.CommandError,
    OSError,
)


def _tracing() -> bool:
    return os.environ.get("UIU_TRACE", "") not in ("", "0", "false")


class _Chain(argparse.Action):
    """Collect --tap and --step into one list, in the order written.

    argparse would otherwise keep the two options in separate lists and lose
    which came first, and a probe chain is nothing but its order.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        chain = list(getattr(namespace, "chain", None) or [])
        chain.append(((option_string or "").lstrip("-"), values))
        namespace.chain = chain


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--apps",
        default=DEFAULT_APPS,
        metavar="DIR",
        help="public policy tree, one folder per app with app.py; private app files are read too (default: policy/data)",
    )
    common.add_argument(
        "--secrets",
        default=None,
        metavar="DIR",
        help="root of the private export tree and private app files (default: $ANDROID_SECRETS_DIR, else secrets)",
    )
    common.add_argument(
        "--state",
        default=DEFAULT_STATE,
        metavar="DIR",
        help="directory for undo copies (default: .state)",
    )
    common.add_argument(
        "--serial",
        default=None,
        metavar="SERIAL",
        help="device serial; the only attached device when omitted",
    )

    parser = argparse.ArgumentParser(
        prog="uiu",
        description="Replay declarative Android UI recipes through uiautomator2.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    listing = subcommands.add_parser(
        "list", parents=[common], help="list app files and whether each one has a stored export"
    )
    listing.set_defaults(handler="list")

    probing = subcommands.add_parser(
        "probe",
        parents=[common],
        help="print the screen as ready-to-paste selectors, tapping as it goes",
    )
    probing.add_argument(
        "--launch", default="", metavar="COMPONENT", help="activity component to start first"
    )
    probing.add_argument(
        "--clear",
        action="store_true",
        help="stop the app before starting it, as a launch step with clear does",
    )
    probing.add_argument(
        "--tap",
        dest="chain",
        action=_Chain,
        metavar="JSON",
        help='selector to tap before reading the screen, e.g. \'{"desc": "Settings"}\'; repeat to chain',
    )
    probing.add_argument(
        "--step",
        dest="chain",
        action=_Chain,
        metavar="JSON",
        help='a step as JSON, e.g. \'{"tap": {"selector": {"text": "Settings"}}}\'',
    )
    probing.add_argument(
        "--all", action="store_true", help="also print structural nodes that carry only an id"
    )
    probing.set_defaults(handler="probe", chain=None)

    backup = subcommands.add_parser(
        "backup",
        parents=[common],
        help="run a recipe's backup steps and report the artifact's hash",
    )
    backup.add_argument(
        "--app", required=True, metavar="PACKAGE", help="package id of the recipe to run"
    )
    backup.add_argument(
        "--trace",
        action="store_true",
        default=_tracing(),
        help="print each step's timing to stderr",
    )
    backup.set_defaults(handler="backup")

    setting = subcommands.add_parser(
        "setup",
        parents=[common],
        help="run a recipe's setup steps, which flip in-app state that has no export",
    )
    setting.add_argument(
        "--app", required=True, metavar="PACKAGE", help="package id of the recipe to run"
    )
    setting.add_argument(
        "--trace",
        action="store_true",
        default=_tracing(),
        help="print each step's timing to stderr",
    )
    setting.set_defaults(handler="setup")

    verifying = subcommands.add_parser(
        "verify",
        parents=[common],
        help="export to a temporary file and compare it with the stored artifact",
    )
    verifying.add_argument(
        "--app", required=True, metavar="PACKAGE", help="package id of the recipe to run"
    )
    verifying.add_argument(
        "--trace",
        action="store_true",
        default=_tracing(),
        help="print each step's timing to stderr",
    )
    verifying.set_defaults(handler="verify")

    restoring = subcommands.add_parser(
        "restore",
        parents=[common],
        help="restore a stored artifact, guarded against overwriting unexported state",
    )
    restoring.add_argument(
        "--app", required=True, metavar="PACKAGE", help="package id of the recipe to run"
    )
    restoring.add_argument(
        "--force",
        action="store_true",
        help="restore even when the device state differs from the stored artifact",
    )
    restoring.add_argument(
        "--trace",
        action="store_true",
        default=_tracing(),
        help="print each step's timing to stderr",
    )
    restoring.set_defaults(handler="restore")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(apps=args.apps, secrets=args.secrets, state=args.state)
    try:
        if args.handler == "list":
            return commands.list_recipes(store)
        if args.handler == "probe":
            return commands.probe(
                args.serial,
                launch=args.launch,
                clear=args.clear,
                chain=args.chain or [],
                show_all=args.all,
            )
        if args.handler == "backup":
            return commands.backup(store, args.app, args.serial, trace=args.trace)
        if args.handler == "setup":
            return commands.setup(store, args.app, args.serial, trace=args.trace)
        if args.handler == "verify":
            return commands.verify(store, args.app, args.serial, trace=args.trace)
        return commands.restore(store, args.app, args.serial, force=args.force, trace=args.trace)
    except BrokenPipeError:
        # A closed pipe is how `| head` ends, not a failure of the command.
        return 0
    except KeyboardInterrupt:
        print("uiu: interrupted", file=sys.stderr)
        return 130
    except _ERRORS as error:
        print(f"uiu: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
