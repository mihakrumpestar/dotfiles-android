"""The device layer: one screen read, one gesture, one file transfer per call.

uiautomator2 (a Python client for the phone's accessibility service) supplies
the screen tree, the gestures and the file transfer, and runs its server as a
jar through `app_process`, so nothing is installed on the phone. Two of its
calls are avoided: on the Android 17 device this was built against `d.info`
fails and `d.app_current()` takes about 10 seconds. Focus therefore comes from
`dumpsys window` and launch from `am start`, one shell round trip each.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import uiautomator2

from .tree import Node, TreeError, parse_dump

# A wedged adb daemon (adbd, the process on the phone that runs shell commands)
# would hang a backup instead of failing it, so every shell call gets a time
# limit.
SHELL_TIMEOUT = 60


class DeviceError(RuntimeError):
    """A device that is unreachable, or a command that said no."""


@dataclass(frozen=True)
class ShellResult:
    code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


class UiDevice(Protocol):
    """The device surface a recipe run uses.

    `Device` is the real implementation. The tests pass a fake that matches this
    protocol structurally, so the runner runs without a phone and neither side
    inherits from the other.
    """

    def dump(self) -> str: ...

    def tap(self, x: int, y: int) -> None: ...

    def long_press(self, x: int, y: int, millis: int) -> None: ...

    def swipe(self, x1: int, y1: int, x2: int, y2: int, millis: int = 300) -> None: ...

    def key(self, code: str) -> None: ...

    def set_text(self, text: str) -> None: ...

    def clear_text(self) -> None: ...

    def push(self, local: Path, remote: str) -> None: ...

    def pull(self, remote: str, local: Path) -> None: ...

    def shell(
        self, command: str | Sequence[str], timeout: float = SHELL_TIMEOUT
    ) -> ShellResult: ...

    def force_stop(self, package: str) -> None: ...

    def wait_gone(self, package: str, within: float = 3.0) -> None: ...

    def device_clock(self) -> int: ...

    def screen_size(self) -> tuple[int, int]: ...

    def launch(self, component: str, clear: bool = False) -> None: ...

    def unlock(self) -> None: ...

    def focus(self) -> str: ...


class Device:
    """One attached device.

    Pass `client` to drive a stand-in instead of a phone. It must offer `shell`,
    `dump_hierarchy`, `click`, `long_click`, `swipe`, `push`, `pull` and
    `__call__`.
    """

    def __init__(self, serial: str | None = None, client: Any = None):
        self.serial: str | None = serial
        self._client: Any = client
        self._screen: tuple[int, int] | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            # The serial is resolved once, here, and never guessed per call.
            self._client = (
                uiautomator2.connect(self.serial) if self.serial else uiautomator2.connect()
            )
        return self._client

    def shell(self, command: str | Sequence[str], timeout: float = SHELL_TIMEOUT) -> ShellResult:
        try:
            response = self.client.shell(command, timeout=timeout)
        except Exception as error:
            # The transport under uiautomator2 raises many unrelated error types.
            raise DeviceError(f"shell {command}: {error}") from error
        return ShellResult(code=int(response.exit_code), output=response.output)

    def shell_ok(self, command: str | Sequence[str], what: str = "") -> str:
        result = self.shell(command)
        if not result.ok:
            raise DeviceError(f"{what or command}: {first_line(result.output)}")
        return result.output

    # Screen

    def dump(self) -> str:
        """The current screen as a UIAutomator dump: XML, one node per widget."""
        try:
            return self.client.dump_hierarchy(compressed=False)
        except Exception as error:
            raise DeviceError(f"reading the screen: {error}") from error

    def nodes(self) -> list[Node]:
        body = self.dump()
        try:
            return parse_dump(body)
        except TreeError as error:
            raise DeviceError(str(error)) from error

    def focus(self) -> str:
        """The focused window's component, or an empty string when unreadable.

        `dumpsys window` reports which component holds input focus. An empty
        string means the line named no window, which is a state to tolerate, not
        an error.
        """
        result = self.shell("dumpsys window | grep -m1 mCurrentFocus")
        line = first_line(result.output)
        line = line.removesuffix("}")
        opening = line.rfind("Window{")
        if opening < 0:
            return ""
        fields = line[opening + len("Window{") :].split()
        return fields[-1] if fields else ""

    def focused_package(self) -> str:
        focus = self.focus()
        return focus.split("/", 1)[0] if "/" in focus else focus

    def home_package(self) -> str:
        output = self.shell_ok(
            [
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.HOME",
            ]
        )
        component = first_line(output)
        return component.split("/", 1)[0] if "/" in component else component

    def screen_size(self) -> tuple[int, int]:
        """The display size in the current rotation, read once.

        `wm size` reports the physical portrait size, so a swipe on a landscape
        screen would aim off screen. uiautomator2's window size follows rotation
        and costs no shell round trip, so it is tried first.
        """
        if self._screen is not None:
            return self._screen
        try:
            width, height = self.client.window_size()
            size = (int(width), int(height))
        except Exception:  # noqa: BLE001
            # An unreadable window size falls back to `wm size` below.
            size = None
        if size is not None:
            self._screen = size
            return size
        output = self.shell_ok("wm size", "reading the screen size")
        for line in output.splitlines():
            _, sep, size = line.partition(":")
            if not sep or "x" not in size:
                continue
            width, _, height = size.strip().partition("x")
            if width.isdigit() and height.isdigit():
                self._screen = (int(width), int(height))
                return self._screen
        raise DeviceError(f"cannot parse wm size output {output.strip()!r}")

    def device_clock(self) -> int:
        """The phone's own clock in seconds; file timestamps on it use the same base."""
        output = self.shell(["date", "+%s"]).output.strip()
        try:
            return int(output)
        except ValueError as error:
            raise DeviceError(f"reading the device clock: {output!r}") from error

    # Gestures

    def tap(self, x: int, y: int) -> None:
        try:
            self.client.click(x, y)
        except Exception as error:
            raise DeviceError(f"tap at {x},{y}: {error}") from error

    def long_press(self, x: int, y: int, millis: int) -> None:
        try:
            self.client.long_click(x, y, duration=millis / 1000)
        except Exception as error:
            raise DeviceError(f"long press at {x},{y}: {error}") from error

    def swipe(self, x1: int, y1: int, x2: int, y2: int, millis: int = 300) -> None:
        try:
            self.client.swipe(x1, y1, x2, y2, duration=millis / 1000)
        except Exception as error:
            raise DeviceError(f"swipe {x1},{y1} to {x2},{y2}: {error}") from error

    def key(self, code: str) -> None:
        self.shell_ok(["input", "keyevent", code], f"input keyevent {code}")

    def set_text(self, text: str) -> None:
        """Replace the focused field's content.

        `input text` goes through the keyboard into the field the user sees, so
        it is tried first. It cannot carry non-ASCII and appends at the cursor,
        so the field is cleared first. Setting the field through accessibility
        is unreliable behind a keyboard, hence it is the fallback.
        """
        refused = None
        if text.isascii():
            try:
                self._clear_via_keys()
                self.shell_ok(["input", "text", text.replace(" ", "%s")], "input text")
                return
            except DeviceError as error:
                refused = error
        try:
            self.client(focused=True).set_text(text, timeout=2)
        except Exception as error:
            if refused is not None:
                raise DeviceError(f"{refused}; setting the field directly said: {error}") from error
            raise DeviceError(f"setting the field directly said: {error}") from error

    def _clear_via_keys(self) -> None:
        """Empty the focused field from its end, 64 deletes back to the start."""
        self.shell_ok(["input", "keyevent", "KEYCODE_MOVE_END"], "input keyevent")
        # Each key event is a round trip, and the argument list has a limit,
        # so deletes go in one call rather than one call per character.
        self.shell_ok(["input", "keyevent"] + ["KEYCODE_DEL"] * 64, "input keyevent")

    def clear_text(self) -> None:
        try:
            self.client(focused=True).clear_text(timeout=2)
            return
        except Exception as error:  # noqa: BLE001
            # uiautomator2 raises several unrelated error types here.
            refused = error
        try:
            self._clear_via_keys()
        except DeviceError as error:
            raise DeviceError(f"{error}; clearing the field directly said: {refused}") from error

    # Files

    def push(self, local: Any, remote: str) -> None:
        try:
            self.client.push(str(local), remote)
        except Exception as error:
            raise DeviceError(f"push {local} to {remote}: {error}") from error

    def pull(self, remote: str, local: Any) -> None:
        try:
            self.client.pull(remote, str(local))
        except Exception as error:
            raise DeviceError(f"pull {remote} to {local}: {error}") from error

    # Lifecycle

    def force_stop(self, package: str) -> None:
        self.shell(["am", "force-stop", package])

    def start(self, component: str, clear: bool = False) -> None:
        command = ["am", "start"] + (["-S"] if clear else []) + ["-n", component]
        result = self.shell(command)
        if not result.ok:
            raise DeviceError(f"am start {component}: {first_line(result.output)}")

    def launch(self, component: str, clear: bool = False) -> None:
        """Start a component, then bring its window forward.

        A file picker or dialog left open by an earlier run can keep the app
        behind it, and `am start` still exits zero. So this backs out of foreign
        windows and starts again. The second wait is best effort; the recipe's
        own `wait` step is the real gate on the right screen.
        """
        package = component.split("/", 1)[0]
        if clear:
            self.force_stop(package)
        self.start(component, clear)
        if self.wait_focus(package, 2.0):
            return
        self.close_foreign(package)
        self.start(component, clear)
        self.wait_focus(package, 6.0)

    def wait_focus(self, package: str, within: float) -> bool:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if self.focus().startswith(package + "/"):
                return True
            time.sleep(0.15)
        return False

    def close_foreign(self, package: str) -> None:
        """Back out of a window another app left on top, up to three times."""
        try:
            home = self.home_package()
        except DeviceError:
            # A phone whose HOME cannot be resolved has no home to protect.
            home = ""
        for _ in range(3):
            try:
                focus = self.focused_package()
            except DeviceError:
                return
            if not focus or focus == package or (home and focus == home):
                return
            self.shell(["input", "keyevent", "KEYCODE_BACK"])
            time.sleep(0.4)

    def wait_gone(self, package: str, within: float = 3.0) -> None:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            result = self.shell(["pidof", package])
            if not result.ok or not result.output.strip():
                return
            time.sleep(0.1)

    # Unlocking

    def awake(self) -> bool:
        result = self.shell("dumpsys power | grep -m1 mWakefulness=")
        return "mWakefulness=Awake" in result.output

    def wake(self) -> None:
        for _ in range(30):
            if self.awake():
                return
            self.shell(["input", "keyevent", "224"])
            time.sleep(0.1)
        raise DeviceError("the screen did not turn on; wake the device and re-run")

    def keyguard_showing(self) -> bool:
        result = self.shell("dumpsys window | grep -m1 isKeyguardShowing")
        if not result.ok:
            raise DeviceError(f"reading keyguard state: {first_line(result.output)}")
        return "isKeyguardShowing=true" in result.output

    def secure_locked(self) -> bool:
        """Whether the keyguard wants a credential, which a swipe cannot supply.

        Read from the tree the running server already serves: a second
        `uiautomator dump` process would fight the first for the accessibility
        connection.
        """
        try:
            nodes = self.nodes()
        except DeviceError:
            return False
        return any("passwordEntry" in node.resource_id for node in nodes)

    def unlock(self) -> None:
        """Wake the panel and dismiss the keyguard, or say why it cannot.

        This layer never sends the power key to blank a screen, because it may
        not lock a device. `wm dismiss-keyguard` is not used either, because
        this ROM ignores it.
        """
        self.wake()
        if not self.keyguard_showing():
            return
        width, height = self.screen_size()
        x, near, far = width // 2, height * 3 // 4, height // 4
        # The swipe is dropped while the panel is still ramping up, so each
        # attempt wakes, swipes, then re-reads the flag instead of assuming
        # either landed.
        for _ in range(4):
            self.shell(["input", "keyevent", "224"])
            time.sleep(0.3)
            self.shell(["input", "swipe", str(x), str(near), str(x), str(far), "200"])
            for _ in range(12):
                if not self.keyguard_showing():
                    return
                time.sleep(0.1)
        if self.secure_locked():
            raise DeviceError("the device has a secure lock screen; unlock it by hand and re-run")
        raise DeviceError("keyguard is still showing after 4 attempts")
