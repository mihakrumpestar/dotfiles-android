"""The adb wrapper: serial resolution, exit codes, and the read cache."""

from __future__ import annotations

import subprocess

import pytest

from androidctl import adb
from androidctl.adb import Adb, AdbError, ShellResult


def completed(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


def test_a_serial_picks_the_only_attached_device(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return completed(out="List of devices attached\nSERIAL\tdevice\n")

    monkeypatch.setattr(adb.subprocess, "run", run)
    device = Adb()

    assert device.serial == "SERIAL"
    assert calls == [["adb", "devices"]]


def test_no_device_and_several_devices_are_refused(monkeypatch):
    monkeypatch.setattr(
        adb.subprocess, "run", lambda command, **kwargs: completed(out="List of devices attached\n")
    )
    with pytest.raises(AdbError, match="found 0"):
        Adb()

    monkeypatch.setattr(
        adb.subprocess,
        "run",
        lambda command, **kwargs: completed(out="List of devices attached\nA\tdevice\nB\tdevice\n"),
    )
    with pytest.raises(AdbError, match="found 2"):
        Adb()


def test_unauthorised_devices_are_not_counted(monkeypatch):
    monkeypatch.setattr(
        adb.subprocess,
        "run",
        lambda command, **kwargs: completed(
            out="List of devices attached\nSERIAL\tunauthorized\nOTHER\tdevice\n"
        ),
    )
    assert Adb().serial == "OTHER"


def test_shell_keeps_the_exit_code_and_both_streams(monkeypatch):
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return completed(255, "out", "refused")

    monkeypatch.setattr(adb.subprocess, "run", run)

    result = Adb("SERIAL").shell("pm disable-user --user 0 com.x")

    assert result == ShellResult(255, "out", "refused")
    assert not result.ok
    assert seen == [["adb", "-s", "SERIAL", "shell", "pm disable-user --user 0 com.x"]]


def test_shell_turns_a_transport_failure_into_code_minus_one(monkeypatch):
    def run(command, **kwargs):
        raise OSError("adb is gone")

    monkeypatch.setattr(adb.subprocess, "run", run)

    result = Adb("SERIAL").shell("true")

    assert result.code == -1
    assert "adb is gone" in result.err


def test_run_reports_stderr_and_the_exit_status(monkeypatch):
    monkeypatch.setattr(
        adb.subprocess,
        "run",
        lambda command, **kwargs: completed(1, "", "Failure [INSTALL_FAILED]"),
    )

    with pytest.raises(AdbError, match="INSTALL_FAILED"):
        Adb("SERIAL").run("install", "-r", "/tmp/app.apk")


def test_read_is_cached_until_a_write_clears_it(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return completed(out="value")

    monkeypatch.setattr(adb.subprocess, "run", run)
    device = Adb("SERIAL")

    assert device.read("settings get system x") == "value"
    assert device.read("settings get system x") == "value"
    assert len(calls) == 1

    device.shell("settings put system x y")
    assert device.read("settings get system x") == "value"
    assert len(calls) == 3


def test_read_raises_with_the_device_message(monkeypatch):
    monkeypatch.setattr(
        adb.subprocess, "run", lambda command, **kwargs: completed(1, "", "no such setting")
    )

    with pytest.raises(AdbError, match="no such setting"):
        Adb("SERIAL").read("settings get system x")


def test_reads_of_different_commands_share_the_cache(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return completed(out="value")

    monkeypatch.setattr(adb.subprocess, "run", run)
    device = Adb("SERIAL")

    device.read("a")
    device.read("b")
    device.read("a")
    assert len(calls) == 2
