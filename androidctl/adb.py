"""The host side of adb: one phone, one command per call.

Every device command is passed to `adb -s <serial> shell` as one string. The
device's own shell parses that string, and its exit code plus stderr are the
only evidence of what happened. A refused command is data, not an exception:
the message is the useful part.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

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
    """One attached device, reached through the adb host binary."""

    def __init__(self, serial: str | None = None, adb: str = "adb"):
        self.adb = adb
        self.serial = serial or _only_device(adb)
        self._memo: dict[str, str] = {}

    def run(self, *args: str) -> str:
        """Run adb itself, for commands such as `install`, and return stdout."""
        self._memo = {}
        try:
            result = self._invoke(args)
        except (OSError, subprocess.SubprocessError) as error:
            raise AdbError(f"adb {' '.join(args)}: {error}") from error
        if result.returncode != 0:
            # `adb install` writes its diagnosis to stdout, so both streams are
            # kept.
            detail = result.stderr.strip() or result.stdout.strip()
            raise AdbError(f"adb {' '.join(args)}: {detail}: exit {result.returncode}")
        return result.stdout

    def shell(self, command: str) -> ShellResult:
        """Run one command on the device and keep its exit code and streams.

        Every write goes through here, so the read cache is dropped: a read
        after a write must see the device, not the earlier value.
        """
        self._memo = {}
        return self._shell(command)

    def _shell(self, command: str) -> ShellResult:
        try:
            result = self._invoke(("shell", command))
        except (OSError, subprocess.SubprocessError) as error:
            # -1 means the call never reached the device. Any other non-zero
            # code is the device's own answer.
            return ShellResult(-1, "", str(error))
        return ShellResult(result.returncode, result.stdout, result.stderr)

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
            raise AdbError(f"{command}: {result.err.strip()}")
        self._memo[command] = result.out
        return result.out

    def _invoke(self, args: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.adb, "-s", self.serial, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SHELL_TIMEOUT,
            check=False,
            # adb shell forwards its stdin to the device. A piped package list
            # must not be eaten by the first command.
            stdin=subprocess.DEVNULL,
        )


def _only_device(adb: str) -> str:
    """The serial of the only attached device, or a clear refusal."""
    try:
        result = subprocess.run(
            [adb, "devices"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AdbError(f"adb devices: {error}") from error
    if result.returncode != 0:
        raise AdbError(f"adb devices: {result.stderr.strip()}")
    attached = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            attached.append(fields[0])
    if len(attached) != 1:
        raise AdbError(f"found {len(attached)}; pass --serial: expected exactly one device")
    return attached[0]
