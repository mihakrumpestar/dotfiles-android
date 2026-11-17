"""Debloat facts, parsed from recorded device output and checked for safety."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from androidctl import debloat
from androidctl.adb import AdbError, ShellResult

REPO = Path(__file__).resolve().parent.parent
ROLE = "cmd role get-role-holders android.app.role."
ROLES = ["HOME", "DIALER", "SMS", "BROWSER", "ASSISTANT", "EMERGENCY", "WALLET"]


class FakeDevice:
    """A device whose answers are keyed by the exact command string."""

    serial = "SERIAL"

    def __init__(self, replies: dict[str, str] | None = None):
        self.replies = dict(replies or {})
        self.results: dict[str, ShellResult] = {}
        self.reads: list[str] = []
        self.shells: list[str] = []

    def read(self, command: str) -> str:
        self.reads.append(command)
        return self.replies[command]

    def shell(self, command: str) -> ShellResult:
        self.shells.append(command)
        if command in self.results:
            return self.results[command]
        return ShellResult(0, self.replies.get(command, ""), "")

    def run(self, *args: str) -> str:
        return self.replies[" ".join(args)]

    def answer(self, command: str, *, code: int = 0, out: str = "", err: str = "") -> None:
        self.results[command] = ShellResult(code, out, err)


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("/system/priv-app/Foo/Foo.apk", "system/priv-app"),
        ("/system/app/EasterEgg/EasterEgg.apk", "system/app"),
        ("/data/app/~~abc==/pkg-xyz==/base.apk", "data/app"),
        ("/product/overlay/X.apk", "product/overlay"),
        ("/apex/com.android.wifi@1", "apex"),
        ("null", "unknown"),
        ("", "unknown"),
        ("/", "unknown"),
    ],
)
def test_tier_names_the_partition(path: str, want: str):
    assert debloat.tier(path) == want


# A real device line: the path contains `==`, so splitting at the first `=`
# truncates the path and drops the package.
DATA_APP_LINE = (
    "package:/data/app/~~abc123def456==/"
    "android.autoinstalls.config.samsung-001122334455==/base.apk="
    "android.autoinstalls.config.samsung"
)


@pytest.mark.parametrize(
    ("out", "want"),
    [
        (DATA_APP_LINE, {"android.autoinstalls.config.samsung": "data/app"}),
        (
            "package:/system/priv-app/Foo/Foo.apk=com.foo",
            {"com.foo": "system/priv-app"},
        ),
        (
            "package:/apex/com.android.runtime@1/apex.apk=com.android.runtime",
            {"com.android.runtime": "apex"},
        ),
        ("random\npackage:no-name\n", {}),
    ],
)
def test_parse_tiers_splits_at_the_last_equals(out: str, want: dict[str, str]):
    assert debloat.parse_tiers(out) == want


@pytest.mark.parametrize("name", ["com.android.egg", "android", "a_b.c"])
def test_package_names_accept_plain_ids(name: str):
    assert debloat.valid_package(name)


# Names reach `adb shell`, so anything with shell metacharacters must fail.
@pytest.mark.parametrize(
    "name",
    ["com.a;reboot", "com.evil$(id)", "com.a b", "com.a|b", "com.a\nb"],
)
def test_package_names_reject_shell_syntax(name: str):
    assert not debloat.valid_package(name)


def test_mutate_rejects_a_shell_injection_attempt():
    device = FakeDevice()
    with pytest.raises(ValueError):
        debloat.mutate(device, "disable-user", "com.a; reboot")
    assert device.shells == []


def test_mutate_returns_the_device_message():
    device = FakeDevice()
    device.answer(
        "pm disable-user --user 0 com.egg",
        out="Package com.egg new state: disabled-user\n",
    )
    assert debloat.mutate(device, "disable-user", "com.egg") == (
        True,
        "Package com.egg new state: disabled-user",
    )


def test_mutate_reports_a_refusal_without_raising():
    device = FakeDevice()
    device.answer(
        "pm disable-user --user 0 com.phone",
        code=255,
        err="java.lang.SecurityException: protected package\n",
    )
    ok, message = debloat.mutate(device, "disable-user", "com.phone")
    assert not ok
    assert message == "java.lang.SecurityException: protected package"


def test_mutate_raises_on_a_transport_failure():
    device = FakeDevice()
    device.answer("pm disable-user --user 0 com.egg", code=-1, err="device offline")
    with pytest.raises(AdbError):
        debloat.mutate(device, "disable-user", "com.egg")


def test_disable_and_enable_use_the_two_allowed_verbs():
    device = FakeDevice()
    assert debloat.disable(device, "com.egg") == (True, "")
    assert debloat.enable(device, "com.egg") == (True, "")
    assert device.shells == [
        "pm disable-user --user 0 com.egg",
        "pm enable --user 0 com.egg",
    ]


def test_state_of_treats_a_missing_key_as_absent():
    states = {"com.on": debloat.ENABLED, "com.off": debloat.DISABLED}
    assert debloat.state_of(states, "com.on") == debloat.ENABLED
    assert debloat.state_of(states, "com.off") == debloat.DISABLED
    assert debloat.state_of(states, "com.missing") == debloat.ABSENT
    assert debloat.state_of({}, "com.missing") == debloat.ABSENT


def test_explain_finds_the_reason_not_the_stack_tail():
    refused = (
        "Exception occurred while executing 'disable-user':\n"
        "java.lang.SecurityException: [Theme] Cannot disable a protected package:"
        " com.samsung.android.themestore\n"
        "\tat com.android.server.wm.Task.m(Task.java:33)\n"
        "\tat android.os.Binder.execTransact(Binder.java:1359)"
    )
    assert debloat.explain(refused) == (
        "java.lang.SecurityException: [Theme] Cannot disable a protected package:"
        " com.samsung.android.themestore"
    )


def test_explain_finds_the_root_only_failure():
    message = debloat.explain("Failure [only root can delete system app for a particular user]")
    assert "only root can delete system app" in message


def test_explain_keeps_the_success_line():
    assert debloat.explain("Package com.android.egg new state: disabled-user\n") == (
        "Package com.android.egg new state: disabled-user"
    )


def test_explain_of_nothing_is_nothing():
    assert debloat.explain("") == ""


def test_states_reads_enabled_and_disabled_lists():
    device = FakeDevice(
        {
            "pm list packages -e": "package:com.on\npackage:com.also-on\n",
            "pm list packages -d": "package:com.off\n",
        }
    )
    assert debloat.states(device) == {
        "com.on": debloat.ENABLED,
        "com.also-on": debloat.ENABLED,
        "com.off": debloat.DISABLED,
    }
    assert device.reads == ["pm list packages -e", "pm list packages -d"]


def test_installed_asks_for_user_zero():
    device = FakeDevice({"pm list packages --user 0": "package:com.a\njunk\n  package:com.b\n"})
    assert debloat.installed(device) == {"com.a", "com.b"}
    assert device.reads == ["pm list packages --user 0"]


def test_tiers_reads_the_apk_paths():
    device = FakeDevice({"pm list packages -f": "package:/system/app/Foo/Foo.apk=com.foo"})
    assert debloat.tiers(device) == {"com.foo": "system/app"}


DUMP = """  Package [com.shared] (1234abc):
    userId=10123
    sharedUser=SharedUserSetting{abc123 com.shared.uid/10123}
    targetSdk=33
  Package [com.plain] (5678def):
    userId=10124
    targetSdk=34
"""


def test_package_facts_reads_one_dump():
    device = FakeDevice({"dumpsys package": DUMP})
    uids, sdks = debloat.package_facts(device)
    assert uids == {"com.shared": "com.shared.uid"}
    assert sdks == {"com.shared": 33, "com.plain": 34}
    assert device.reads == ["dumpsys package"]


def test_role_holders_reads_every_role_in_order():
    device = FakeDevice({ROLE + "HOME": "com.home\n"})
    device.answer(ROLE + "DIALER", code=1, err="Error: unknown role")
    holders = debloat.role_holders(device)
    assert list(holders) == ROLES
    assert holders["HOME"] == "com.home"
    assert holders["DIALER"] == ""
    assert device.shells == [ROLE + role for role in ROLES]


def test_role_holders_raises_on_a_transport_failure():
    device = FakeDevice()
    device.answer(ROLE + "HOME", code=-1, err="device offline")
    with pytest.raises(AdbError):
        debloat.role_holders(device)


def test_launchers_keeps_top_level_components_only():
    command = (
        "cmd package query-activities --components "
        "-a android.intent.action.MAIN -c android.intent.category.HOME"
    )
    device = FakeDevice({command: "  package: com.a/.Main\ncom.b/com.b.Home\n"})
    assert debloat.launchers(device) == ["com.b"]
    assert device.shells == [command]


@pytest.mark.parametrize(
    ("out", "want"),
    [("com.keyboard/.LatinIme\n", "com.keyboard"), ("", ""), ("null\n", "")],
)
def test_keyboard_reports_no_keyboard_as_empty(out: str, want: str):
    device = FakeDevice({"settings get secure default_input_method": out})
    assert debloat.keyboard(device) == want


def test_crashes_counts_fatal_exceptions():
    device = FakeDevice({"logcat -d -b crash": "FATAL EXCEPTION: main\nFATAL EXCEPTION: worker\n"})
    assert debloat.crashes(device) == 2


@pytest.mark.parametrize(("out", "want"), [("1\n", True), ("0\n", False), ("", False)])
def test_boot_completed_reads_the_property(out: str, want: bool):
    device = FakeDevice({"getprop sys.boot_completed": out})
    assert debloat.boot_completed(device) is want


def module_literals() -> dict[str, list[str]]:
    """Every string constant in the androidctl modules, keyed by file."""
    found: dict[str, list[str]] = {}
    for path in sorted((REPO / "androidctl").glob("*.py")):
        tree = ast.parse(path.read_text())
        found[str(path)] = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
    return found


def test_never_emits_a_destructive_pm_command():
    # Removing a package or wiping its data is irreversible on a preload that
    # lives in /data/app, so the source may not even mention those commands.
    banned = re.compile(r"\buninstall\b|\bclear\b")
    saw_command = False
    for path, literals in module_literals().items():
        for literal in literals:
            if literal.startswith("pm "):
                saw_command = True
            if banned.search(literal) and "pm" in literal:
                pytest.fail(f"{path} emits a banned command: {literal!r}")
    assert saw_command, "expected the toolchain to build pm commands"


def test_only_disable_and_enable_verbs_are_built():
    banned = {"uninstall", "clear", "hide", "block", "suspend"}
    for path, literals in module_literals().items():
        if not any("pm" in literal.split() for literal in literals):
            continue
        for literal in literals:
            assert literal not in banned, f"{path} references banned verb {literal!r}"


def test_no_vendor_package_ids_are_hardcoded():
    # Selection is per device and per person; a baked-in list is a regression.
    vendor = re.compile(r"^com\.(samsung|sec|google|microsoft|android)\.[\w.]+$")
    for path, literals in module_literals().items():
        for literal in literals:
            assert not vendor.fullmatch(literal), f"{path} hardcodes {literal!r}"
