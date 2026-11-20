"""The host side of adb: one phone, one command per call.

Commands reach the device through adbutils, which speaks adb-server's socket
protocol instead of spawning a process per call. Where the device advertises
`shell_v2` (this phone does), the exit code and the two streams survive: a
refused command is data, not an exception, because its message is the useful
part.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import adbutils

# A wedged adbd must fail instead of hanging a command forever. The slowest
# normal call is one `dumpsys package`, about 1.2 seconds.
SHELL_TIMEOUT = 120


class AdbError(RuntimeError):
    """adb could not run, or the device answered with a non-zero exit."""


@dataclass(frozen=True)
class ShellResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Device(Protocol):
    """What a command needs from a device: a serial and three calls.

    `Adb` is the real one. Tests pass a fake with the same shape, so every
    parser and command builder can run without a phone.
    """

    serial: str

    def run(self, *args: str) -> str: ...

    def shell(self, command: str) -> ShellResult: ...

    def read(self, command: str) -> str: ...


class Adb:
    """One attached device, reached through adbutils.

    The adbutils handle is opened on first use and kept, so one object costs one
    connection for its whole life. Tests replace `_client` and `_devices`.
    """

    def __init__(self, serial: str | None = None, adb: str = "adb"):
        # `adb` was the host binary to spawn. adbutils finds the binary itself
        # (ADBUTILS_ADB_PATH, then PATH), so the parameter is kept only so
        # callers keep their shape.
        self.adb = adb
        self.serial = serial or _only_device()
        self._memo: dict[str, str] = {}
        self._handle: adbutils.AdbDevice | None = None
        self._features: str | None = None

    def run(self, *args: str) -> str:
        """Run adb itself, for commands such as `install`, and return stdout."""
        self._memo = {}
        if len(args) != 3 or args[0] != "install" or args[1] != "-r":
            raise AdbError(f"adb {' '.join(args)}: only `install -r <path>` is supported")
        try:
            # -r replaces the app; nolaunch and silent keep adbutils' own
            # convenience behaviour (starting the app, printing progress) out
            # of what used to be a plain `adb install -r`.
            self._device().install(args[2], nolaunch=True, silent=True, flags=["-r"])
        except (adbutils.AdbError, OSError) as error:
            # A refused install is `pm install` exiting 1 with its diagnosis in
            # the message, which adbutils carries in the exception.
            raise AdbError(f"adb {' '.join(args)}: {error}: exit 1") from error
        return ""

    def shell(self, command: str) -> ShellResult:
        """Run one command on the device and keep its exit code and streams.

        Every write goes through here, so the read cache is dropped: a read
        after a write must see the device, not the earlier value.
        """
        self._memo = {}
        return self._shell(command)

    def _shell(self, command: str) -> ShellResult:
        try:
            device = self._device()
            v2 = self._shell_v2(device)
            result = device.shell2(command, timeout=SHELL_TIMEOUT, v2=v2)
        except (adbutils.AdbError, OSError) as error:
            # -1 means the call never reached the device. Any other non-zero
            # code is the device's own answer.
            return ShellResult(-1, "", str(error))
        if v2:
            return ShellResult(result.returncode, result.stdout, result.stderr)
        # Without shell_v2 the two streams arrive merged in `output`.
        return ShellResult(result.returncode, result.output, "")

    def read(self, command: str) -> str:
        """Run a read-only command once per handle and cache the output.

        One `dumpsys package` is about 118k lines and several parsers read it.
        Every write clears the cache, so a read after a write sees the device.
        """
        if command in self._memo:
            return self._memo[command]
        # Not `shell`: a read must not drop the other cached reads, or an
        # interleaved profile pays for the same dump several times.
        result = self._shell(command)
        if not result.ok:
            raise AdbError(f"{command}: {result.err.strip() or result.out.strip()}")
        self._memo[command] = result.out
        return result.out

    def _device(self) -> adbutils.AdbDevice:
        if self._handle is None:
            self._handle = _client(self.serial)
        return self._handle

    def _shell_v2(self, device: adbutils.AdbDevice) -> bool:
        """Whether the device advertises the protocol that splits the streams."""
        if self._features is None:
            self._features = device.get_features()
        return "shell_v2" in self._features.split(",")


def _devices() -> list[adbutils.AdbDevice]:
    """Every device adb-server reports, whatever its state."""
    return adbutils.adb.device_list()


def _client(serial: str) -> adbutils.AdbDevice:
    """The adbutils handle for one serial."""
    return adbutils.adb.device(serial)


def _only_device() -> str:
    """The serial of the only attached device, or a clear refusal."""
    try:
        attached = [
            device.serial
            for device in _devices()
            if device.serial is not None and device.get_state() == "device"
        ]
    except (adbutils.AdbError, OSError) as error:
        raise AdbError(f"adb devices: {error}") from error
    if len(attached) != 1:
        raise AdbError(f"found {len(attached)}; pass --serial: expected exactly one device")
    return attached[0]
