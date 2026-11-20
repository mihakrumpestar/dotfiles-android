"""Profile parsing, store routing, command builders and device parsers.

Ported from internal/profile/profile_test.go and internal/profile/apps_test.go.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from support import REPO

from androidctl.adb import AdbError, ShellResult
from androidctl.commands import load_policy
from androidctl.profile import (
    _BULK_MARK,
    DEVICE_CONFIG,
    KV,
    SETTINGS,
    AppLinksDomainStore,
    AppLinksSelectionStore,
    AppLinksStore,
    AppOpsStore,
    DeviceIdleStore,
    Entry,
    LocaleStore,
    PermissionsStore,
    ProfileError,
    RoleStore,
    SnapshotEntry,
    StandbyStore,
    State,
    Store,
    _parse_app_links,
    _parse_app_op,
    _parse_device_idle,
    _parse_domain_state,
    _parse_locales,
    _parse_permission,
    _parse_role,
    _parse_selection_state,
    _parse_standby,
    equivalent,
    key,
    packages,
    parse,
    quote,
    read_all,
    read_many,
    render,
    reset_ok,
    satisfied,
    section_names,
    section_store,
    store_for,
)


class FakeDevice:
    """A device that answers reads by command and records every shell call.

    A command joined by `; echo MARK` is answered the way a shell would: each
    part looked up on its own, its output followed by the marker line.
    """

    serial = "fake"

    def __init__(
        self,
        answers: dict[str, str] | None = None,
        *,
        code: int = 0,
        out: str = "",
        err: str = "",
        read_error: str | None = None,
    ) -> None:
        self.answers = answers or {}
        self.result = ShellResult(code, out, err)
        self.read_error = read_error
        self.reads: list[str] = []
        self.shells: list[str] = []

    def run(self, *args: str) -> str:
        raise AssertionError(f"unexpected adb call: {args}")

    def shell(self, command: str) -> ShellResult:
        self.shells.append(command)
        return self.result

    def read(self, command: str) -> str:
        self.reads.append(command)
        if self.read_error is not None:
            raise AdbError(self.read_error)
        if f"; echo {_BULK_MARK}" in command:
            return self._script(command)
        return self.answers.get(command, "")

    def _script(self, command: str) -> str:
        """Answer a `;`-joined script of reads and `echo MARK` lines."""
        out: list[str] = []
        statement: list[str] = []
        for part in command.split(";"):
            part = part.strip()
            if part == f"echo {_BULK_MARK}":
                if statement:
                    out.append(self.answers.get("; ".join(statement), ""))
                    statement = []
                out.append(_BULK_MARK + "\n")
                continue
            statement.append(part)
        if statement:
            out.append(self.answers.get("; ".join(statement), ""))
        return "".join(out)


def check_command(build: Callable[[], str], want: str | None, want_error: str | None) -> None:
    if want_error is not None:
        with pytest.raises(ProfileError) as caught:
            build()
        assert want_error in str(caught.value)
        return
    assert build() == want


@pytest.mark.parametrize(
    ("text", "want", "want_error"),
    [
        pytest.param(
            '# leading\n[system]\na = "1"   # first\n\n[secure]\nb = "2"  # second\n',
            [
                Entry("system", "a", "1", "first"),
                Entry("secure", "b", "2", "second"),
            ],
            None,
            id="sections and comments",
        ),
        pytest.param(
            '[system]\na = "1"   # why: because\n',
            [Entry("system", "a", "1", "because")],
            None,
            id="why prefix is stripped",
        ),
        pytest.param(
            '[secure]\na = ""   # nothing\n',
            [Entry("secure", "a", "", "nothing")],
            None,
            id="empty value allowed",
        ),
        pytest.param('[system]\na = "1"\n', None, "no trailing # reason", id="missing reason"),
        pytest.param("[system]\na = 1   # x\n", None, "must be quoted", id="unquoted value"),
        pytest.param('a = "1"   # x\n', None, "before any [namespace]", id="entry before section"),
        pytest.param('[]\na = "1"   # x\n', None, "invalid namespace", id="empty namespace"),
        pytest.param(
            '[system]\na = "1" tail   # x\n',
            None,
            "unexpected text",
            id="trailing garbage",
        ),
        pytest.param("# nothing\n", None, "profile is empty", id="empty profile"),
        pytest.param(
            '[system]\na = "1"   # first\na = "2"   # second\n',
            None,
            "duplicate system/a",
            id="duplicate key",
        ),
        pytest.param('[system]\n = "1"   # x\n', None, "expected key", id="empty key"),
        pytest.param(
            '[[system]]\na = "1"   # x\n', None, "invalid namespace", id="invalid namespace"
        ),
        pytest.param('[applink]\na = "1"   # x\n', None, "unknown section", id="unknown section"),
        pytest.param(
            '[system]\na; reboot = "1"   # x\n',
            None,
            "expected key",
            id="invalid key",
        ),
    ],
)
def test_parse(text: str, want: list[Entry] | None, want_error: str | None) -> None:
    if want_error is not None:
        with pytest.raises(ProfileError) as caught:
            parse(text)
        assert want_error in str(caught.value)
        return
    assert want is not None
    assert parse(text) == want


# The namespace alone must decide the store. Routing a typo to DeviceConfig
# would write a row nothing reads.
@pytest.mark.parametrize(
    ("namespace", "known"),
    [
        ("system", True),
        ("secure", True),
        ("global", True),
        ("telephony", True),
        ("applink", False),
        ("unknown", False),
        ("permissions", True),
        ("appops", True),
        ("role", True),
        ("deviceidle", True),
        ("standby", True),
        ("locale", True),
        ("applinks", True),
        ("applinks_domain", True),
        ("applinks_selection", True),
    ],
)
def test_section_store(namespace: str, known: bool) -> None:
    assert (section_store(namespace) is not None) is known


# Every app-scoped section must select its own store, not the DeviceConfig
# fallback that would accept a key it cannot reach.
@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        ("system", SETTINGS),
        ("secure", SETTINGS),
        ("global", SETTINGS),
        ("telephony", DEVICE_CONFIG),
        ("permissions", PermissionsStore()),
        ("appops", AppOpsStore()),
        ("role", RoleStore()),
        ("deviceidle", DeviceIdleStore()),
        ("standby", StandbyStore()),
        ("locale", LocaleStore()),
        ("applinks", AppLinksStore()),
        ("applinks_domain", AppLinksDomainStore()),
        ("applinks_selection", AppLinksSelectionStore()),
    ],
)
def test_store_identity(namespace: str, expected: Store) -> None:
    store = section_store(namespace)
    assert store is not None
    assert type(store) is type(expected)
    if isinstance(expected, KV):
        assert store is expected


def test_section_names_are_sorted() -> None:
    assert section_names() == [
        "applinks",
        "applinks_domain",
        "applinks_selection",
        "appops",
        "deviceidle",
        "global",
        "locale",
        "permissions",
        "role",
        "secure",
        "standby",
        "system",
        "telephony",
    ]


# The key grammar must carry the separators the app-scoped stores split on.
@pytest.mark.parametrize(
    ("text", "want", "want_error"),
    [
        pytest.param(
            '[permissions]\ncom.example.app:android.permission.CAMERA = "granted"   # camera\n',
            "com.example.app:android.permission.CAMERA",
            None,
            id="package and permission",
        ),
        pytest.param(
            '[appops]\ncom.example.app:RECORD_AUDIO = "ignore"   # no mic\n',
            "com.example.app:RECORD_AUDIO",
            None,
            id="package and app op",
        ),
        pytest.param(
            '[role]\nandroid.app.role.SMS = "com.example.sms"   # default sms\n',
            "android.app.role.SMS",
            None,
            id="role with dots and underscore",
        ),
        pytest.param(
            '[standby]\ncom.example-app = "rare"   # parked\n',
            "com.example-app",
            None,
            id="package with hyphen",
        ),
        pytest.param(
            '[applinks_domain]\ncom.example.app:foo-bar.example = "approved"   # links\n',
            "com.example.app:foo-bar.example",
            None,
            id="domain with hyphen",
        ),
        pytest.param(
            '[applinks_selection]\ncom.example.app:*.discord.com = "enabled"   # links\n',
            "com.example.app:*.discord.com",
            None,
            id="wildcard domain",
        ),
        pytest.param(
            '[foo:bar]\na = "1"   # x\n',
            None,
            "invalid namespace",
            id="colon in namespace is still invalid",
        ),
    ],
)
def test_parse_app_keys(text: str, want: str | None, want_error: str | None) -> None:
    if want_error is not None:
        with pytest.raises(ProfileError) as caught:
            parse(text)
        assert want_error in str(caught.value)
        return
    profile = parse(text)
    assert len(profile) == 1
    assert profile[0].key == want


# A readback is evidence only when the state says where the value came from.
@pytest.mark.parametrize(
    ("entry", "value", "state", "want"),
    [
        (Entry("standby", "", "rare"), "rare", State.EXPLICIT, True),
        (Entry("standby", "", "rare"), "never", State.EXPLICIT, False),
        (Entry("appops", "", "allow"), "", State.UNSET, False),
        (Entry("standby", "", "exempted"), "exempted", State.IMPLICIT, True),
        (Entry("standby", "", "never"), "exempted", State.IMPLICIT, False),
        (Entry("appops", "", "default"), "allow", State.IMPLICIT, True),
        (Entry("appops", "", "default"), "default", State.EXPLICIT, True),
        (Entry("applinks_domain", "", "1"), "verified", State.EXPLICIT, True),
        (Entry("applinks_domain", "", "verified"), "1", State.EXPLICIT, True),
        (Entry("applinks_domain", "", "approved"), "denied", State.EXPLICIT, False),
    ],
    ids=[
        "explicit match",
        "explicit mismatch",
        "unset never matches",
        "derived value that matches",
        "derived value that differs",
        "app op reset to default",
        "app op default held explicitly",
        "domain code and token",
        "domain token and code",
        "domain mismatch",
    ],
)
def test_satisfied(entry: Entry, value: str, state: State, want: bool) -> None:
    assert satisfied(entry, value, state) is want


@pytest.mark.parametrize(
    ("entry", "value", "state", "want"),
    [
        (Entry("secure", "a"), "", State.UNSET, True),
        (Entry("deviceidle", "com.example.app"), "ignore", State.EXPLICIT, True),
        (Entry("deviceidle", "com.example.app"), "allow", State.IMPLICIT, True),
        (Entry("role", "android.app.role.SMS"), "", State.EXPLICIT, True),
        (Entry("locale", "com.example.app"), "", State.EXPLICIT, True),
        (
            Entry("permissions", "com.example.app:android.permission.CAMERA"),
            "revoked",
            State.EXPLICIT,
            True,
        ),
        (
            Entry("permissions", "com.example.app:android.permission.CAMERA"),
            "granted",
            State.EXPLICIT,
            False,
        ),
        (
            Entry("applinks_domain", "com.example.app:foo.example"),
            "none",
            State.EXPLICIT,
            True,
        ),
        (
            Entry("applinks_domain", "com.example.app:foo.example"),
            "approved",
            State.EXPLICIT,
            False,
        ),
    ],
    ids=[
        "settings delete leaves the key unset",
        "deviceidle lets go of a user row",
        "deviceidle keeps a system-excidle row",
        "role clears to empty",
        "locale clears to empty",
        "permission revoke reads revoked",
        "permission still granted",
        "domain back to none",
        "domain still approved",
    ],
)
def test_reset_ok(entry: Entry, value: str, state: State, want: bool) -> None:
    assert reset_ok(entry, value, state) is want


def test_shipped_profile_parses() -> None:
    # One declaration with two sources: settings in profile.conf, app-scoped
    # state in each policy/data/*/app.py. `load_policy` is the production entry
    # point, so it is what the shipped files must satisfy; it raises on a
    # duplicate key or an unknown section instead of returning a merged list.
    declaration = load_policy()
    assert len(declaration) >= 30, f"profiles have only {len(declaration)} entries"

    seen: set[str] = set()
    for entry in declaration:
        assert entry.reason != "", f"{entry.namespace}/{entry.key} has no reason"
        name = key(entry)
        assert name not in seen, f"{name} is declared twice"
        seen.add(name)

    # Every store must be exercised, or the merged command hides one of them.
    assert any(store_for(entry) is SETTINGS for entry in declaration)
    assert any(store_for(entry) is DEVICE_CONFIG for entry in declaration)
    for store_type in (PermissionsStore, AppOpsStore, RoleStore, DeviceIdleStore):
        assert any(isinstance(store_for(entry), store_type) for entry in declaration), (
            f"no entry exercises the {store_type.__name__} store"
        )


def test_shipped_profile_keeps_platform_traps_out() -> None:
    # Verified on the work phone: these keys do not exist, belong to the other
    # store, or are owned by another app. Comments may mention them, entries may
    # not.
    profile = parse((REPO / "policy" / "profile.conf").read_text(encoding="utf-8"))
    forbidden = {
        "night_display": "Samsung uses blue_light_filter*",
        "alarm_vibration": "no such key on this firmware",
        "screen_off_timeout": "owned by Keep Screen On",
    }
    for entry in profile:
        assert entry.key not in forbidden, f"{entry.key} is in the profile: {forbidden[entry.key]}"
    for namespace in ("system", "secure", "global"):
        assert section_store(namespace) is SETTINGS
    assert section_store("telephony") is DEVICE_CONFIG
    assert section_store("applink") is None


# The exact shell commands matter: `settings put` and `cmd device_config put`
# are the only difference between the stores.
@pytest.mark.parametrize(
    ("store", "parts", "want"),
    [
        ("settings", ("put", "system", "a", "1"), "settings put system a 1"),
        ("settings", ("get", "secure", "b"), "settings get secure b"),
        ("settings", ("put", "secure", "b", ""), "settings put secure b ''"),
        (
            "settings",
            ("put", "secure", "b", "two words"),
            "settings put secure b 'two words'",
        ),
        (
            "device_config",
            ("put", "telephony", "c", "2"),
            "cmd device_config put telephony c 2",
        ),
        (
            "device_config",
            ("get", "telephony", "d"),
            "cmd device_config get telephony d",
        ),
        (
            "device_config",
            ("get", "telephony", "a;reboot"),
            "cmd device_config get telephony 'a;reboot'",
        ),
        ("settings", ("delete", "secure", "b"), "settings delete secure b"),
        (
            "device_config",
            ("delete", "telephony", "c"),
            "cmd device_config delete telephony c",
        ),
    ],
)
def test_store_commands(store: str, parts: tuple[str, ...], want: str) -> None:
    target = SETTINGS if store == "settings" else DEVICE_CONFIG
    assert target.command(*parts) == want


@pytest.mark.parametrize(
    ("value", "want"),
    [
        ("1", "1"),
        ("opportunistic", "opportunistic"),
        ("0.0", "0.0"),
        ("", "''"),
        ("two words", "'two words'"),
        ("it's", "'it'\\''s'"),
        ("$HOME", "'$HOME'"),
    ],
)
def test_quote(value: str, want: str) -> None:
    assert quote(value) == want


def test_kv_read_null_is_unset() -> None:
    device = FakeDevice({"settings get secure a": "null\n"})
    assert SETTINGS.read(device, Entry("secure", "a")) == ("", State.UNSET)
    assert device.reads == ["settings get secure a"]


def test_kv_read_trims_and_is_explicit() -> None:
    device = FakeDevice({"settings get secure a": " 1 \n"})
    assert SETTINGS.read(device, Entry("secure", "a")) == ("1", State.EXPLICIT)


def test_kv_write_and_reset() -> None:
    device = FakeDevice(out="ok\n")
    assert SETTINGS.write(device, Entry("secure", "a", "1")) == (True, "ok")
    assert SETTINGS.reset(device, Entry("secure", "a")) == (True, "ok")
    assert device.shells == ["settings put secure a 1", "settings delete secure a"]


def test_kv_write_falls_back_to_stderr() -> None:
    device = FakeDevice(code=1, err="Error: bad\n")
    assert SETTINGS.write(device, Entry("secure", "a", "1")) == (False, "Error: bad")


def test_read_all_captures_each_store() -> None:
    device = FakeDevice(
        {
            "settings get secure a": "1\n",
            "cmd deviceidle whitelist": "user,com.example.app,10123\n",
        }
    )
    entries = [Entry("secure", "a"), Entry("deviceidle", "com.example.app")]
    before = read_all(device, entries)
    assert before == {
        "secure/a": SnapshotEntry(state=State.EXPLICIT, value="1"),
        "deviceidle/com.example.app": SnapshotEntry(state=State.EXPLICIT, value="allow"),
    }


def test_read_all_wraps_a_read_error() -> None:
    device = FakeDevice(read_error="offline")
    with pytest.raises(AdbError) as caught:
        read_all(device, [Entry("secure", "a")])
    assert str(caught.value) == "reading secure/a: offline"


DUMPSYS_PACKAGE = """Packages:
  Package [com.example.app] (2f3a1b):
    userId=10123
    install permissions:
      android.permission.INTERNET: granted=true
    User 10: ceDataInode=1
      runtime permissions:
        android.permission.CAMERA: granted=true
        android.permission.BODY_SENSORS: granted=true
    User 0: ceDataInode=2
      runtime permissions:
        android.permission.CAMERA: granted=false, flags=[ USER_SET ]
        android.permission.RECORD_AUDIO: granted=true, flags=[ USER_SET ]
"""


@pytest.mark.parametrize(
    ("permission", "want", "state"),
    [
        ("android.permission.CAMERA", "revoked", State.EXPLICIT),
        ("android.permission.RECORD_AUDIO", "granted", State.EXPLICIT),
        ("android.permission.BODY_SENSORS", "", State.UNSET),
        ("android.permission.INTERNET", "", State.UNSET),
        ("android.permission.POST_NOTIFICATIONS", "", State.UNSET),
        ("android.permission.CAM", "", State.UNSET),
    ],
    ids=[
        "revoked for user 0 though granted for user 10",
        "granted",
        "granted to another user only",
        "install permission outside the user block",
        "absent on this platform",
        "prefix of a listed permission",
    ],
)
def test_parse_permission(permission: str, want: str, state: State) -> None:
    assert _parse_permission(DUMPSYS_PACKAGE, permission) == (want, state)


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("permissions", "com.example.app:android.permission.CAMERA", "granted"),
            "pm grant com.example.app android.permission.CAMERA",
            None,
        ),
        (
            Entry("permissions", "com.example.app:android.permission.CAMERA", "revoked"),
            "pm revoke com.example.app android.permission.CAMERA",
            None,
        ),
        (
            Entry("permissions", "com.example.app:android.permission.CAMERA", "on"),
            None,
            "granted or revoked",
        ),
        (
            Entry("permissions", "com.example.app", "granted"),
            None,
            "<package>:<name>",
        ),
        (
            Entry("permissions", "com.example.app:bad perm", "granted"),
            None,
            "invalid permission",
        ),
        (
            Entry("permissions", "bad package:android.permission.CAMERA", "granted"),
            None,
            "not a package name",
        ),
    ],
    ids=[
        "grant",
        "revoke",
        "unknown value",
        "missing separator",
        "invalid permission",
        "invalid package",
    ],
)
def test_permissions_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: PermissionsStore().write_command(entry), want, want_error)


def test_permissions_reset_command() -> None:
    got = PermissionsStore().reset_command("com.example.app:android.permission.CAMERA")
    assert got == "pm revoke com.example.app android.permission.CAMERA"


APP_OPS_OUTPUT = """CAMERA: allow; time=+1h32m12s ago
Uid mode: RECORD_AUDIO: ignore; time=+3d2h ago
Default mode: deny
"""


@pytest.mark.parametrize(
    ("op", "want", "state"),
    [
        ("CAMERA", "allow", State.EXPLICIT),
        ("RECORD_AUDIO", "ignore", State.EXPLICIT),
        ("VIBRATE", "deny", State.IMPLICIT),
    ],
    ids=["stored row is explicit", "uid row is explicit", "default is implicit"],
)
def test_parse_app_op(op: str, want: str, state: State) -> None:
    assert _parse_app_op(APP_OPS_OUTPUT, op) == (want, state)


def test_parse_app_op_unknown_operation() -> None:
    out = "Error: Unknown operation string: NOPE"
    assert _parse_app_op(out, "NOPE") == ("", State.UNSET)


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("appops", "com.example.app:CAMERA", "allow"),
            "cmd appops set com.example.app CAMERA allow",
            None,
        ),
        (
            Entry("appops", "com.example.app:RECORD_AUDIO", "ignore"),
            "cmd appops set com.example.app RECORD_AUDIO ignore",
            None,
        ),
        (
            Entry("appops", "com.example.app:CAMERA", "ALLOW"),
            None,
            "invalid app ops mode",
        ),
        (
            Entry("appops", "com.example.app:camera", "allow"),
            None,
            "invalid app op",
        ),
    ],
    ids=["allow", "any lowercase word", "uppercase mode", "lowercase op"],
)
def test_appops_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: AppOpsStore().write_command(entry), want, want_error)


def test_appops_reset_command() -> None:
    got = AppOpsStore().reset_command("com.example.app:CAMERA")
    assert got == "cmd appops set com.example.app CAMERA default"


@pytest.mark.parametrize(
    ("out", "want"),
    [
        ("com.example.sms\n", "com.example.sms"),
        (
            "com.example.sms;com.example.backup\n",
            "com.example.sms,com.example.backup",
        ),
        ("\n", ""),
        ("", ""),
    ],
    ids=[
        "single holder",
        "holders joined with a semicolon",
        "unheld prints an empty line",
        "empty output",
    ],
)
def test_parse_role(out: str, want: str) -> None:
    assert _parse_role(out) == want


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("role", "android.app.role.SMS", "com.example.sms"),
            "cmd role add-role-holder android.app.role.SMS com.example.sms",
            None,
        ),
        (
            Entry("role", "android.app.role.SMS", ""),
            "cmd role clear-role-holders android.app.role.SMS",
            None,
        ),
        (
            Entry("role", "android.app.role.SMS", "com.example.sms,com.example.backup"),
            None,
            "more than one package",
        ),
        (Entry("role", "SMS", "com.example.sms"), None, "invalid role"),
        (
            Entry("role", "android.app.role.SMS", "com.example.sms;reboot"),
            None,
            "not a package name",
        ),
    ],
    ids=[
        "one holder",
        "empty clears",
        "two packages are rejected",
        "invalid role",
        "invalid package",
    ],
)
def test_role_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: RoleStore().write_command(entry), want, want_error)


def test_role_reset_command() -> None:
    got = RoleStore().reset_command("android.app.role.SMS")
    assert got == "cmd role clear-role-holders android.app.role.SMS"


DEVICE_IDLE_OUTPUT = """system-excidle,com.example.app,10123
system,com.example.other,1000
user,com.example.allowed,10124
"""


@pytest.mark.parametrize(
    ("package", "want", "state"),
    [
        ("com.example.allowed", "allow", State.EXPLICIT),
        ("com.example.app", "allow", State.IMPLICIT),
        ("com.example.other", "ignore", State.EXPLICIT),
        ("com.example.absent", "ignore", State.EXPLICIT),
    ],
    ids=[
        "user row is explicit",
        "system-excidle row is implicit",
        "system row ignores",
        "absent package ignores",
    ],
)
def test_parse_device_idle(package: str, want: str, state: State) -> None:
    assert _parse_device_idle(DEVICE_IDLE_OUTPUT, package) == (want, state)


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("deviceidle", "com.example.app", "allow"),
            "cmd deviceidle whitelist +com.example.app",
            None,
        ),
        (
            Entry("deviceidle", "com.example.app", "ignore"),
            "cmd deviceidle whitelist -com.example.app",
            None,
        ),
        (
            Entry("deviceidle", "com.example.app", "deny"),
            None,
            "allow or ignore",
        ),
        (
            Entry("deviceidle", "com.example/app", "allow"),
            None,
            "not a package name",
        ),
    ],
    ids=["allow", "ignore", "unknown value", "invalid package"],
)
def test_deviceidle_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: DeviceIdleStore().write_command(entry), want, want_error)


def test_deviceidle_reset_command() -> None:
    got = DeviceIdleStore().reset_command("com.example.app")
    assert got == "cmd deviceidle whitelist -com.example.app"


@pytest.mark.parametrize(
    ("out", "want", "state"),
    [
        ("5", "exempted", State.IMPLICIT),
        ("10\n", "active", State.EXPLICIT),
        ("20", "working_set", State.EXPLICIT),
        ("30", "frequent", State.EXPLICIT),
        ("40", "rare", State.EXPLICIT),
        ("45", "restricted", State.EXPLICIT),
        ("50", "never", State.EXPLICIT),
    ],
    ids=[
        "exempted is the doze floor",
        "active",
        "working set",
        "frequent",
        "rare",
        "restricted",
        "never",
    ],
)
def test_parse_standby(out: str, want: str, state: State) -> None:
    assert _parse_standby(out) == (want, state)


def test_parse_standby_unknown_number() -> None:
    with pytest.raises(ProfileError):
        _parse_standby("99")


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("standby", "com.example.app", "active"),
            "am set-standby-bucket com.example.app active",
            None,
        ),
        (
            Entry("standby", "com.example.app", "never"),
            "am set-standby-bucket com.example.app never",
            None,
        ),
        (
            Entry("standby", "com.example.app", "exempted"),
            None,
            "doze-whitelisted",
        ),
        (
            Entry("standby", "com.example.app", "sleeping"),
            None,
            "invalid standby bucket",
        ),
    ],
    ids=["active", "never is writable", "exempted is derived", "unknown bucket"],
)
def test_standby_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: StandbyStore().write_command(entry), want, want_error)


def test_standby_reset_refuses() -> None:
    device = FakeDevice()
    ok, message = StandbyStore().reset(device, Entry("standby", "com.example.app"))
    assert ok is False
    assert message == "a standby bucket cannot be unset"
    assert device.shells == []


@pytest.mark.parametrize(
    ("out", "want", "want_error"),
    [
        (
            "Locales for com.example.app for user 0 are [en-US]",
            "en-US",
            None,
        ),
        (
            "Locales for com.example.app for user 0 are [en-US,sl-SI]",
            "en-US,sl-SI",
            None,
        ),
        ("Locales for com.example.app for user 0 are []", "", None),
        (
            "Unknown package com.example.notinstalled for userId 0",
            None,
            "Unknown package com.example.notinstalled",
        ),
    ],
    ids=["one locale", "locale list", "system default", "unknown package"],
)
def test_parse_locales(out: str, want: str | None, want_error: str | None) -> None:
    if want_error is not None:
        with pytest.raises(ProfileError) as caught:
            _parse_locales(out)
        assert want_error in str(caught.value)
        return
    assert _parse_locales(out) == want


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("locale", "com.example.app", "en-US,sl-SI"),
            "cmd locale set-app-locales com.example.app --locales en-US,sl-SI",
            None,
        ),
        (
            Entry("locale", "com.example.app", ""),
            "cmd locale set-app-locales com.example.app --locales ''",
            None,
        ),
        (
            Entry("locale", "com.example.app", "en_US"),
            None,
            "invalid locale",
        ),
        (
            Entry("locale", "bad package", "en-US"),
            None,
            "not a package name",
        ),
    ],
    ids=[
        "locale list",
        "empty clears",
        "underscore is not a locale separator",
        "invalid package",
    ],
)
def test_locale_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: LocaleStore().write_command(entry), want, want_error)


def test_locale_reset_command() -> None:
    got = LocaleStore().reset_command("com.example.app")
    assert got == "cmd locale set-app-locales com.example.app --locales ''"


@pytest.mark.parametrize(
    ("out", "want", "state"),
    [
        (
            ("com.example.app:\n  User 0:\n    Verification link handling allowed: true\n"),
            "allowed",
            State.EXPLICIT,
        ),
        (
            ("com.example.app:\n  User 0:\n    Verification link handling allowed: false\n"),
            "disallowed",
            State.EXPLICIT,
        ),
        (
            "com.morphe.android.youtube:\n  User 0:\n    Selection state:\n",
            "",
            State.UNSET,
        ),
        ("", "", State.UNSET),
    ],
    ids=["allowed", "disallowed", "declares no web links", "empty output"],
)
def test_parse_app_links(out: str, want: str, state: State) -> None:
    assert _parse_app_links(out) == (want, state)


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("applinks", "com.example.app", "allowed"),
            "pm set-app-links-allowed --user 0 --package com.example.app true",
            None,
        ),
        (
            Entry("applinks", "com.example.app", "disallowed"),
            "pm set-app-links-allowed --user 0 --package com.example.app false",
            None,
        ),
        (
            Entry("applinks", "com.example.app", "deny"),
            None,
            "allowed or disallowed",
        ),
    ],
    ids=["allowed", "disallowed", "unknown value"],
)
def test_applinks_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: AppLinksStore().write_command(entry), want, want_error)


def test_applinks_reset_refuses() -> None:
    device = FakeDevice()
    ok, message = AppLinksStore().reset(device, Entry("applinks", "com.example.app"))
    assert ok is False
    assert message == "link handling has no unset state"
    assert device.shells == []


DOMAIN_OUTPUT = """com.example.app:
  User 0:
    Verification link handling allowed: true
    Selection state:
      Enabled:
        foo-bar.example
    Domain verification state:
      foo-bar.example: approved
      other.example: denied
      configured.example: system_configured
      old.example: migrated
      failure.example: legacy_failure
      coded.example: 1024
      plain.example: none
"""


@pytest.mark.parametrize(
    ("domain", "want", "state"),
    [
        ("foo-bar.example", "approved", State.EXPLICIT),
        ("other.example", "denied", State.EXPLICIT),
        ("plain.example", "none", State.EXPLICIT),
        ("configured.example", "system_configured", State.IMPLICIT),
        ("old.example", "migrated", State.IMPLICIT),
        ("failure.example", "legacy_failure", State.IMPLICIT),
        ("coded.example", "1024", State.IMPLICIT),
        ("absent.example", "", State.UNSET),
    ],
    ids=[
        "approved",
        "denied",
        "none is writable",
        "system_configured is derived",
        "migrated is derived",
        "legacy_failure is derived",
        "codes from 1024 up are derived",
        "undeclared domain is unset",
    ],
)
def test_parse_domain_state(domain: str, want: str, state: State) -> None:
    assert _parse_domain_state(DOMAIN_OUTPUT, domain) == (want, state)


# Each raw token the platform prints, with the state it implies.
@pytest.mark.parametrize(
    ("token", "state"),
    [
        ("none", State.EXPLICIT),
        ("verified", State.EXPLICIT),
        ("approved", State.EXPLICIT),
        ("denied", State.EXPLICIT),
        ("0", State.EXPLICIT),
        ("1", State.EXPLICIT),
        ("2", State.EXPLICIT),
        ("3", State.EXPLICIT),
        ("system_configured", State.IMPLICIT),
        ("pre_verified", State.IMPLICIT),
        ("migrated", State.IMPLICIT),
        ("restored", State.IMPLICIT),
        ("legacy_failure", State.IMPLICIT),
        ("1024", State.IMPLICIT),
        ("4096", State.IMPLICIT),
    ],
)
def test_parse_domain_state_tokens(token: str, state: State) -> None:
    out = f"Domain verification state:\n  foo-bar.example: {token}\n"
    assert _parse_domain_state(out, "foo-bar.example") == (token, state)


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("applinks_domain", "com.example.app:foo-bar.example", "none"),
            "pm set-app-links --package com.example.app 0 foo-bar.example",
            None,
        ),
        (
            Entry("applinks_domain", "com.example.app:foo-bar.example", "verified"),
            "pm set-app-links --package com.example.app 1 foo-bar.example",
            None,
        ),
        (
            Entry("applinks_domain", "com.example.app:foo-bar.example", "1"),
            "pm set-app-links --package com.example.app 1 foo-bar.example",
            None,
        ),
        (
            Entry("applinks_domain", "com.example.app:foo-bar.example", "approved"),
            "pm set-app-links --package com.example.app 2 foo-bar.example",
            None,
        ),
        (
            Entry("applinks_domain", "com.example.app:foo-bar.example", "denied"),
            "pm set-app-links --package com.example.app 3 foo-bar.example",
            None,
        ),
        (
            Entry("applinks_domain", "com.example.app:*.discord.com", "approved"),
            "pm set-app-links --package com.example.app 2 '*.discord.com'",
            None,
        ),
        (
            Entry(
                "applinks_domain",
                "com.example.app:foo-bar.example",
                "system_configured",
            ),
            None,
            "use none (0), verified (1), approved (2) or denied (3)",
        ),
        (
            Entry("applinks_domain", "com.example.app:Foo.example", "approved"),
            None,
            "invalid domain",
        ),
    ],
    ids=[
        "none",
        "verified is writable",
        "bare code is writable",
        "approved",
        "denied",
        "wildcard domain",
        "platform-decided token is not writable",
        "invalid domain",
    ],
)
def test_domain_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: AppLinksDomainStore().write_command(entry), want, want_error)


@pytest.mark.parametrize(
    ("key_value", "want"),
    [
        (
            "com.example.app:foo-bar.example",
            "pm set-app-links --package com.example.app 0 foo-bar.example",
        ),
        (
            "com.example.app:*.discord.com",
            "pm set-app-links --package com.example.app 0 '*.discord.com'",
        ),
    ],
)
def test_domain_reset_command(key_value: str, want: str) -> None:
    assert AppLinksDomainStore().reset_command(key_value) == want


SELECTION_OUTPUT = """com.example.app:
  User 0:
    Verification link handling allowed: true
    Selection state:
      Enabled:
        *.discord.com
        foo-bar.example
      Disabled:
        disabled.example
    Domain verification state:
      foo-bar.example: approved
"""


@pytest.mark.parametrize(
    ("domain", "want", "state"),
    [
        ("*.discord.com", "enabled", State.EXPLICIT),
        ("foo-bar.example", "enabled", State.EXPLICIT),
        ("disabled.example", "disabled", State.EXPLICIT),
        ("absent.example", "", State.UNSET),
    ],
    ids=["enabled wildcard", "enabled", "disabled", "absent from both lists"],
)
def test_parse_selection_state(domain: str, want: str, state: State) -> None:
    assert _parse_selection_state(SELECTION_OUTPUT, domain) == (want, state)


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (
            Entry("applinks_selection", "com.example.app:foo-bar.example", "enabled"),
            (
                "pm set-app-links-user-selection --user 0 --package com.example.app"
                " true foo-bar.example"
            ),
            None,
        ),
        (
            Entry("applinks_selection", "com.example.app:foo-bar.example", "disabled"),
            (
                "pm set-app-links-user-selection --user 0 --package com.example.app"
                " false foo-bar.example"
            ),
            None,
        ),
        (
            Entry("applinks_selection", "com.example.app:*.discord.com", "enabled"),
            (
                "pm set-app-links-user-selection --user 0 --package com.example.app"
                " true '*.discord.com'"
            ),
            None,
        ),
        (
            Entry("applinks_selection", "com.example.app:foo-bar.example", "allowed"),
            None,
            "enabled or disabled",
        ),
        (
            Entry("applinks_selection", "com.example.app:Foo.example", "enabled"),
            None,
            "invalid domain",
        ),
    ],
    ids=["enabled", "disabled", "wildcard domain", "unknown value", "invalid domain"],
)
def test_selection_write_command(entry: Entry, want: str | None, want_error: str | None) -> None:
    check_command(lambda: AppLinksSelectionStore().write_command(entry), want, want_error)


def test_selection_reset_refuses() -> None:
    device = FakeDevice()
    ok, message = AppLinksSelectionStore().reset(
        device, Entry("applinks_selection", "com.example.app:foo-bar.example")
    )
    assert ok is False
    assert "no unset state" in message
    assert device.shells == []


@pytest.mark.parametrize(
    ("entry", "want", "want_error"),
    [
        (Entry("global", "window_animation_scale"), [], None),
        (
            Entry("permissions", "com.example.app:android.permission.CAMERA"),
            ["com.example.app"],
            None,
        ),
        (Entry("appops", "com.example.app:CAMERA"), ["com.example.app"], None),
        (
            Entry("role", "android.app.role.SMS", "com.example.sms"),
            ["com.example.sms"],
            None,
        ),
        (Entry("role", "android.app.role.SMS", ""), [], None),
        (Entry("deviceidle", "com.example.app"), ["com.example.app"], None),
        (Entry("standby", "com.example.app"), ["com.example.app"], None),
        (Entry("locale", "com.example.app"), ["com.example.app"], None),
        (Entry("applinks", "com.example.app"), ["com.example.app"], None),
        (
            Entry("applinks_domain", "com.example.app:foo-bar.example"),
            ["com.example.app"],
            None,
        ),
        (
            Entry("applinks_selection", "com.example.app:*.discord.com"),
            ["com.example.app"],
            None,
        ),
        (Entry("permissions", "com.example.app"), None, "<package>:<name>"),
        (Entry("standby", "not a package"), None, "not a package name"),
        (
            Entry("role", "android.app.role.SMS", "a.b,c.d"),
            None,
            "more than one package",
        ),
    ],
    ids=[
        "settings names no package",
        "permission",
        "app op",
        "role holder",
        "empty role",
        "deviceidle",
        "standby",
        "locale",
        "applinks",
        "applinks domain",
        "applinks selection",
        "malformed permission key",
        "malformed standby key",
        "multi-package role",
    ],
)
def test_packages(entry: Entry, want: list[str] | None, want_error: str | None) -> None:
    if want_error is not None:
        with pytest.raises(ProfileError) as caught:
            packages(entry)
        assert want_error in str(caught.value)
        return
    assert packages(entry) == want


@pytest.mark.parametrize(
    ("prefix", "parts", "want"),
    [
        (
            ("cmd", "locale", "set-app-locales"),
            ("com.example.app", "--locales", ""),
            "cmd locale set-app-locales com.example.app --locales ''",
        ),
        (
            ("pm", "grant"),
            ("com.example.app", "two words"),
            "pm grant com.example.app 'two words'",
        ),
        (
            ("pm", "set-app-links", "--package"),
            ("com.example.app", "0", "foo-bar.example"),
            "pm set-app-links --package com.example.app 0 foo-bar.example",
        ),
        (
            ("pm", "set-app-links-user-selection", "--user", "0", "--package"),
            ("com.example.app", "true", "*.discord.com"),
            (
                "pm set-app-links-user-selection --user 0 --package com.example.app"
                " true '*.discord.com'"
            ),
        ),
    ],
    ids=["empty value", "value with a space", "domain with a hyphen", "wildcard domain"],
)
def test_mutated_commands_quote_every_part(
    prefix: tuple[str, ...], parts: tuple[str, ...], want: str
) -> None:
    assert render(prefix, *parts) == want


def test_equivalent_rejects_unknown_domain_value() -> None:
    assert equivalent(Entry("applinks_domain", "", "bogus"), "none") is False
    assert equivalent(Entry("applinks_domain", "", "none"), "bogus") is False


def test_a_domain_verification_user_block_does_not_hide_the_runtime_block() -> None:
    dump = """\
Domain verification status:
  com.example.app:
    User 0:
      Verification link handling allowed: true
      Selection state:
        Disabled:
          example.com

  User 0: ceDataInode=2
    runtime permissions:
      android.permission.POST_NOTIFICATIONS: granted=true, flags=[ USER_SET ]
"""
    assert _parse_permission(dump, "android.permission.POST_NOTIFICATIONS") == (
        "granted",
        State.EXPLICIT,
    )


OTHER_PACKAGE_DUMP = """Packages:
  Package [com.other.app] (9c8b7a):
    User 0: ceDataInode=3
      runtime permissions:
        android.permission.POST_NOTIFICATIONS: granted=true, flags=[ USER_SET ]
        android.permission.CAMERA: granted=true, flags=[ USER_SET ]
"""

APP_LINKS_OUTPUT = """Domain verification status:
  com.example.app:
    ID: 4efb5a31-be8e-46a5-9722-f00474442875
    Domain verification state:
      example.com: verified
    User 0:
      Verification link handling allowed: true
      Selection state:
        Enabled:
          example.com
"""

# A device that answers both the one-command-per-entry reads and the bulk
# reads, so `read_many` and the per-entry `read` can be held against the same
# output. A key the `list` path must not answer (absent, or folded onto a
# second line) still has its own `get` answer, as on a phone.
BULK_ANSWERS: dict[str, str] = {
    "settings list system": "clock=24\nempty=\nname=two=parts\n",
    "settings get system clock": "24\n",
    "settings get system empty": "\n",
    "settings get system name": "two=parts\n",
    "settings get system missing": "null\n",
    "settings list secure": "nulled=null\n",
    "settings get secure nulled": "null\n",
    "settings list global": "private_dns_mode=opportunistic\n",
    "settings get global private_dns_mode": "opportunistic\n",
    "cmd device_config list telephony": "ramping_ringer_enabled=true\n",
    "cmd device_config get telephony ramping_ringer_enabled": "true\n",
    "cmd device_config get telephony missing": "null\n",
    "dumpsys package com.example.app": DUMPSYS_PACKAGE,
    "dumpsys package com.other.app": OTHER_PACKAGE_DUMP,
    "cmd appops get com.example.app": APP_OPS_OUTPUT,
    "cmd appops get com.example.app CAMERA": "CAMERA: allow; time=+1h32m12s ago\n",
    "cmd appops get com.example.app RECORD_AUDIO": "RECORD_AUDIO: ignore; time=+3d2h ago\n",
    "cmd appops get com.example.app VIBRATE": "No operations.\nDefault mode: deny\n",
    "cmd appops get com.example.app POST_NOTIFICATION": "No operations.\nDefault mode: deny\n",
    "cmd role get-role-holders android.app.role.SMS": "org.fossify.messages\n",
    "cmd role get-role-holders android.app.role.HOME": "\n",
    "cmd deviceidle whitelist": ("user,com.example.app,10123\nsystem-excidle,com.other.app,1000\n"),
    "am get-standby-bucket com.example.app": "10\n",
    "cmd locale get-app-locales com.example.app": (
        "Locales for com.example.app for user 0 are [en-US]\n"
    ),
    "pm get-app-links --user 0 com.example.app": APP_LINKS_OUTPUT,
}


def bulk_device() -> FakeDevice:
    return FakeDevice(dict(BULK_ANSWERS))


@pytest.mark.parametrize(
    ("entries", "calls"),
    [
        pytest.param(
            [
                Entry("system", "clock"),
                Entry("system", "empty"),
                Entry("system", "name"),
                Entry("system", "missing"),
                Entry("secure", "nulled"),
                Entry("global", "private_dns_mode"),
            ],
            [
                "settings list system",
                "settings list secure",
                "settings list global",
                "settings get system missing",
            ],
            id="settings namespaces: one list each, gets only for absent keys",
        ),
        pytest.param(
            [
                Entry("telephony", "ramping_ringer_enabled"),
                Entry("telephony", "missing"),
            ],
            [
                "cmd device_config list telephony",
                "cmd device_config get telephony missing",
            ],
            id="device config",
        ),
        pytest.param(
            [
                Entry("permissions", "com.example.app:android.permission.CAMERA", "revoked"),
                Entry("permissions", "com.example.app:android.permission.RECORD_AUDIO", "granted"),
                Entry("permissions", "com.example.app:android.permission.BODY_SENSORS", "granted"),
                Entry(
                    "permissions", "com.other.app:android.permission.POST_NOTIFICATIONS", "granted"
                ),
            ],
            [
                (
                    "dumpsys package com.example.app; echo __androidctl_bulk_mark__;"
                    " dumpsys package com.other.app; echo __androidctl_bulk_mark__"
                ),
            ],
            id="permissions: one dump per package in one invocation",
        ),
        pytest.param(
            [Entry("permissions", "com.example.app:android.permission.CAMERA", "revoked")],
            ["dumpsys package com.example.app"],
            id="permissions: one package needs no marker",
        ),
        pytest.param(
            [
                Entry("appops", "com.example.app:CAMERA", "allow"),
                Entry("appops", "com.example.app:RECORD_AUDIO", "ignore"),
                Entry("appops", "com.example.app:VIBRATE", "deny"),
                Entry("appops", "com.example.app:POST_NOTIFICATION", "deny"),
            ],
            [
                "cmd appops get com.example.app",
                "cmd appops get com.example.app VIBRATE",
                "cmd appops get com.example.app POST_NOTIFICATION",
            ],
            id="app ops: one list per package, a get for an op it does not hold",
        ),
        pytest.param(
            [Entry("role", "android.app.role.SMS"), Entry("role", "android.app.role.HOME")],
            [
                (
                    "cmd role get-role-holders android.app.role.SMS; echo __androidctl_bulk_mark__;"
                    " cmd role get-role-holders android.app.role.HOME; echo __androidctl_bulk_mark__"
                ),
            ],
            id="roles: batched into one invocation",
        ),
        pytest.param(
            [Entry("role", "android.app.role.SMS")],
            ["cmd role get-role-holders android.app.role.SMS"],
            id="role: one role needs no marker",
        ),
        pytest.param(
            [
                Entry("deviceidle", "com.example.app"),
                Entry("deviceidle", "com.other.app"),
            ],
            ["cmd deviceidle whitelist"],
            id="device idle: one whitelist shared",
        ),
        pytest.param(
            [Entry("standby", "com.example.app")],
            ["am get-standby-bucket com.example.app"],
            id="standby: one read per entry",
        ),
        pytest.param(
            [Entry("locale", "com.example.app")],
            ["cmd locale get-app-locales com.example.app"],
            id="locale: one read per entry",
        ),
        pytest.param(
            [
                Entry("applinks", "com.example.app"),
                Entry("applinks_domain", "com.example.app:example.com"),
                Entry("applinks_selection", "com.example.app:example.com"),
            ],
            ["pm get-app-links --user 0 com.example.app"],
            id="app links: three stores share one read",
        ),
    ],
)
def test_read_many_agrees_with_the_per_entry_read(entries: list[Entry], calls: list[str]) -> None:
    device = bulk_device()
    got = read_many(device, entries)

    assert device.reads == calls
    assert device.shells == []
    for entry in entries:
        assert got[key(entry)] == store_for(entry).read(device, entry), key(entry)


@pytest.mark.parametrize(
    ("entry", "want"),
    [
        pytest.param(Entry("system", "clock"), ("24", State.EXPLICIT), id="listed value"),
        pytest.param(Entry("system", "empty"), ("", State.EXPLICIT), id="listed empty value"),
        pytest.param(Entry("system", "name"), ("two=parts", State.EXPLICIT), id="value holding ="),
        pytest.param(Entry("system", "missing"), ("", State.UNSET), id="absent becomes unset"),
        pytest.param(Entry("secure", "nulled"), ("", State.UNSET), id="listed null is unset"),
        pytest.param(
            Entry("permissions", "com.example.app:android.permission.CAMERA"),
            ("revoked", State.EXPLICIT),
            id="permission refused for user 0",
        ),
        pytest.param(
            Entry("permissions", "com.example.app:android.permission.BODY_SENSORS"),
            ("", State.UNSET),
            id="permission absent from the dump",
        ),
        pytest.param(
            Entry("appops", "com.example.app:VIBRATE"),
            ("deny", State.IMPLICIT),
            id="derived app op",
        ),
        pytest.param(
            Entry("appops", "com.example.app:POST_NOTIFICATION"),
            ("deny", State.IMPLICIT),
            id="app op with no row",
        ),
        pytest.param(
            Entry("role", "android.app.role.HOME"), ("", State.EXPLICIT), id="unheld role"
        ),
        pytest.param(
            Entry("deviceidle", "com.other.app"), ("allow", State.IMPLICIT), id="system-excidle"
        ),
    ],
)
def test_read_many_states(entry: Entry, want: tuple[str, State]) -> None:
    assert read_many(bulk_device(), [entry])[key(entry)] == want


def test_a_second_packages_permission_is_not_answered_by_the_first_packages_dump() -> None:
    # package one refuses CAMERA and never held POST_NOTIFICATIONS; package two
    # grants both. A dump answered across the boundary would report one's
    # values for two.
    entries = [
        Entry("permissions", "com.example.app:android.permission.POST_NOTIFICATIONS", "granted"),
        Entry("permissions", "com.other.app:android.permission.CAMERA", "revoked"),
        Entry("permissions", "com.other.app:android.permission.POST_NOTIFICATIONS", "granted"),
        Entry("permissions", "com.example.app:android.permission.CAMERA", "granted"),
    ]
    got = read_many(bulk_device(), entries)

    assert got["permissions/com.example.app:android.permission.POST_NOTIFICATIONS"] == (
        "",
        State.UNSET,
    )
    assert got["permissions/com.example.app:android.permission.CAMERA"] == (
        "revoked",
        State.EXPLICIT,
    )
    assert got["permissions/com.other.app:android.permission.CAMERA"] == ("granted", State.EXPLICIT)
    assert got["permissions/com.other.app:android.permission.POST_NOTIFICATIONS"] == (
        "granted",
        State.EXPLICIT,
    )


def test_the_settings_list_and_the_get_fallback_agree() -> None:
    device = FakeDevice(
        {
            # `folded` continues on a second line, so the list cannot carry it and
            # `get` has to answer. `missing` is absent from the list altogether.
            "settings list secure": "answered=1\nempty=\nfolded=first line\nsecond line\n",
            "settings get secure answered": "1\n",
            "settings get secure empty": "\n",
            "settings get secure folded": "first line\nsecond line\n",
            "settings get secure missing": "null\n",
        }
    )
    entries = [
        Entry("secure", "answered"),
        Entry("secure", "empty"),
        Entry("secure", "folded"),
        Entry("secure", "missing"),
    ]
    got = read_many(device, entries)

    assert device.reads == [
        "settings list secure",
        "settings get secure folded",
        "settings get secure missing",
    ]
    for entry in entries:
        assert got[key(entry)] == store_for(entry).read(device, entry), key(entry)
    assert got["secure/answered"] == ("1", State.EXPLICIT)
    assert got["secure/empty"] == ("", State.EXPLICIT)
    assert got["secure/folded"] == ("first line\nsecond line", State.EXPLICIT)
    assert got["secure/missing"] == ("", State.UNSET)


def test_read_many_reads_nothing_for_an_empty_profile() -> None:
    device = bulk_device()
    assert read_many(device, []) == {}
    assert device.reads == []
    assert device.shells == []
