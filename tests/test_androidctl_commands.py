"""The command layer: the markers, ordering and refusals it prints.

These use a fake device keyed by the exact command string, and a fake store for
the profile commands, so the output and the safety orderings are pinned without
a phone.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from androidctl import apps, cli, commands, profile, ratings, snapshots
from androidctl.adb import ShellResult


class FakeAdb:
    """A device whose replies are keyed by the exact command string."""

    serial = "SERIAL"

    def __init__(
        self,
        reads: dict[str, str] | None = None,
        shells: Mapping[str, ShellResult | list[ShellResult]] | None = None,
    ):
        self.reads = dict(reads or {})
        self.shells = dict(shells or {})
        self.calls: list[tuple[str, str]] = []
        self.runs: list[tuple[str, ...]] = []

    def read(self, command: str) -> str:
        self.calls.append(("read", command))
        return self.reads[command]

    def shell(self, command: str) -> ShellResult:
        self.calls.append(("shell", command))
        reply = self.shells.get(command, ShellResult(0, "", ""))
        if isinstance(reply, list):
            # A sequence models one command that changes its answer, like pidof.
            return reply.pop(0) if len(reply) > 1 else reply[0]
        return reply

    def run(self, *args: str) -> str:
        self.runs.append(args)
        return ""


def states_reads(*, enabled=("com.a", "com.b"), disabled=("com.c",)):
    return {
        "pm list packages -e": "".join(f"package:{name}\n" for name in enabled),
        "pm list packages -d": "".join(f"package:{name}\n" for name in disabled),
        "pm list packages --user 0": "".join(f"package:{name}\n" for name in (*enabled, *disabled)),
    }


def facts_reads():
    return {
        "dumpsys package": (
            "Packages:\n"
            "  Package [com.a] (2f3a1b):\n"
            "    userId=10123\n"
            "    sharedUser=SharedUserSetting{abc android.uid.system/1000}\n"
            "    targetSdk=37\n"
            "  Package [com.b] (3a4b5c):\n"
            "    targetSdk=30\n"
        ),
        "pm list packages -f": (
            "package:/system/priv-app/A/A.apk=com.a\npackage:/data/app/~~x==/B/base.apk=com.b\n"
        ),
    }


def roles_shells(holder: str = "com.launcher") -> dict[str, ShellResult]:
    replies = {
        f"cmd role get-role-holders android.app.role.{role}": ShellResult(0, "", "")
        for role in ("HOME", "DIALER", "SMS", "BROWSER", "ASSISTANT", "EMERGENCY", "WALLET")
    }
    replies["cmd role get-role-holders android.app.role.HOME"] = ShellResult(0, holder + "\n", "")
    return replies


def report_device() -> FakeAdb:
    return FakeAdb({**states_reads(), **facts_reads()}, roles_shells())


@pytest.fixture(autouse=True)
def offline_ratings(monkeypatch):
    monkeypatch.setattr(
        ratings,
        "load_ratings",
        lambda state: ratings.Ratings({}, "cache", "offline"),
    )


def test_report_prints_one_sorted_row_per_package_with_dashes_for_missing_facts():
    out = io.StringIO()
    assert commands.report(report_device(), out=out) == 0
    text = out.getvalue()

    assert "# SERIAL: 3 shown, 2 enabled" in text
    assert "# ratings: cache (offline) (main)" in text
    rows = [line for line in text.splitlines() if line.startswith(("enabled", "disabled"))]
    assert [line.split()[-1] for line in rows] == ["com.a", "com.b", "com.c"]
    # com.a has a shared uid and a tier, com.b has neither a shared uid nor a
    # rating, so both get a dash in the columns that have nothing to say.
    assert "android.uid.system" in rows[0]
    assert "-" in rows[1].split()


def test_report_enabled_only_filters_rows_but_counts_all_enabled():
    out = io.StringIO()
    commands.report(report_device(), enabled_only=True, out=out)
    rows = [
        line for line in out.getvalue().splitlines() if line.startswith(("enabled", "disabled"))
    ]
    assert [line.split()[-1] for line in rows] == ["com.a", "com.b"]


def test_report_json_uses_the_agent_field_names_and_omits_empty_ones():
    out = io.StringIO()
    commands.report(report_device(), as_json=True, out=out)
    rows = json.loads(out.getvalue())

    assert [row["pkg"] for row in rows] == ["com.a", "com.b", "com.c"]
    assert rows[0]["shared_user"] == "android.uid.system"
    assert rows[0]["target_sdk"] == 37
    assert rows[0]["tier"] == "system/priv-app"
    assert "shared_user" not in rows[1]
    assert rows[1]["target_sdk"] == 30
    assert "rating" not in rows[2]


def test_health_reports_each_check_and_fails_on_a_problem():
    device = FakeAdb(
        states_reads(enabled=("com.a",), disabled=("com.c",)),
        {
            **roles_shells("com.c"),
            "cmd package query-activities --components -a android.intent.action.MAIN -c android.intent.category.HOME": ShellResult(
                0, "com.a/.Main\n", ""
            ),
            "settings get secure default_input_method": ShellResult(0, "com.c/.Ime\n", ""),
            "logcat -d -b crash": ShellResult(0, "FATAL EXCEPTION\n", ""),
            "getprop sys.boot_completed": ShellResult(0, "1\n", ""),
        },
    )
    out = io.StringIO()
    with pytest.raises(commands.CommandError, match="health check failed"):
        commands.health(device, out=out)
    text = out.getvalue()
    assert "launcher   com.a" in text
    assert "keyboard   com.c" in text
    assert "crashes    1" in text
    assert "default keyboard com.c is disabled" in text
    assert "HOME role is held by disabled com.c" in text


def test_health_says_ok_when_every_check_passes():
    device = FakeAdb(
        states_reads(enabled=("com.a", "com.b"), disabled=()),
        {
            **roles_shells("com.a"),
            "cmd package query-activities --components -a android.intent.action.MAIN -c android.intent.category.HOME": ShellResult(
                0, "com.a/.Main\n", ""
            ),
            "settings get secure default_input_method": ShellResult(0, "com.b/.Ime\n", ""),
            "getprop sys.boot_completed": ShellResult(0, "1\n", ""),
        },
    )
    out = io.StringIO()
    assert commands.health(device, out=out) == 0
    assert out.getvalue().rstrip().endswith("ok")


def test_disable_dry_run_shows_absent_skipped_and_would_without_writing_or_snapshotting(tmp_path):
    device = report_device()
    out = io.StringIO()
    assert (
        commands.disable(device, ["com.a", "com.c", "com.missing"], tmp_path, dry_run=True, out=out)
        == 0
    )
    text = out.getvalue()
    assert "absent    com.missing  not installed" in text
    assert "skipped   com.c  already disabled" in text
    assert "would     pm disable-user --user 0 com.a  [shares android.uid.system]" in text
    assert "snapshot:" not in text
    assert not (tmp_path / "snapshots").exists()


def test_disable_snapshots_before_writing_and_reports_the_device_message(tmp_path):
    device = report_device()
    device.shells["pm disable-user --user 0 com.a"] = ShellResult(255, "", "Failure [not allowed]")
    out = io.StringIO()

    with pytest.raises(commands.CommandError, match="1 of 1 did not disable"):
        commands.disable(device, ["com.a"], tmp_path, out=out)

    text = out.getvalue()
    assert text.startswith("snapshot: ")
    assert "FAILED    com.a  Failure [not allowed]" in text
    snapshots_seen = list((tmp_path / "snapshots").glob("*.json"))
    assert len(snapshots_seen) == 1


def test_disable_refuses_a_bad_package_before_touching_the_device(tmp_path):
    device = FakeAdb()
    with pytest.raises(commands.CommandError, match="not a package name"):
        commands.disable(device, ["com.a;rm -rf /"], tmp_path)
    assert device.calls == []


def test_package_restore_enables_only_what_the_snapshot_saw_enabled_and_the_device_disabled(
    tmp_path,
):
    path = snapshots.write_package_snapshot(
        tmp_path, "SERIAL", {"a": "enabled", "b": "enabled", "c": "disabled"}
    )
    device = FakeAdb(states_reads(enabled=("b",), disabled=("a", "c")))
    out = io.StringIO()
    assert commands.restore_packages(device, tmp_path, dry_run=True, out=out) == 0
    text = out.getvalue()
    assert f"snapshot {path.name}" in text
    assert "would     pm enable --user 0 a" in text
    assert "pm enable --user 0 b" not in text
    assert "pm enable --user 0 c" not in text


def test_package_restore_refuses_another_devices_snapshot(tmp_path):
    path = snapshots.write_package_snapshot(tmp_path, "OTHER", {"a": "enabled"})
    with pytest.raises(commands.CommandError, match="snapshot is for OTHER"):
        commands.restore_packages(FakeAdb(), tmp_path, from_path=path)


class FakeStore:
    """A profile store that keeps its values in memory."""

    def __init__(self, values: dict[str, str] | None = None, states=None):
        self.values = dict(values or {})
        self.states = dict(states or {})
        self.writes: list[tuple[str, str]] = []
        self.resets: list[str] = []

    def read(self, device, entry) -> tuple[str, profile.State]:
        key = profile.key(entry)
        return self.values.get(key, ""), self.states.get(key, profile.State.UNSET)

    def write(self, device, entry) -> tuple[bool, str]:
        key = profile.key(entry)
        self.values[key] = entry.value
        self.states[key] = profile.State.EXPLICIT
        self.writes.append((key, entry.value))
        return True, ""

    def reset(self, device, entry) -> tuple[bool, str]:
        key = profile.key(entry)
        self.values.pop(key, None)
        self.states[key] = profile.State.UNSET
        self.resets.append(key)
        return True, ""


def profile_file(tmp_path: Path) -> Path:
    path = tmp_path / "profile.conf"
    path.write_text('[system]\na = "1"   # first\nb = "2"   # second\n')
    return path


def patch_store(monkeypatch, store: FakeStore):
    monkeypatch.setattr(profile, "store_for", lambda entry: store)
    monkeypatch.setattr(profile, "section_store", lambda namespace: store)


def test_apply_dry_run_marks_same_and_would_and_writes_nothing(tmp_path, monkeypatch):
    store = FakeStore({"system/a": "1"}, {"system/a": profile.State.EXPLICIT})
    patch_store(monkeypatch, store)
    device = FakeAdb({"pm list packages --user 0": ""})
    out = io.StringIO()

    assert commands.apply(device, [str(profile_file(tmp_path))], tmp_path, out=out) == 0

    text = out.getvalue()
    assert "  same   system/a = 1  (first)" in text
    assert "  would  system/b = 2  (second)" in text
    assert "dry run; re-run with --yes to write" in text
    assert store.writes == []


def test_apply_yes_snapshots_only_the_changed_entries_and_reads_them_back(tmp_path, monkeypatch):
    store = FakeStore({"system/a": "1"}, {"system/a": profile.State.EXPLICIT})
    patch_store(monkeypatch, store)
    device = FakeAdb({"pm list packages --user 0": ""})
    out = io.StringIO()

    assert commands.apply(device, [str(profile_file(tmp_path))], tmp_path, yes=True, out=out) == 0

    assert store.writes == [("system/b", "2")]
    snapshot = snapshots.read_profile_snapshot(next((tmp_path / "profile").glob("*.json")))
    assert set(snapshot.before()) == {"system/b"}
    assert "applied 1 entries" in out.getvalue()


def test_apply_with_yes_and_nothing_to_do_skips_the_snapshot(tmp_path, monkeypatch):
    store = FakeStore(
        {"system/a": "1", "system/b": "2"},
        {"system/a": profile.State.EXPLICIT, "system/b": profile.State.EXPLICIT},
    )
    patch_store(monkeypatch, store)
    out = io.StringIO()
    assert (
        commands.apply(
            FakeAdb({"pm list packages --user 0": ""}),
            [str(profile_file(tmp_path))],
            tmp_path,
            yes=True,
            out=out,
        )
        == 0
    )
    assert "nothing to do" in out.getvalue()
    assert not (tmp_path / "profile").exists()


def test_verify_reports_ok_unset_and_drift(tmp_path, monkeypatch):
    store = FakeStore(
        {"system/a": "1"},
        {"system/a": profile.State.EXPLICIT, "system/b": profile.State.UNSET},
    )
    patch_store(monkeypatch, store)
    out = io.StringIO()
    with pytest.raises(commands.CommandError, match="1 entr"):
        commands.verify(
            FakeAdb({"pm list packages --user 0": ""}),
            [str(profile_file(tmp_path))],
            out=out,
        )
    text = out.getvalue()
    assert "  ok      system/a = 1" in text
    assert '  unset   system/b (want "2")' in text


def test_load_profiles_refuses_a_key_declared_twice(tmp_path):
    first = tmp_path / "one.conf"
    second = tmp_path / "two.conf"
    first.write_text('[system]\na = "1"   # one\n')
    second.write_text('[system]\na = "2"   # two\n')
    with pytest.raises(commands.CommandError, match="also declared"):
        commands.load_profiles([str(first), str(second)])


def test_revert_writes_explicit_values_resets_implicit_and_unsets_missing(tmp_path, monkeypatch):
    store = FakeStore(
        {"system/a": "9", "system/b": "9", "system/c": "9"},
        {
            "system/a": profile.State.EXPLICIT,
            "system/b": profile.State.IMPLICIT,
            "system/c": profile.State.EXPLICIT,
        },
    )
    patch_store(monkeypatch, store)
    path = snapshots.write_profile_snapshot(
        tmp_path,
        "SERIAL",
        {
            "system/a": profile.SnapshotEntry(profile.State.EXPLICIT, "1"),
            "system/b": profile.SnapshotEntry(profile.State.IMPLICIT, "5"),
            "system/c": profile.SnapshotEntry(profile.State.UNSET),
        },
    )
    out = io.StringIO()

    assert commands.revert(FakeAdb(), tmp_path, from_path=path, out=out) == 0

    text = out.getvalue()
    assert "  restore system/a = 1" in text
    assert "  reset   system/b (was 5)" in text
    assert "  unset   system/c" in text
    assert "reverted 3 entr(ies)" in text
    assert store.writes == [("system/a", "1")]


def app_spec() -> apps.AppSpec:
    return apps.AppSpec(
        name="demo",
        package="com.example.demo",
        release="https://example.invalid/release",
        assets={"arm64-v8a": "app-arm64-v8a-release.apk", "universal": "app-release.apk"},
        checksum="asset",
    )


def shizuku_spec() -> apps.AppSpec:
    return apps.AppSpec(
        name="shizuku",
        package="moe.shizuku.privileged.api",
        release="https://example.invalid/release",
        assets={"universal": "shizuku-next.apk"},
        checksum="digest",
        start=apps.StartSpec("libshizuku.so", "shizuku_server"),
    )


def test_install_dry_run_verifies_the_asset_without_downloading(monkeypatch, tmp_path):
    asset = apps.Asset("app-arm64-v8a-release.apk", "https://x/app.apk", 10)
    checksum = apps.Asset("app-arm64-v8a-release.apk.sha256", "https://x/app.apk.sha256", 64)
    release = apps.Release("v1.2.3", (asset, checksum))
    monkeypatch.setattr(commands.apps, "latest_release", lambda spec: release)
    monkeypatch.setattr(commands.apps, "expected_hash", lambda spec, release, apk: "a" * 64)
    device = FakeAdb(
        shells={"getprop ro.product.cpu.abilist": ShellResult(0, "arm64-v8a,armeabi-v7a\n", "")}
    )
    out = io.StringIO()

    assert commands.install_app(device, app_spec(), tmp_path, dry_run=True, out=out) == 0

    text = out.getvalue()
    assert "v1.2.3 app-arm64-v8a-release.apk" in text
    assert "sha256 " + "a" * 64 in text
    assert "dry run" in text
    assert device.runs == []


def test_disable_actual_prints_the_device_message_and_succeeds(tmp_path):
    device = report_device()
    device.shells["pm disable-user --user 0 com.a"] = ShellResult(
        0, "Package com.a new state: disabled", ""
    )
    out = io.StringIO()
    assert commands.disable(device, ["com.a"], tmp_path, out=out) == 0
    assert "ok        com.a  Package com.a new state: disabled" in out.getvalue()


def test_package_restore_actual_enables_and_reports_failures(tmp_path):
    snapshots.write_package_snapshot(tmp_path, "SERIAL", {"a": "enabled", "b": "enabled"})
    device = FakeAdb(states_reads(enabled=(), disabled=("a", "b")))
    device.shells["pm enable --user 0 a"] = ShellResult(0, "Package a new state: enabled", "")
    device.shells["pm enable --user 0 b"] = ShellResult(255, "", "Failure [not allowed]")
    out = io.StringIO()

    with pytest.raises(commands.CommandError, match="1 of 2 did not re-enable"):
        commands.restore_packages(device, tmp_path, out=out)

    text = out.getvalue()
    assert "ok        a  Package a new state: enabled" in text
    assert "FAILED    b  Failure [not allowed]" in text


class IgnoringStore(FakeStore):
    """A store whose write is accepted and then ignored, the failure readback exists for."""

    def read(self, device, entry) -> tuple[str, profile.State]:
        key = profile.key(entry)
        if key == "system/a":
            return "1", profile.State.EXPLICIT
        return "9", profile.State.EXPLICIT


def test_apply_reports_a_write_the_platform_ignored(tmp_path, monkeypatch):
    store = IgnoringStore()
    patch_store(monkeypatch, store)
    out = io.StringIO()
    err = io.StringIO()

    with pytest.raises(commands.CommandError, match="1 of 1 writes did not take effect"):
        commands.apply(
            FakeAdb({"pm list packages --user 0": ""}),
            [str(profile_file(tmp_path))],
            tmp_path,
            yes=True,
            out=out,
            err=err,
        )

    assert "IGNORED system/b" in err.getvalue()


def test_revert_reports_a_failed_write(tmp_path, monkeypatch):
    class RefusingStore(FakeStore):
        def write(self, device, entry) -> tuple[bool, str]:
            return False, "read-only store"

    store = RefusingStore()
    patch_store(monkeypatch, store)
    path = snapshots.write_profile_snapshot(
        tmp_path, "SERIAL", {"system/a": profile.SnapshotEntry(profile.State.EXPLICIT, "1")}
    )
    err = io.StringIO()
    with pytest.raises(commands.CommandError, match="1 of 1 could not be restored"):
        commands.revert(FakeAdb(), tmp_path, from_path=path, err=err)
    assert "FAILED  system/a  read-only store" in err.getvalue()


def test_install_runs_adb_and_checks_the_package_is_enabled(monkeypatch, tmp_path):
    spec = apps.AppSpec(
        name="two",
        package="com.example.two",
        release="https://example.invalid/release",
        assets={"arm64-v8a": "two-arm64.apk", "universal": "two.apk"},
        checksum="asset",
    )
    asset = apps.Asset("two-arm64.apk", "https://x/two.apk", 10)
    checksum = apps.Asset("two-arm64.apk.sha256", "https://x/two.apk.sha256", 64)
    release = apps.Release("v1.2.3", (asset, checksum))
    apk = tmp_path / "two.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(commands.apps, "latest_release", lambda spec: release)
    monkeypatch.setattr(commands.apps, "expected_hash", lambda spec, release, apk: "a" * 64)
    monkeypatch.setattr(commands.apps, "download", lambda asset, want, where: apk)
    device = FakeAdb(
        {
            "pm list packages -e": "package:com.example.two\n",
            "pm list packages -d": "",
        },
        {"getprop ro.product.cpu.abilist": ShellResult(0, "arm64-v8a\n", "")},
    )
    out = io.StringIO()

    assert commands.install_app(device, spec, tmp_path, out=out) == 0

    assert device.runs == [("install", "-r", str(apk))]
    assert "installed com.example.two" in out.getvalue()


def test_install_refuses_a_successful_install_that_is_not_enabled(monkeypatch, tmp_path):
    asset = apps.Asset("app-arm64-v8a-release.apk", "https://x/app.apk", 10)
    checksum = apps.Asset("app-arm64-v8a-release.apk.sha256", "https://x/app.apk.sha256", 64)
    release = apps.Release("v1.2.3", (asset, checksum))
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(commands.apps, "latest_release", lambda spec: release)
    monkeypatch.setattr(commands.apps, "expected_hash", lambda spec, release, apk: "a" * 64)
    monkeypatch.setattr(commands.apps, "download", lambda asset, want, where: apk)
    device = FakeAdb(
        {"pm list packages -e": "", "pm list packages -d": ""},
        {"getprop ro.product.cpu.abilist": ShellResult(0, "arm64-v8a\n", "")},
    )
    with pytest.raises(commands.CommandError, match="not enabled"):
        commands.install_app(device, app_spec(), tmp_path, out=io.StringIO())


APK_PATH = "/data/app/~~h==/moe.shizuku.privileged.api-x==/base.apk"
STARTER = "/data/app/~~h==/moe.shizuku.privileged.api-x==/lib/arm64/libshizuku.so"
LS_STARTER = "ls /data/app/~~h==/moe.shizuku.privileged.api-x==/lib/arm64/libshizuku.so"


def start_device(*, pidof: ShellResult | list[ShellResult], library: str = "") -> FakeAdb:
    shells: dict[str, ShellResult | list[ShellResult]] = {
        "pidof shizuku_server": pidof,
        "pm path moe.shizuku.privileged.api": ShellResult(0, f"package:{APK_PATH}\n", ""),
        "getprop ro.product.cpu.abilist": ShellResult(0, "arm64-v8a,armeabi-v7a\n", ""),
    }
    if library:
        shells[LS_STARTER] = ShellResult(0, library + "\n", "")
    return FakeAdb(shells=shells)


def test_start_app_runs_the_bundled_starter_and_waits_for_the_process():
    device = start_device(pidof=ShellResult(0, "4242\n", ""), library=STARTER)
    out = io.StringIO()

    commands.start_app(device, shizuku_spec(), out=out)

    assert ("shell", LS_STARTER) in device.calls
    assert ("shell", STARTER) in device.calls
    assert "started   shizuku (shizuku_server)" in out.getvalue()


def test_start_app_replaces_an_already_running_process():
    device = start_device(
        pidof=[ShellResult(0, "15129\n", ""), ShellResult(0, "4242\n", "")],
        library=STARTER,
    )
    out = io.StringIO()

    commands.start_app(device, shizuku_spec(), out=out)

    assert ("shell", STARTER) in device.calls
    assert "started   shizuku (shizuku_server)" in out.getvalue()


def test_start_app_reports_a_missing_library():
    device = start_device(pidof=ShellResult(0, "", ""))
    with pytest.raises(commands.CommandError, match="could not find libshizuku.so"):
        commands.start_app(device, shizuku_spec(), out=io.StringIO())


def test_start_app_times_out_without_sleeping(monkeypatch):
    def no_sleep(seconds):
        raise AssertionError("timeout=0 must fail without sleeping")

    monkeypatch.setattr(commands.time, "sleep", no_sleep)
    device = start_device(pidof=ShellResult(0, "", ""), library=STARTER)
    with pytest.raises(commands.CommandError, match="is not running"):
        commands.start_app(device, shizuku_spec(), out=io.StringIO(), timeout=0)


def test_init_installs_then_starts_each_spec_in_order(monkeypatch):
    calls: list[tuple[str, str]] = []

    def install(device, spec, state, **kwargs):
        calls.append(("install", spec.name))
        return 0

    def start(device, spec, **kwargs):
        calls.append(("start", spec.name))

    monkeypatch.setattr(commands, "install_app", install)
    monkeypatch.setattr(commands, "start_app", start)
    two = apps.AppSpec(
        name="two",
        package="com.example.two",
        release="https://example.invalid/release",
        assets={"universal": "two.apk"},
        checksum="asset",
    )

    assert commands.init_device(FakeAdb(), [app_spec(), two], "state", out=io.StringIO()) == 0

    assert calls == [
        ("install", "demo"),
        ("start", "demo"),
        ("install", "two"),
        ("start", "two"),
    ]


def test_init_dry_run_starts_nothing(monkeypatch):
    started: list[apps.AppSpec] = []
    monkeypatch.setattr(commands, "install_app", lambda device, spec, state, **kwargs: 0)
    monkeypatch.setattr(commands, "start_app", lambda device, spec, **kwargs: started.append(spec))

    assert commands.init_device(FakeAdb(), [app_spec()], "state", dry_run=True) == 0
    assert started == []


def test_the_bootstrap_list_names_apps_that_load():
    packages = cli._packages(commands.INIT_FILE)
    assert packages == ["moe.shizuku.privileged.api", "dev.imranr.obtainium"]
    assert [apps.load(package).name for package in packages] == ["shizuku", "obtainium"]


def test_restore_refuses_a_snapshot_package_name_that_is_not_a_package(tmp_path):
    path = tmp_path / "snapshots" / "20260101T000000Z-SERIAL.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"created": "x", "serial": "SERIAL", "packages": {"com.a;reboot": "enabled"}}')
    device = FakeAdb(states_reads(enabled=(), disabled=("com.a;reboot",)))
    with pytest.raises(commands.CommandError, match="not a package name"):
        commands.restore_packages(device, tmp_path, from_path=path)


def test_apply_counts_a_readback_that_raises_instead_of_aborting(tmp_path, monkeypatch):
    class BrokenReadback(FakeStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.wrote = False

        def write(self, device, entry) -> tuple[bool, str]:
            self.wrote = True
            return super().write(device, entry)

        def read(self, device, entry) -> tuple[str, profile.State]:
            if self.wrote and profile.key(entry) == "system/b":
                raise profile.ProfileError("bad key in the store")
            return super().read(device, entry)

    store = BrokenReadback({"system/a": "1"}, {"system/a": profile.State.EXPLICIT})
    patch_store(monkeypatch, store)
    err = io.StringIO()
    with pytest.raises(commands.CommandError, match="1 of 1 writes did not take effect"):
        commands.apply(
            FakeAdb({"pm list packages --user 0": ""}),
            [str(profile_file(tmp_path))],
            tmp_path,
            yes=True,
            out=io.StringIO(),
            err=err,
        )
    assert "IGNORED system/b  bad key in the store" in err.getvalue()


def test_a_profile_that_is_not_utf8_is_a_clean_error(tmp_path):
    path = tmp_path / "bad.conf"
    path.write_bytes(b'[system]\na = "\xff"   # bad\n')
    with pytest.raises(commands.CommandError, match="not UTF-8"):
        commands.load_profiles([str(path)])


def test_revert_continues_when_a_reset_readback_fails(tmp_path, monkeypatch):
    class BrokenReset(FakeStore):
        def read(self, device, entry) -> tuple[str, profile.State]:
            if profile.key(entry) == "system/a":
                raise profile.ProfileError("device offline")
            return super().read(device, entry)

    store = BrokenReset({"system/b": "9"}, {"system/b": profile.State.EXPLICIT})
    patch_store(monkeypatch, store)
    path = snapshots.write_profile_snapshot(
        tmp_path,
        "SERIAL",
        {
            "system/a": profile.SnapshotEntry(profile.State.IMPLICIT, "5"),
            "system/b": profile.SnapshotEntry(profile.State.EXPLICIT, "2"),
        },
    )
    out = io.StringIO()
    err = io.StringIO()
    with pytest.raises(commands.CommandError, match="1 of 2 could not be restored"):
        commands.revert(FakeAdb(), tmp_path, from_path=path, out=out, err=err)
    assert "FAILED  system/a  device offline" in err.getvalue()
    assert "restore system/b = 2" in out.getvalue()


def test_revert_reports_an_empty_namespace_and_continues(tmp_path, monkeypatch):
    store = FakeStore({"secure/b": "9"}, {"secure/b": profile.State.EXPLICIT})
    monkeypatch.setattr(profile, "store_for", lambda entry: store)
    monkeypatch.setattr(
        profile, "section_store", lambda namespace: None if not namespace else store
    )
    path = snapshots.write_profile_snapshot(
        tmp_path,
        "SERIAL",
        {
            "/secure/a": profile.SnapshotEntry(profile.State.EXPLICIT, "1"),
            "secure/b": profile.SnapshotEntry(profile.State.EXPLICIT, "2"),
        },
    )
    out = io.StringIO()
    err = io.StringIO()
    with pytest.raises(commands.CommandError, match="1 of 2 could not be restored"):
        commands.revert(FakeAdb(), tmp_path, from_path=path, out=out, err=err)
    assert "FAILED  /secure/a  unknown section []" in err.getvalue()
    assert "restore secure/b = 2" in out.getvalue()


def test_a_starter_path_may_hold_the_app_directory_hash_padding():
    assert commands._starter_path(STARTER, "libshizuku.so")


@pytest.mark.parametrize(
    "candidate", ["/data/app/x;reboot/libshizuku.so", "/data/app/../libshizuku.so"]
)
def test_a_starter_path_with_shell_syntax_is_refused(candidate):
    assert not commands._starter_path(candidate, "libshizuku.so")
