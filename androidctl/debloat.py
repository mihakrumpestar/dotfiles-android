"""Facts from a connected phone, and the one reversible write this tool allows.

A package is one installed app. User 0 is the owner profile, the account of the
person holding the phone, as opposed to a work profile. A role is a system job
such as the home screen or the SMS app.

The only write is `pm disable-user --user 0 <pkg>` and its inverse
`pm enable --user 0 <pkg>`. Removing an app is deliberately absent: it can
delete an APK from /data/app that `cmd package install-existing` cannot restore.
"""

from __future__ import annotations

import json
import re

from androidctl.adb import AdbError, Device

# Package states as `pm list packages -e/-d` reports them. Absent means the
# device does not list the package at all.
ENABLED = "enabled"
DISABLED = "disabled"
ABSENT = "absent"

# Partitions hold APKs in these subdirectories. Anything else directly under a
# partition is a package path in its own right, e.g. /apex/<name>@<version>.
APP_SLOTS = frozenset({"app", "priv-app", "overlay"})

# Deliberately narrow: the name is pasted into `adb shell`, so a wider grammar
# would be a shell-injection hole.
_PACKAGE = re.compile(r"[A-Za-z0-9_.]+")

_HEADER = re.compile(r"^  Package \[([A-Za-z0-9_.]+)\] \([0-9a-f]+\):", re.MULTILINE)
_SHARED_USER = re.compile(r"sharedUser=SharedUserSetting\{[0-9a-f]+ ([^/]+)/\d+\}")
_TARGET_SDK = re.compile(r"targetSdk=(\d+)")

_LAUNCHER_QUERY = (
    "cmd package query-activities --components "
    "-a android.intent.action.MAIN -c android.intent.category.HOME"
)


def valid_package(name: str) -> bool:
    """Whether name is a plain Android package id."""
    return _PACKAGE.fullmatch(name) is not None


def tier(path: str) -> str:
    """The partition that shipped an APK, e.g. "system/priv-app"."""
    if path in ("", "null"):
        return "unknown"
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "unknown"
    if len(parts) > 1 and parts[1] in APP_SLOTS:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def parse_tiers(out: str) -> dict[str, str]:
    """Read `package:<path>=<name>` lines into name -> partition.

    The split is at the last `=` because a /data/app path contains `==`.
    """
    found: dict[str, str] = {}
    for line in out.split("\n"):
        body = line.strip()
        if not body.startswith("package:"):
            continue
        path, separator, name = body[len("package:") :].rpartition("=")
        if not separator or not valid_package(name):
            continue
        found[name] = tier(path)
    return found


def states(device: Device) -> dict[str, str]:
    """Package name -> enabled or disabled, as the device lists them."""
    found: dict[str, str] = {}
    for flag, state in (("-e", ENABLED), ("-d", DISABLED)):
        out = device.read(f"pm list packages {flag}")
        for line in out.split("\n"):
            if line.startswith("package:"):
                found[line[len("package:") :].strip()] = state
    return found


def installed(device: Device) -> set[str]:
    """Packages present for user 0.

    `pm list packages` without --user covers every user, so an app installed
    only in a work profile would pass a skip check and then fail against user 0.
    """
    out = device.read("pm list packages --user 0")
    found: set[str] = set()
    for line in out.split("\n"):
        body = line.strip()
        if body.startswith("package:"):
            found.add(body[len("package:") :])
    return found


def state_of(states: dict[str, str], package: str) -> str:
    """One package's state, with a missing key reading as absent.

    A preview that claimed it would disable a package the device does not have
    would be a lie.
    """
    state = states.get(package, "")
    return state if state in (ENABLED, DISABLED) else ABSENT


def tiers(device: Device) -> dict[str, str]:
    """Package name -> partition, taken from the installed APK path."""
    return parse_tiers(device.read("pm list packages -f"))


def package_facts(device: Device) -> tuple[dict[str, str], dict[str, int]]:
    """Shared uid and target SDK per package, from one `dumpsys package` dump.

    The dump is about 118k lines, so reading it twice would cost a second for
    the same data. A shared uid groups apps that run as one Linux user. A target
    SDK far behind the OS suggests an abandoned app, which is a hint rather than
    a guarantee.
    """
    out = device.read("dumpsys package")
    matches = list(_HEADER.finditer(out))
    uids: dict[str, str] = {}
    sdks: dict[str, int] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(out)
        body = out[match.end() : end]
        if shared := _SHARED_USER.search(body):
            uids[match.group(1)] = shared.group(1)
        if target := _TARGET_SDK.search(body):
            sdks[match.group(1)] = int(target.group(1))
    return uids, sdks


def role_holders(device: Device) -> dict[str, str]:
    """Role name -> holding package, empty when nobody holds the role.

    A role this firmware does not know and an empty role look the same, so only
    a transport failure is an error.
    """
    holders: dict[str, str] = {}
    for role in ("HOME", "DIALER", "SMS", "BROWSER", "ASSISTANT", "EMERGENCY", "WALLET"):
        result = device.shell(f"cmd role get-role-holders android.app.role.{role}")
        if result.code == -1:
            raise AdbError(f"reading {role} role: {result.err.strip()}")
        holder = ""
        if result.ok:
            lines = result.out.strip().split("\n")
            if lines[0]:
                holder = lines[0].strip()
        holders[role] = holder
    return holders


def launchers(device: Device) -> list[str]:
    """Every package that offers a HOME activity, so it can act as home screen."""
    result = device.shell(_LAUNCHER_QUERY)
    if result.code == -1:
        raise AdbError(f"querying launchers: {result.err.strip()}")
    found: list[str] = []
    for line in result.out.split("\n"):
        if "/" in line and not line.startswith(" "):
            found.append(line.split("/", 1)[0])
    return found


def keyboard(device: Device) -> str:
    """The default keyboard package, or "" when none is set."""
    result = device.shell("settings get secure default_input_method")
    if result.code == -1:
        raise AdbError(f"reading default keyboard: {result.err.strip()}")
    out = result.out.strip()
    if out in ("", "null"):
        return ""
    return out.split("/", 1)[0]


def crashes(device: Device) -> int:
    """FATAL EXCEPTION entries in the crash buffer, which is historical."""
    result = device.shell("logcat -d -b crash")
    if result.code == -1:
        raise AdbError(f"reading crash buffer: {result.err.strip()}")
    return result.out.count("FATAL EXCEPTION")


def boot_completed(device: Device) -> bool:
    """Whether the device finished booting."""
    result = device.shell("getprop sys.boot_completed")
    if result.code == -1:
        raise AdbError(f"reading sys.boot_completed: {result.err.strip()}")
    return result.out.strip() == "1"


def mutate(device: Device, verb: str, package: str) -> tuple[bool, str]:
    """Run `pm <verb> --user 0 <pkg>` and return the device's own message.

    Success means the command was accepted, not that the state changed. A
    refusal is data, not an exception: its message is the useful part.
    """
    if not valid_package(package):
        raise ValueError(f"not a package name: {json.dumps(package)}")
    result = device.shell(f"pm {verb} --user 0 {package}")
    if result.code == -1:
        raise AdbError(f"pm {verb} --user 0 {package}: {result.err.strip()}")
    return result.code == 0, explain(result.out + result.err)


def explain(raw: str) -> str:
    """Pick the sentence a person needs out of a `pm` refusal.

    A refusal opens with "Exception occurred while executing", then a stack
    trace whose last line is always a Binder frame. Neither says why. The reason
    is the line carrying the exception class or the platform's Failure message.
    """
    prefix = ""
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("at "):
            continue
        if line.startswith("Exception occurred while executing"):
            prefix = line
        elif "Failure [" in line or "Exception" in line:
            return line
        elif not prefix:
            prefix = line
    return prefix


def disable(device: Device, package: str) -> tuple[bool, str]:
    """Turn a package off for user 0 without removing it."""
    return mutate(device, "disable-user", package)


def enable(device: Device, package: str) -> tuple[bool, str]:
    """Reverse disable."""
    return mutate(device, "enable", package)
