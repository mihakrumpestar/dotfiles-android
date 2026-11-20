"""Declarative device state: Android's key/value stores plus app-scoped state.

A profile is an ordered list of entries. Each entry's section names the store
that owns it, so a value cannot land in a store that cannot see it.

The Settings provider, written with `settings put`, holds the `system`, `secure`
and `global` namespaces. DeviceConfig, written with `cmd device_config put`, is a
separate per-feature store such as `telephony`; neither store can see the
other's keys. App-scoped state, such as permissions, app ops (per-app switches),
roles (system jobs such as the SMS app), doze whitelisting, standby buckets and
app links, changes only through its own command.

Snapshot file I/O lives in snapshots.py. This module only talks to the device.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from androidctl.adb import AdbError, Device


class ProfileError(ValueError):
    """A profile line, key or value a store refuses."""


class State(StrEnum):
    """Where a value came from.

    Explicit means somebody set it. Implicit means the platform derives it, so a
    write would not stick. Unset means the store has nothing to say for the key.
    """

    UNSET = "unset"
    IMPLICIT = "implicit"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class Entry:
    """One namespace/key/value triple plus the reason it is wanted."""

    namespace: str
    key: str
    value: str = ""
    reason: str = ""


Profile = list[Entry]


@dataclass(frozen=True)
class SnapshotEntry:
    """One key's state before a run, so the run can be undone."""

    state: State
    value: str = ""


class Store(Protocol):
    """One declarative store, with the three operations apply, verify and revert.

    A store with no unset state returns False from `reset`.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]: ...

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]: ...

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]: ...


# Namespaces reach the device's shell, so a wider grammar would be syntax there.
_NAME_RE = re.compile(r"[A-Za-z0-9_.]+")
# Keys may carry the colon, hyphen and wildcard the app-scoped stores split on.
# Each store validates the parts it splits out before they reach a command.
_KEY_RE = re.compile(r"[A-Za-z0-9_.:*-]+")

_OP_RE = re.compile(r"[A-Z0-9_]+")
_PERM_RE = re.compile(r"[A-Za-z0-9_.]+")
_ROLE_RE = re.compile(r"android\.app\.role\.[A-Z_]+")
_DOMAIN_RE = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
_LOCALE_RE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]+)*")
_MODE_RE = re.compile(r"[a-z]+")
_PACKAGE_RE = re.compile(r"[A-Za-z0-9_.]+")

_LINK_HANDLING_RE = re.compile(r"Verification link handling allowed: (true|false)")

# Values that need shell quoting. The device's sh parses the joined command.
_SHELL_SPECIALS = frozenset(" \t\n\"'$`\\|&;<>()*?[]{}~!#")


def _q(value: str) -> str:
    """Render a string for an error message, with JSON's escaping rules."""
    return json.dumps(value, ensure_ascii=False)


class KV:
    """A device key/value store reached by one command prefix."""

    def __init__(self, prefix: tuple[str, ...], namespaces: tuple[str, ...] = ()):
        self.prefix = prefix
        # The namespaces this store accepts. For Settings, every other namespace
        # belongs to DeviceConfig.
        self.namespaces = namespaces

    def command(self, *parts: str) -> str:
        """One store invocation with every argument quoted."""
        return render(self.prefix, *parts)

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        return _kv_value(device.read(self.command("get", entry.namespace, entry.key)))

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        return _mutate(device, self.command("put", entry.namespace, entry.key, entry.value))

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        return _mutate(device, self.command("delete", entry.namespace, entry.key))


# The Settings provider and the separate device configuration store.
SETTINGS = KV(("settings",), ("system", "secure", "global"))
DEVICE_CONFIG = KV(("cmd", "device_config"))


def _kv_value(out: str) -> tuple[str, State]:
    """One key/value read: `null` means never written, anything else is explicit."""
    value = out.strip()
    # Both stores print `null` for a key that was never written.
    if value == "null":
        return "", State.UNSET
    return value, State.EXPLICIT


def render(prefix: tuple[str, ...], *parts: str) -> str:
    """Join a command prefix and its arguments, quoting every argument."""
    return " ".join([*prefix, *(quote(part) for part in parts)])


def quote(value: str) -> str:
    """Make a value survive `adb shell`, which joins arguments into one string.

    An empty value still needs quotes, or `settings put secure key` arrives one
    argument short and cannot clear a setting.
    """
    if value and not any(character in _SHELL_SPECIALS for character in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _mutate(device: Device, command: str) -> tuple[bool, str]:
    """Run one mutation and return the device's own message, if any."""
    result = device.shell(command)
    message = result.out.strip()
    if message == "":
        message = result.err.strip()
    return result.ok, message


def key(entry: Entry) -> str:
    """Name an entry in output and in snapshots."""
    return f"{entry.namespace}/{entry.key}"


def _is_settings(namespace: str) -> bool:
    return namespace in SETTINGS.namespaces


@dataclass(frozen=True)
class _PackageKey:
    package: str
    name: str


def _split_package_key(key_value: str) -> _PackageKey:
    """Split `<package>:<name>` and validate the package every command names."""
    package, separator, name = key_value.partition(":")
    if not separator or package == "" or name == "":
        raise ProfileError(f"key {_q(key_value)} must be <package>:<name>")
    if not _PACKAGE_RE.fullmatch(package):
        raise ProfileError(f"not a package name: {_q(package)}")
    return _PackageKey(package, name)


def _split_domain_key(key_value: str) -> _PackageKey:
    """Split `<package>:<domain>` and validate both parts."""
    parts = _split_package_key(key_value)
    if not _DOMAIN_RE.fullmatch(parts.name):
        raise ProfileError(f"invalid domain {_q(parts.name)}")
    return parts


def _package_key(key_value: str) -> str:
    if not _PACKAGE_RE.fullmatch(key_value):
        raise ProfileError(f"not a package name: {_q(key_value)}")
    return key_value


def _role_holder(value: str) -> str:
    """A role's value: one package, or empty for no holder.

    A role holds a single package. Adding a second evicts the first, so a list
    would silently keep only its last name.
    """
    value = value.strip()
    if value == "":
        return ""
    if "," in value:
        raise ProfileError(
            f"role value {_q(value)} names more than one package; "
            "a role holds one holder and the platform evicts the previous one"
        )
    if not _PACKAGE_RE.fullmatch(value):
        raise ProfileError(f"not a package name: {_q(value)}")
    return value


# One domain state, spelled as a token or as the code the device prints.
_DOMAIN_CODES = {
    "0": "0",
    "none": "0",
    "1": "1",
    "verified": "1",
    "2": "2",
    "approved": "2",
    "3": "3",
    "denied": "3",
}

# (number printed, bucket name, state) from `am get-standby-bucket`.
_STANDBY_BUCKETS: tuple[tuple[str, str, State], ...] = (
    ("5", "exempted", State.IMPLICIT),
    ("10", "active", State.EXPLICIT),
    ("20", "working_set", State.EXPLICIT),
    ("30", "frequent", State.EXPLICIT),
    ("40", "rare", State.EXPLICIT),
    ("45", "restricted", State.EXPLICIT),
    ("50", "never", State.EXPLICIT),
)


class PermissionsStore:
    """Android runtime permissions, the ones an app asks for while running.

    A permission absent from the dump reads Unset, so a grant command that was a
    silent no-op cannot compare equal to `revoked`.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        parts = _split_package_key(entry.key)
        if not _PERM_RE.fullmatch(parts.name):
            raise ProfileError(f"invalid permission {_q(parts.name)}")
        out = device.read(render(("dumpsys", "package"), parts.package))
        return _parse_permission(out, parts.name)

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        parts = _split_package_key(entry.key)
        if not _PERM_RE.fullmatch(parts.name):
            raise ProfileError(f"invalid permission {_q(parts.name)}")
        if entry.value == "granted":
            return render(("pm", "grant"), parts.package, parts.name)
        if entry.value == "revoked":
            return render(("pm", "revoke"), parts.package, parts.name)
        raise ProfileError(f"permission value must be granted or revoked, got {_q(entry.value)}")

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.reset_command(entry.key)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def reset_command(self, key_value: str) -> str:
        parts = _split_package_key(key_value)
        if not _PERM_RE.fullmatch(parts.name):
            raise ProfileError(f"invalid permission {_q(parts.name)}")
        return render(("pm", "revoke"), parts.package, parts.name)


def _parse_permission(out: str, permission: str) -> tuple[str, State]:
    """Read a permission's state from a `User 0:` block that holds runtime ones.

    The dump has one block per user. Some apps print an earlier `User 0:` block
    for domain verification, so the search continues until a block with a
    `runtime permissions:` section is found. A grant in a work profile says
    nothing about user 0. Install permissions sit outside the block and cannot be
    changed with `pm grant`, so they must not match.
    """
    header = -1
    runtime = False
    for raw in out.split("\n"):
        trimmed = raw.strip()
        if trimmed == "":
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        if header < 0:
            if trimmed.startswith("User 0:"):
                header = indent
                runtime = False
            continue
        if indent <= header:
            header = -1
            runtime = False
            if trimmed.startswith("User 0:"):
                header = indent
            continue
        if trimmed == "runtime permissions:":
            runtime = True
            continue
        if not runtime:
            continue
        name, separator, rest = trimmed.partition(":")
        if not separator or name != permission:
            continue
        value = rest.strip().partition(",")[0]
        if value == "granted=true":
            return "granted", State.EXPLICIT
        if value == "granted=false":
            return "revoked", State.EXPLICIT
    return "", State.UNSET


class AppOpsStore:
    """App ops, per-app permission switches separate from install permissions.

    A stored row is Explicit. An op with no row prints only its default mode,
    which the platform derives, so it is Implicit.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        parts = _split_package_key(entry.key)
        if not _OP_RE.fullmatch(parts.name):
            raise ProfileError(f"invalid app op {_q(parts.name)}")
        out = device.read(render(("cmd", "appops", "get"), parts.package, parts.name))
        return _parse_app_op(out, parts.name)

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        parts = _split_package_key(entry.key)
        if not _OP_RE.fullmatch(parts.name):
            raise ProfileError(f"invalid app op {_q(parts.name)}")
        if not _MODE_RE.fullmatch(entry.value):
            raise ProfileError(f"invalid app ops mode {_q(entry.value)}")
        return render(("cmd", "appops", "set"), parts.package, parts.name, entry.value)

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.reset_command(entry.key)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def reset_command(self, key_value: str) -> str:
        parts = _split_package_key(key_value)
        if not _OP_RE.fullmatch(parts.name):
            raise ProfileError(f"invalid app op {_q(parts.name)}")
        return render(("cmd", "appops", "set"), parts.package, parts.name, "default")


def _parse_app_op(out: str, op: str) -> tuple[str, State]:
    fallback = ""
    for raw in out.split("\n"):
        line = raw.strip()
        line = line.removeprefix("Uid mode:")
        line = line.strip()
        name, separator, mode = line.partition(":")
        if not separator:
            continue
        name = name.strip()
        mode = mode.strip()
        if name == "Default mode":
            if fallback == "":
                fallback = mode
            continue
        if name != op:
            continue
        head, separator, _ = mode.partition(";")
        if separator:
            mode = head.strip()
        return mode, State.EXPLICIT
    if fallback != "":
        return fallback, State.IMPLICIT
    return "", State.UNSET


class RoleStore:
    """Role holders, where a role is a system job such as the SMS app.

    The key is the full role name; the value is one package, cleared with
    `clear-role-holders`.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        if not _ROLE_RE.fullmatch(entry.key):
            raise ProfileError(f"invalid role {_q(entry.key)}")
        out = device.read(render(("cmd", "role", "get-role-holders"), entry.key))
        return _parse_role(out), State.EXPLICIT

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        if not _ROLE_RE.fullmatch(entry.key):
            raise ProfileError(f"invalid role {_q(entry.key)}")
        holder = _role_holder(entry.value)
        if holder == "":
            return render(("cmd", "role", "clear-role-holders"), entry.key)
        return render(("cmd", "role", "add-role-holder"), entry.key, holder)

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.reset_command(entry.key)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def reset_command(self, key_value: str) -> str:
        if not _ROLE_RE.fullmatch(key_value):
            raise ProfileError(f"invalid role {_q(key_value)}")
        return render(("cmd", "role", "clear-role-holders"), key_value)


def _parse_role(out: str) -> str:
    """Join the holders `cmd role` prints on one line separated by `;`.

    An unheld role prints a single empty line, which is an explicit empty value.
    """
    holders = [holder.strip() for holder in out.split(";") if holder.strip()]
    return ",".join(holders)


class DeviceIdleStore:
    """The doze whitelist: apps allowed to run while the phone sleeps.

    A `user` row can be removed. A `system-excidle` row belongs to the platform
    and `cmd deviceidle whitelist -<pkg>` cannot remove it.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        package = _package_key(entry.key)
        out = device.read(render(("cmd", "deviceidle", "whitelist")))
        return _parse_device_idle(out, package)

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        _package_key(entry.key)
        if entry.value == "allow":
            sign = "+"
        elif entry.value == "ignore":
            sign = "-"
        else:
            raise ProfileError(f"deviceidle value must be allow or ignore, got {_q(entry.value)}")
        return render(("cmd", "deviceidle", "whitelist"), sign + entry.key)

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.reset_command(entry.key)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def reset_command(self, key_value: str) -> str:
        _package_key(key_value)
        return render(("cmd", "deviceidle", "whitelist"), "-" + key_value)


def _parse_device_idle(out: str, package: str) -> tuple[str, State]:
    excidle = False
    for line in out.split("\n"):
        fields = line.strip().split(",")
        if len(fields) < 2 or fields[1] != package:
            continue
        if fields[0] == "user":
            return "allow", State.EXPLICIT
        if fields[0] == "system-excidle":
            excidle = True
    if excidle:
        return "allow", State.IMPLICIT
    return "ignore", State.EXPLICIT


class StandbyStore:
    """App standby buckets, a ranking of how freely an app runs in background.

    Every app has a bucket, so there is nothing to unset; revert writes back the
    bucket that was read.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        package = _package_key(entry.key)
        out = device.read(render(("am", "get-standby-bucket"), package))
        return _parse_standby(out)

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        package = _package_key(entry.key)
        if entry.value == "exempted":
            raise ProfileError(
                "standby bucket exempted is the platform's floor for a "
                "doze-whitelisted app; use active, working_set, frequent, rare, "
                "restricted or never"
            )
        for _, name, _ in _STANDBY_BUCKETS:
            if name == entry.value:
                return render(("am", "set-standby-bucket"), package, name)
        raise ProfileError(f"invalid standby bucket {_q(entry.value)}")

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        return False, "a standby bucket cannot be unset"


def _parse_standby(out: str) -> tuple[str, State]:
    number = out.strip()
    for bucket_number, name, state in _STANDBY_BUCKETS:
        if bucket_number == number:
            return name, state
    raise ProfileError(f"unexpected standby bucket {_q(number)}")


class LocaleStore:
    """Per-app locales. An empty value clears the override.

    Clearing takes the app back to the system language.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        package = _package_key(entry.key)
        out = device.read(render(("cmd", "locale", "get-app-locales"), package))
        return _parse_locales(out), State.EXPLICIT

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        _package_key(entry.key)
        if entry.value != "":
            for locale in entry.value.split(","):
                if not _LOCALE_RE.fullmatch(locale):
                    raise ProfileError(f"invalid locale {_q(locale)}")
        return render(("cmd", "locale", "set-app-locales"), entry.key, "--locales", entry.value)

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.reset_command(entry.key)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def reset_command(self, key_value: str) -> str:
        _package_key(key_value)
        return render(("cmd", "locale", "set-app-locales"), key_value, "--locales", "")


def _parse_locales(out: str) -> str:
    """Refuse `Unknown package ...`, which the command prints and exits 0 for.

    Turning that line into an empty locale list would hide a missing package.
    """
    body = out.strip()
    if body.startswith("Unknown package "):
        raise ProfileError(body)
    start = body.find("[")
    end = body.rfind("]")
    if start < 0 or end <= start:
        raise ProfileError(f"unexpected locale output {_q(body)}")
    return body[start + 1 : end].strip()


class AppLinksStore:
    """Whether an app may open its verified links by default.

    Verified links are links for a domain the app proved it owns. An app that
    declares no web link has no line to compare, so the entry reads Unset.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        package = _package_key(entry.key)
        out = device.read(render(("pm", "get-app-links", "--user", "0"), package))
        return _parse_app_links(out)

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        _package_key(entry.key)
        if entry.value == "allowed":
            allowed = "true"
        elif entry.value == "disallowed":
            allowed = "false"
        else:
            raise ProfileError(
                f"applinks value must be allowed or disallowed, got {_q(entry.value)}"
            )
        return render(
            ("pm", "set-app-links-allowed", "--user", "0", "--package"),
            entry.key,
            allowed,
        )

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        return False, "link handling has no unset state"


def _parse_app_links(out: str) -> tuple[str, State]:
    match = _LINK_HANDLING_RE.search(out)
    if match is None:
        return "", State.UNSET
    if match.group(1) == "true":
        return "allowed", State.EXPLICIT
    return "disallowed", State.EXPLICIT


class AppLinksDomainStore:
    """One domain's verification state under an app.

    The `Domain verification state:` block seeds every declared domain, so a
    domain missing from it is one the app does not declare: a typo, not a
    `none` state.
    """

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        parts = _split_domain_key(entry.key)
        out = device.read(render(("pm", "get-app-links", "--user", "0"), parts.package))
        return _parse_domain_state(out, parts.name)

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        parts = _split_domain_key(entry.key)
        state = _DOMAIN_CODES.get(entry.value)
        if state is None:
            raise ProfileError(
                f"applinks_domain value {_q(entry.value)} is not writable; "
                "use none (0), verified (1), approved (2) or denied (3)"
            )
        return render(("pm", "set-app-links", "--package"), parts.package, state, parts.name)

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.reset_command(entry.key)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def reset_command(self, key_value: str) -> str:
        parts = _split_domain_key(key_value)
        return render(("pm", "set-app-links", "--package"), parts.package, "0", parts.name)


def _parse_domain_state(out: str, domain: str) -> tuple[str, State]:
    """Return the token the device prints for a domain.

    A token outside the writable table is one the platform decided, so it is
    Implicit because a write cannot set it.
    """
    header = -1
    for line in out.split("\n"):
        trimmed = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))
        if header < 0:
            if trimmed.startswith("Domain verification state:"):
                header = indent
            continue
        if trimmed == "":
            continue
        if indent <= header:
            break
        name, separator, token = trimmed.partition(":")
        if not separator:
            continue
        if name.strip() != domain:
            continue
        token = token.strip()
        if token in _DOMAIN_CODES:
            return token, State.EXPLICIT
        return token, State.IMPLICIT
    return "", State.UNSET


class AppLinksSelectionStore:
    """The user's per-domain choice between the app's links and the browser."""

    def read(self, device: Device, entry: Entry) -> tuple[str, State]:
        parts = _split_domain_key(entry.key)
        out = device.read(render(("pm", "get-app-links", "--user", "0"), parts.package))
        return _parse_selection_state(out, parts.name)

    def write(self, device: Device, entry: Entry) -> tuple[bool, str]:
        try:
            command = self.write_command(entry)
        except ProfileError as error:
            return False, str(error)
        return _mutate(device, command)

    def write_command(self, entry: Entry) -> str:
        parts = _split_domain_key(entry.key)
        if entry.value == "enabled":
            selected = "true"
        elif entry.value == "disabled":
            selected = "false"
        else:
            raise ProfileError(
                f"applinks_selection value must be enabled or disabled, got {_q(entry.value)}"
            )
        return render(
            ("pm", "set-app-links-user-selection", "--user", "0", "--package"),
            parts.package,
            selected,
            parts.name,
        )

    def reset(self, device: Device, entry: Entry) -> tuple[bool, str]:
        return (
            False,
            (
                "the platform shows every declared domain as disabled until it is "
                "chosen, so there is no unset state to restore"
            ),
        )


def _parse_selection_state(out: str, domain: str) -> tuple[str, State]:
    """Read the Selection state block, where Enabled and Disabled head the lists."""
    header = -1
    selection = ""
    for line in out.split("\n"):
        trimmed = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))
        if header < 0:
            if trimmed.startswith("Selection state:"):
                header = indent
            continue
        if trimmed == "":
            continue
        if indent <= header:
            break
        if trimmed == "Enabled:":
            selection = "enabled"
            continue
        if trimmed == "Disabled:":
            selection = "disabled"
            continue
        if trimmed.endswith(":"):
            break
        if selection and trimmed == domain:
            return selection, State.EXPLICIT
    return "", State.UNSET


# The app-scoped stores, selected by their own section name.
_APP_STORES: dict[str, Store] = {
    "permissions": PermissionsStore(),
    "appops": AppOpsStore(),
    "role": RoleStore(),
    "deviceidle": DeviceIdleStore(),
    "standby": StandbyStore(),
    "locale": LocaleStore(),
    "applinks": AppLinksStore(),
    "applinks_domain": AppLinksDomainStore(),
    "applinks_selection": AppLinksSelectionStore(),
}

# The DeviceConfig namespaces a profile may name. Listing only the ones the
# profiles use keeps a typo a parse error.
_DEVICE_CONFIG_NAMESPACES = frozenset({"telephony"})


def section_store(namespace: str) -> Store | None:
    """The store that owns a section, or None when the section is unknown.

    The namespace alone decides the store: routing a typo to DeviceConfig would
    write a row nothing reads.
    """
    if namespace in SETTINGS.namespaces:
        return SETTINGS
    store = _APP_STORES.get(namespace)
    if store is not None:
        return store
    if namespace in _DEVICE_CONFIG_NAMESPACES:
        return DEVICE_CONFIG
    return None


def store_for(entry: Entry) -> Store:
    """The store that owns an entry's section.

    `parse` rejects an unknown section, so every parsed entry has one.
    """
    store = section_store(entry.namespace)
    if store is None:
        raise ProfileError(f"unknown section [{entry.namespace}]")
    return store


def section_names() -> list[str]:
    """Every section a profile may use, sorted, for parse errors."""
    names = [
        *SETTINGS.namespaces,
        *_APP_STORES,
        *_DEVICE_CONFIG_NAMESPACES,
    ]
    return sorted(names)


def read_all(device: Device, entries: Profile) -> dict[str, SnapshotEntry]:
    """Capture every entry's state before a run, so the run can be undone.

    It stops at the first read error.
    """
    before: dict[str, SnapshotEntry] = {}
    for entry in entries:
        try:
            value, state = store_for(entry).read(device, entry)
        except AdbError as error:
            raise AdbError(f"reading {key(entry)}: {error}") from error
        except ProfileError as error:
            raise ProfileError(f"reading {key(entry)}: {error}") from error
        before[key(entry)] = SnapshotEntry(state=state, value=value)
    return before


# One marker separates two commands' output inside one shell invocation. It
# cannot occur in a package dump or a role list, so splitting on it is exact.
_BULK_MARK = "__androidctl_bulk_mark__"


def read_many(device: Device, entries: Profile) -> dict[str, tuple[str, State]]:
    """Read many entries with as few device calls as possible.

    The result is keyed by `key(entry)` and must be identical to what
    `store_for(entry).read(device, entry)` returns for each entry, including
    State (EXPLICIT/IMPLICIT/UNSET) and the empty string for an unset value.
    """
    groups: dict[str, Profile] = {}
    for entry in entries:
        groups.setdefault(_family(entry.namespace), []).append(entry)
    values: dict[str, tuple[str, State]] = {}
    for family, group in groups.items():
        shipped = _shipped_store(family)
        if shipped is None or not all(isinstance(store_for(e), shipped) for e in group):
            # A store the bulk readers do not know (or a stand-in a test
            # installed) is read one entry at a time, so the answer still comes
            # from the store that owns the entry.
            for entry in group:
                values[key(entry)] = store_for(entry).read(device, entry)
            continue
        match family:
            case "settings":
                values.update(_kv_many(device, SETTINGS, group))
            case "device_config":
                values.update(_kv_many(device, DEVICE_CONFIG, group))
            case "permissions":
                values.update(_permissions_many(device, group))
            case "appops":
                values.update(_appops_many(device, group))
            case "role":
                values.update(_roles_many(device, group))
            case "applinks":
                values.update(_applinks_many(device, group))
            case "deviceidle":
                values.update(_deviceidle_many(device, group))
            case _:
                # Standby and locale are one call per app with nothing to share.
                for entry in group:
                    values[key(entry)] = store_for(entry).read(device, entry)
    return {key(entry): values[key(entry)] for entry in entries}


def _shipped_store(family: str) -> type | tuple[type, ...] | None:
    """The store class a family's bulk reader speaks to, or None for no reader.

    The three app-link stores share one `pm get-app-links`, so all three count
    as the shipped holder of that family.
    """
    return {
        "settings": KV,
        "device_config": KV,
        "permissions": PermissionsStore,
        "appops": AppOpsStore,
        "role": RoleStore,
        "applinks": (AppLinksStore, AppLinksDomainStore, AppLinksSelectionStore),
        "deviceidle": DeviceIdleStore,
    }.get(family)


def _family(namespace: str) -> str:
    """The stores that can answer several namespaces from one read.

    The three app-link sections share a `pm get-app-links`; the settings
    namespaces share the `settings` prefix, never the device config one.
    """
    if namespace in SETTINGS.namespaces:
        return "settings"
    if namespace in _DEVICE_CONFIG_NAMESPACES:
        return "device_config"
    if namespace.startswith("applinks"):
        return "applinks"
    return namespace


def _kv_many(device: Device, store: KV, entries: Profile) -> dict[str, tuple[str, State]]:
    """List each namespace once; keys the list omits fall back to one `get`.

    A multi-line value would not survive `list` intact, so a key the list does
    not show is asked for on its own.
    """
    listed = {
        namespace: _parse_kv_list(device.read(store.command("list", namespace)))
        for namespace in dict.fromkeys(entry.namespace for entry in entries)
    }
    values: dict[str, tuple[str, State]] = {}
    for entry in entries:
        table = listed[entry.namespace]
        if entry.key in table:
            values[key(entry)] = _kv_value(table[entry.key])
        else:
            values[key(entry)] = store.read(device, entry)
    return values


def _parse_kv_list(out: str) -> dict[str, str]:
    """`key=value` lines, split on the first `=`, because a value may hold one.

    A value that spans lines arrives as continuation lines, which would leave
    the key truncated, so a key with a continuation is dropped and read on its
    own.
    """
    table: dict[str, str] = {}
    last = ""
    for line in out.split("\n"):
        if line == "":
            continue
        entry_key, separator, value = line.partition("=")
        if not separator:
            table.pop(last, None)
            last = ""
            continue
        table[entry_key] = value
        last = entry_key
    return table


def _permissions_many(device: Device, entries: Profile) -> dict[str, tuple[str, State]]:
    wanted: dict[str, list[tuple[Entry, str]]] = {}
    for entry in entries:
        parts = _split_package_key(entry.key)
        if not _PERM_RE.fullmatch(parts.name):
            raise ProfileError(f"invalid permission {_q(parts.name)}")
        wanted.setdefault(parts.package, []).append((entry, parts.name))
    dumps = _package_dumps(device, list(wanted))
    values: dict[str, tuple[str, State]] = {}
    for package, group in wanted.items():
        for entry, permission in group:
            values[key(entry)] = _parse_permission(dumps[package], permission)
    return values


def _package_dumps(device: Device, packages: list[str]) -> dict[str, str]:
    """One package dump each, collected in a single shell invocation.

    `dumpsys package a b` on this firmware prints only `a`'s dump and no
    delimiter, so the dumps are asked for one at a time and separated by a
    marker. `dumpsys package <pkg>` exits 0 for an unknown package, exactly as
    the per-entry read assumes.
    """
    if len(packages) == 1:
        return {packages[0]: device.read(render(("dumpsys", "package"), packages[0]))}
    command = "; ".join(
        f"{render(('dumpsys', 'package'), package)}; echo {_BULK_MARK}" for package in packages
    )
    parts = device.read(command).split(_BULK_MARK)
    if len(parts) != len(packages) + 1 or parts[-1].strip() != "":
        return {
            package: device.read(render(("dumpsys", "package"), package)) for package in packages
        }
    return dict(zip(packages, parts[:-1]))


def _appops_many(device: Device, entries: Profile) -> dict[str, tuple[str, State]]:
    """One `cmd appops get <pkg>` per package answers every op of that package.

    An op with no stored row is not in that list, and its default mode is only
    printed for the single-op form, so those fall back to the per-entry read.
    """
    wanted: dict[str, list[tuple[Entry, str]]] = {}
    for entry in entries:
        parts = _split_package_key(entry.key)
        if not _OP_RE.fullmatch(parts.name):
            raise ProfileError(f"invalid app op {_q(parts.name)}")
        wanted.setdefault(parts.package, []).append((entry, parts.name))
    values: dict[str, tuple[str, State]] = {}
    for package, group in wanted.items():
        out = device.read(render(("cmd", "appops", "get"), package))
        for entry, op in group:
            if _has_app_op(out, op):
                values[key(entry)] = _parse_app_op(out, op)
            else:
                values[key(entry)] = store_for(entry).read(device, entry)
    return values


def _has_app_op(out: str, op: str) -> bool:
    """Whether the package list holds a row for the op, stored or uid-wide."""
    for raw in out.split("\n"):
        line = raw.strip().removeprefix("Uid mode:").strip()
        name, separator, _ = line.partition(":")
        if separator and name.strip() == op:
            return True
    return False


# The three app-link sections each parse a different part of one dump.
_APPLINKS_PARSERS: dict[str, Callable[[str, str], tuple[str, State]]] = {
    "applinks_domain": _parse_domain_state,
    "applinks_selection": _parse_selection_state,
}


def _applinks_many(device: Device, entries: Profile) -> dict[str, tuple[str, State]]:
    """One `pm get-app-links` per package answers all three app-link sections."""
    wanted: dict[str, list[tuple[Entry, str]]] = {}
    for entry in entries:
        if entry.namespace == "applinks":
            wanted.setdefault(_package_key(entry.key), []).append((entry, ""))
        else:
            parts = _split_domain_key(entry.key)
            wanted.setdefault(parts.package, []).append((entry, parts.name))
    values: dict[str, tuple[str, State]] = {}
    for package, group in wanted.items():
        out = device.read(render(("pm", "get-app-links", "--user", "0"), package))
        for entry, name in group:
            if entry.namespace == "applinks":
                values[key(entry)] = _parse_app_links(out)
            else:
                values[key(entry)] = _APPLINKS_PARSERS[entry.namespace](out, name)
    return values


def _deviceidle_many(device: Device, entries: Profile) -> dict[str, tuple[str, State]]:
    """The whitelist is one read for every app asked about."""
    packages = [_package_key(entry.key) for entry in entries]
    out = device.read(render(("cmd", "deviceidle", "whitelist")))
    return {
        key(entry): _parse_device_idle(out, package)
        for entry, package in zip(entries, packages, strict=True)
    }


def _roles_many(device: Device, entries: Profile) -> dict[str, tuple[str, State]]:
    """Batch every role lookup into one shell invocation."""
    for entry in entries:
        if not _ROLE_RE.fullmatch(entry.key):
            raise ProfileError(f"invalid role {_q(entry.key)}")
    roles = list(dict.fromkeys(entry.key for entry in entries))
    holders = _role_holders(device, roles)
    return {key(entry): (holders[entry.key], State.EXPLICIT) for entry in entries}


def _role_holders(device: Device, roles: list[str]) -> dict[str, str]:
    if len(roles) == 1:
        return {roles[0]: _parse_role(device.read(_role_command(roles[0])))}
    command = "; ".join(f"{_role_command(role)}; echo {_BULK_MARK}" for role in roles)
    parts = device.read(command).split(_BULK_MARK)
    if len(parts) != len(roles) + 1 or parts[-1].strip() != "":
        return {role: _parse_role(device.read(_role_command(role))) for role in roles}
    return {role: _parse_role(part) for role, part in zip(roles, parts[:-1])}


def _role_command(role: str) -> str:
    return render(("cmd", "role", "get-role-holders"), role)


def neutral(entry: Entry) -> Entry | None:
    """The entry a store resets to when it has no delete.

    A store whose reset deletes the key has no neutral value, so this returns
    None and only Unset is accepted for it.
    """
    match entry.namespace:
        case "permissions":
            return replace(entry, value="revoked")
        case "appops":
            return replace(entry, value="default")
        case "role" | "locale":
            return replace(entry, value="")
        case "deviceidle":
            return replace(entry, value="ignore")
        case "applinks_domain":
            return replace(entry, value="none")
        case _:
            return None


def equivalent(entry: Entry, got: str) -> bool:
    """Whether a value the device reports satisfies the entry.

    A domain state accepts either spelling the platform prints.
    """
    if entry.namespace != "applinks_domain":
        return got == entry.value
    code = _DOMAIN_CODES.get(got)
    return code is not None and code == _DOMAIN_CODES.get(entry.value)


def satisfied(entry: Entry, value: str, state: State) -> bool:
    """Whether a value and state read from the device satisfy the entry.

    Unset never satisfies: the store has nothing to say, so a write that left it
    unset did not stick. A derived value satisfies when it already equals the
    wanted value, because the platform reasserts it. An app op asked to be
    `default` satisfies too, because the row is gone by design and the platform
    reports the derived mode instead of the word.
    """
    if state is State.UNSET:
        return False
    if entry.namespace == "appops" and entry.value == "default" and state is State.IMPLICIT:
        return True
    return equivalent(entry, value)


def reset_ok(entry: Entry, value: str, state: State) -> bool:
    """Whether a readback after `reset` is what the reset aims for.

    A key/value store deletes the row, so Unset is the only goal there. An
    app-scoped store resets to a value the platform reports explicitly: a revoked
    permission, a dropped app op, no role holder, `ignore` or `none`. Unset there
    means the parser found nothing where the device should have answered, which
    is not evidence of success. deviceidle also accepts a derived allow, because
    a system-excidle row cannot be removed.
    """
    if state is State.IMPLICIT and entry.namespace == "deviceidle":
        return True
    if state is State.UNSET:
        return _is_settings(entry.namespace)
    wanted = neutral(entry)
    if wanted is None:
        return False
    return satisfied(wanted, value, state)


def packages(entry: Entry) -> list[str]:
    """The packages an entry names.

    A run skips entries whose package the device does not install for user 0. A
    malformed key is a profile error, not a reason to exempt the entry.
    """
    if entry.namespace == "role":
        holder = _role_holder(entry.value)
        return [holder] if holder else []
    if entry.namespace in ("permissions", "appops"):
        return [_split_package_key(entry.key).package]
    if entry.namespace in ("applinks_domain", "applinks_selection"):
        return [_split_domain_key(entry.key).package]
    if entry.namespace in ("deviceidle", "standby", "locale", "applinks"):
        return [_package_key(entry.key)]
    return []


def parse(text: str) -> Profile:
    """Read a profile.

    Each `[section]` selects a store, and each key needs a quoted value and a
    trailing reason:

        [namespace]
        key = "value"   # reason

    The reason is required: a change nobody can justify later is
    indistinguishable from a mistake.
    """
    profile: Profile = []
    namespace = ""
    seen: set[str] = set()
    for index, raw in enumerate(text.split("\n")):
        line = index + 1
        raw = raw.strip()
        if raw == "" or raw.startswith("#"):
            continue
        if raw.startswith("[") and raw.endswith("]"):
            namespace = raw[1:-1]
            if not _NAME_RE.fullmatch(namespace):
                raise ProfileError(f"line {line}: invalid namespace {_q(namespace)}")
            if section_store(namespace) is None:
                known = ", ".join(section_names())
                raise ProfileError(
                    f"line {line}: unknown section [{namespace}]; known sections: {known}"
                )
            continue
        if namespace == "":
            raise ProfileError(f"line {line}: entry before any [namespace]")
        entry_key, separator, rest = raw.partition("=")
        entry_key = entry_key.strip()
        if not separator or not _KEY_RE.fullmatch(entry_key):
            raise ProfileError(f'line {line}: expected key = "value"')
        try:
            value, reason = _split_value_and_reason(rest)
        except ProfileError as error:
            raise ProfileError(f"line {line}: {error}") from error
        if reason == "":
            raise ProfileError(f"line {line}: {entry_key} has no trailing # reason")
        name = f"{namespace}/{entry_key}"
        if name in seen:
            raise ProfileError(f"line {line}: duplicate {name}")
        seen.add(name)
        profile.append(Entry(namespace, entry_key, value, reason))
    if not profile:
        raise ProfileError("profile is empty")
    return profile


def _split_value_and_reason(rest: str) -> tuple[str, str]:
    """Split a quoted value from its trailing reason.

    The first quote opens the value and the next quote closes it. Text after the
    closing quote must start with `#`. A leading `why:` or `why ` is stripped
    from the reason.
    """
    rest = rest.strip()
    if not rest.startswith('"'):
        raise ProfileError('value must be quoted, e.g. "90"')
    end = rest.find('"', 1)
    if end < 0:
        raise ProfileError("unterminated value")
    value = rest[1:end]
    tail = rest[end + 1 :].strip()
    if tail == "":
        return value, ""
    if not tail.startswith("#"):
        raise ProfileError("unexpected text after the value")
    note = tail[1:].strip()
    lowered = note.lower()
    for prefix in ("why:", "why "):
        if lowered.startswith(prefix):
            return value, note[len(prefix) :].strip()
    return value, note
