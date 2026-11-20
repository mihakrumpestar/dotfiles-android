"""The adb wrapper: serial resolution, exit codes, and the read cache.

The transport is adbutils, so the fake stands in at `androidctl.adb._client`
and `androidctl.adb._devices` instead of at `subprocess.run`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from androidctl import adb
from androidctl.adb import Adb, AdbError, ShellResult


@dataclass
class Reply:
    """What adbutils' `shell2` returns, with the streams already decoded."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    output: str = ""
    command: str = ""


class FakeAdbUtils:
    """One adbutils device: the calls the wrapper makes, nothing more."""

    def __init__(
        self,
        *,
        features: str = "shell_v2,cmd,stat_v2",
        reply: Reply | None = None,
        error: Exception | None = None,
        install_error: Exception | None = None,
        serial: str = "SERIAL",
        state: str = "device",
    ) -> None:
        self.serial = serial
        self.state = state
        self.features = features
        self.reply = reply or Reply(stdout="", output="")
        self.error = error
        self.install_error = install_error
        self.shells: list[tuple[str, bool]] = []
        self.installs: list[tuple[str, list[str]]] = []
        self.feature_calls = 0

    def get_state(self) -> str:
        return self.state

    def get_features(self) -> str:
        self.feature_calls += 1
        return self.features

    def shell2(self, command: str, timeout: float = 0, v2: bool = False) -> Reply:
        assert timeout > 0
        self.shells.append((command, v2))
        if self.error is not None:
            raise self.error
        return self.reply

    def install(self, path: str, **kwargs) -> None:
        self.installs.append((path, list(kwargs.get("flags") or [])))
        assert kwargs.get("nolaunch") is True
        assert kwargs.get("silent") is True
        if self.install_error is not None:
            raise self.install_error


def wire(monkeypatch, fake: FakeAdbUtils, devices: list[FakeAdbUtils] | None = None) -> None:
    """Point the wrapper at one fake device, and at the list devices() reports."""
    monkeypatch.setattr(adb, "_client", lambda serial: fake)
    monkeypatch.setattr(adb, "_devices", lambda: devices if devices is not None else [fake])


def test_a_serial_picks_the_only_attached_device(monkeypatch):
    fake = FakeAdbUtils(serial="SERIAL")
    wire(monkeypatch, fake)

    assert Adb().serial == "SERIAL"


def test_no_device_and_several_devices_are_refused(monkeypatch):
    wire(monkeypatch, FakeAdbUtils(), devices=[])
    with pytest.raises(AdbError, match="found 0"):
        Adb()

    two = [FakeAdbUtils(serial="A"), FakeAdbUtils(serial="B")]
    monkeypatch.setattr(adb, "_devices", lambda: two)
    with pytest.raises(AdbError, match="found 2"):
        Adb()


def test_unauthorised_devices_are_not_counted(monkeypatch):
    unauthorised = FakeAdbUtils(serial="SERIAL", state="unauthorized")
    attached = FakeAdbUtils(serial="OTHER", state="device")
    wire(monkeypatch, attached, devices=[unauthorised, attached])

    assert Adb().serial == "OTHER"


def test_a_device_list_failure_is_an_adb_error(monkeypatch):
    def broken() -> list[FakeAdbUtils]:
        raise adb.adbutils.AdbError("server is gone")

    monkeypatch.setattr(adb, "_devices", broken)
    with pytest.raises(AdbError, match="adb devices: server is gone"):
        Adb()


def test_shell_keeps_the_exit_code_and_both_streams(monkeypatch):
    fake = FakeAdbUtils(
        reply=Reply(255, stdout="out\n", stderr="refused\n", output="out\nrefused\n")
    )
    wire(monkeypatch, fake)

    result = Adb("SERIAL").shell("pm disable-user --user 0 com.x")

    assert result == ShellResult(255, "out\n", "refused\n")
    assert not result.ok
    assert fake.shells == [("pm disable-user --user 0 com.x", True)]


def test_shell_falls_back_without_shell_v2(monkeypatch):
    fake = FakeAdbUtils(features="cmd,stat_v2", reply=Reply(1, output="merged\n"))
    wire(monkeypatch, fake)

    result = Adb("SERIAL").shell("true")

    assert result == ShellResult(1, "merged\n", "")
    assert fake.shells == [("true", False)]


def test_features_are_asked_for_once_per_device(monkeypatch):
    fake = FakeAdbUtils()
    wire(monkeypatch, fake)
    device = Adb("SERIAL")

    device.shell("true")
    device.shell("false")

    assert fake.feature_calls == 1
    assert [v2 for _, v2 in fake.shells] == [True, True]


def test_shell_turns_a_transport_failure_into_code_minus_one(monkeypatch):
    fake = FakeAdbUtils(error=adb.adbutils.AdbError("adb is gone"))
    wire(monkeypatch, fake)

    result = Adb("SERIAL").shell("true")

    assert result.code == -1
    assert "adb is gone" in result.err


def test_run_installs_through_the_device_and_reports_a_refusal(monkeypatch):
    fake = FakeAdbUtils(
        install_error=adb.adbutils.AdbInstallError("Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]")
    )
    wire(monkeypatch, fake)

    with pytest.raises(AdbError, match="INSTALL_FAILED_UPDATE_INCOMPATIBLE") as caught:
        Adb("SERIAL").run("install", "-r", "/tmp/app.apk")
    assert str(caught.value).startswith("adb install -r /tmp/app.apk: ")
    assert fake.installs == [("/tmp/app.apk", ["-r"])]


def test_run_install_succeeds_without_output(monkeypatch):
    fake = FakeAdbUtils()
    wire(monkeypatch, fake)

    assert Adb("SERIAL").run("install", "-r", "/tmp/app.apk") == ""
    assert fake.installs == [("/tmp/app.apk", ["-r"])]


def test_run_refuses_a_command_it_does_not_understand(monkeypatch):
    fake = FakeAdbUtils()
    wire(monkeypatch, fake)

    with pytest.raises(AdbError, match="only `install -r <path>` is supported"):
        Adb("SERIAL").run("push", "/tmp/a", "/sdcard/a")
    assert fake.installs == []


def test_the_device_handle_is_created_once_and_lazily(monkeypatch):
    fake = FakeAdbUtils()
    clients = []

    def client(serial: str) -> FakeAdbUtils:
        clients.append(serial)
        return fake

    monkeypatch.setattr(adb, "_client", client)
    device = Adb("SERIAL")
    assert clients == []

    device.shell("true")
    device.shell("false")
    device.read("settings get system x")
    assert clients == ["SERIAL"]


def test_read_is_cached_until_a_write_clears_it(monkeypatch):
    fake = FakeAdbUtils(reply=Reply(stdout="value"))
    wire(monkeypatch, fake)
    device = Adb("SERIAL")

    assert device.read("settings get system x") == "value"
    assert device.read("settings get system x") == "value"
    assert len(fake.shells) == 1

    device.shell("settings put system x y")
    assert device.read("settings get system x") == "value"
    assert len(fake.shells) == 3


def test_read_raises_with_the_device_message(monkeypatch):
    fake = FakeAdbUtils(reply=Reply(1, stderr="no such setting"))
    wire(monkeypatch, fake)

    with pytest.raises(AdbError, match="no such setting"):
        Adb("SERIAL").read("settings get system x")


def test_reads_of_different_commands_share_the_cache(monkeypatch):
    fake = FakeAdbUtils(reply=Reply(stdout="value"))
    wire(monkeypatch, fake)
    device = Adb("SERIAL")

    device.read("a")
    device.read("b")
    device.read("a")
    assert len(fake.shells) == 2
