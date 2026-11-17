"""The device wrapper: reply parsing, the fallbacks, and the abort paths.

The uiautomator2 client is replaced by a stand-in here, so every test decides
exactly what the phone answers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pytest
from support import xml_node, xml_screen

import uiu.device
from uiu.device import Device, DeviceError


@dataclass
class Reply:
    """One shell reply: an exit code and the text the command printed."""

    exit_code: int = 0
    output: str = ""


def answers(*pairs: tuple[str, Reply | str]) -> Callable[[str | Sequence[str]], Reply]:
    """A reply function that returns the first answer whose needle is in the command."""

    def reply(command: str | Sequence[str]) -> Reply:
        text = command if isinstance(command, str) else " ".join(str(part) for part in command)
        for needle, answer in pairs:
            if needle in text:
                return answer if isinstance(answer, Reply) else Reply(output=answer)
        return Reply()

    return reply


class Field:
    """The focused-widget proxy that `client(focused=True)` returns."""

    def __init__(self, client: Client):
        self.client = client

    def set_text(self, text: str, timeout: float | None = None) -> None:
        self.client.field_calls.append(("set_text", text, timeout))
        if self.client.field_error is not None:
            raise self.client.field_error

    def clear_text(self, timeout: float | None = None) -> None:
        self.client.field_calls.append(("clear_text", timeout))
        if self.client.field_error is not None:
            raise self.client.field_error


class Client:
    """The part of the uiautomator2 surface the device layer touches.

    Most calls are recorded in a list, and a test can make any of them raise
    instead of answering.
    """

    def __init__(self, reply: Callable[[str | Sequence[str]], Reply] | None = None):
        self.reply = reply or (lambda command: Reply())
        self.shells: list[tuple[str | Sequence[str], float]] = []
        self.field_calls: list[tuple] = []
        self.field_error: Exception | None = None
        self.hierarchy = xml_screen()
        self.dumps = 0
        self.taps: list[tuple[int, int]] = []
        self.long_clicks: list[tuple[int, int, float]] = []
        self.swipes: list[tuple[int, int, int, int, float]] = []
        self.pushes: list[tuple[str, str]] = []
        self.pulls: list[tuple[str, str]] = []
        self.shell_error: Exception | None = None
        self.window: tuple[int, int] | None = None
        self.window_error: Exception | None = None
        self.window_reads = 0

    def window_size(self) -> tuple[int, int]:
        self.window_reads += 1
        if self.window_error is not None:
            raise self.window_error
        if self.window is None:
            raise RuntimeError("no window size")
        return self.window

    def shell(self, command: str | Sequence[str], timeout: float = 60) -> Reply:
        self.shells.append((command, timeout))
        if self.shell_error is not None:
            raise self.shell_error
        return self.reply(command)

    def __call__(self, **kwargs) -> Field:
        assert kwargs == {"focused": True}, kwargs
        return Field(self)

    def dump_hierarchy(self, compressed: bool = False) -> str:
        self.dumps += 1
        return self.hierarchy

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def long_click(self, x: int, y: int, duration: float = 0.5) -> None:
        self.long_clicks.append((x, y, duration))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self.swipes.append((x1, y1, x2, y2, duration))

    def push(self, local: str, remote: str) -> None:
        self.pushes.append((local, remote))

    def pull(self, remote: str, local: str) -> None:
        self.pulls.append((remote, local))


@pytest.fixture
def fast_time(monkeypatch):
    """No real waiting: sleep returns at once, and each monotonic read advances the clock."""

    class Clock:
        now = 0.0
        slept = 0.0

        @classmethod
        def sleep(cls, seconds: float) -> None:
            cls.slept += seconds

        @classmethod
        def monotonic(cls) -> float:
            cls.now += 0.5
            return cls.now

    monkeypatch.setattr(uiu.device.time, "sleep", Clock.sleep)
    monkeypatch.setattr(uiu.device.time, "monotonic", Clock.monotonic)
    return Clock


def list_shells(client: Client) -> list[list[str]]:
    return [list(command) for command, _ in client.shells if isinstance(command, list)]


# Shell


def test_shell_turns_a_transport_error_into_a_device_error():
    client = Client()
    client.shell_error = OSError("device offline")
    device = Device(client=client)

    with pytest.raises(DeviceError, match="device offline"):
        device.shell(["date", "+%s"])

    assert client.shells == [(["date", "+%s"], 60)]


# Focus


def test_focus_reads_the_component_out_of_the_window_line():
    client = Client(answers(("mCurrentFocus", "mCurrentFocus=Window{abc u0 pkg/pkg.MainActivity}")))
    assert Device(client=client).focus() == "pkg/pkg.MainActivity"


def test_focus_is_empty_when_the_line_has_no_window():
    client = Client(answers(("mCurrentFocus", "mCurrentFocus=null")))
    assert Device(client=client).focus() == ""


# Screen size


def test_screen_size_prefers_the_rotated_window_size_and_never_shells_out():
    client = Client(answers(("wm size", "Physical size: 1080x2340\n")))
    client.window = (2340, 1080)
    assert Device(client=client).screen_size() == (2340, 1080)
    assert client.shells == []


def test_screen_size_falls_back_to_wm_size_when_the_window_size_raises():
    client = Client(answers(("wm size", "Physical size: 1080x2340\n")))
    client.window_error = RuntimeError("no display yet")
    assert Device(client=client).screen_size() == (1080, 2340)


def test_screen_size_reads_the_physical_size_before_an_override():
    client = Client(answers(("wm size", "Physical size: 1080x2340\n")))
    assert Device(client=client).screen_size() == (1080, 2340)


def test_screen_size_falls_back_to_the_override_line():
    client = Client(answers(("wm size", "Override size: 720x1560\n")))
    assert Device(client=client).screen_size() == (720, 1560)


def test_screen_size_refuses_garbage_from_both_reads():
    client = Client(answers(("wm size", "something else\n")))
    client.window_error = RuntimeError("no display yet")
    with pytest.raises(DeviceError, match="cannot parse wm size"):
        Device(client=client).screen_size()


def test_screen_size_is_read_once():
    client = Client()
    client.window = (2340, 1080)
    device = Device(client=client)

    device.screen_size()
    device.screen_size()

    assert client.window_reads == 1
    assert client.shells == []


# Clock


def test_device_clock_returns_the_device_epoch():
    client = Client(answers(("date", "1712345678\n")))
    assert Device(client=client).device_clock() == 1712345678


def test_device_clock_refuses_an_unreadable_date():
    client = Client(answers(("date", "not-a-number\n")))
    with pytest.raises(DeviceError, match="reading the device clock: 'not-a-number'"):
        Device(client=client).device_clock()


# Text entry


def test_set_text_clears_then_uses_input_text_for_ascii():
    client = Client()
    Device(client=client).set_text("a b")

    assert list_shells(client) == [
        ["input", "keyevent", "KEYCODE_MOVE_END"],
        ["input", "keyevent"] + ["KEYCODE_DEL"] * 64,
        ["input", "text", "a%sb"],
    ]
    assert client.field_calls == []


def test_set_text_uses_the_focused_field_for_non_ascii():
    client = Client()
    Device(client=client).set_text("é")

    assert client.field_calls == [("set_text", "é", 2)]
    assert client.shells == []


def test_set_text_falls_back_to_the_focused_field_when_input_text_fails():
    client = Client(answers(("input text", Reply(1, "Error: not a text field"))))
    Device(client=client).set_text("a b")

    assert client.field_calls == [("set_text", "a b", 2)]
    assert list_shells(client)[-1] == ["input", "text", "a%sb"]


def test_set_text_reports_both_causes_when_both_routes_fail():
    client = Client(answers(("input text", Reply(1, "Error: nothing is focused"))))
    client.field_error = RuntimeError("ACTION_SET_TEXT refused")
    device = Device(client=client)

    with pytest.raises(DeviceError) as caught:
        device.set_text("a b")

    message = str(caught.value)
    assert "ACTION_SET_TEXT refused" in message
    assert "nothing is focused" in message


def test_clear_text_falls_back_to_move_end_and_one_delete_burst():
    client = Client()
    client.field_error = RuntimeError("no field is focused")
    Device(client=client).clear_text()

    assert client.field_calls == [("clear_text", 2)]
    assert list_shells(client) == [
        ["input", "keyevent", "KEYCODE_MOVE_END"],
        ["input", "keyevent"] + ["KEYCODE_DEL"] * 64,
    ]


# Lifecycle


def test_close_foreign_treats_an_unresolvable_home_and_empty_focus_as_nothing_to_do():
    client = Client(
        answers(
            ("resolve-activity", Reply(1, "Error: Activity not found")),
            ("mCurrentFocus", "mCurrentFocus=null"),
        )
    )

    Device(client=client).close_foreign("com.example.demo")

    assert ["input", "keyevent", "KEYCODE_BACK"] not in list_shells(client)


def test_launch_starts_the_component_even_when_focus_never_matches(fast_time):
    client = Client(answers(("mCurrentFocus", "mCurrentFocus=Window{abc u0 com.other/.Main}")))
    device = Device(client=client)

    device.launch("com.example.demo/.MainActivity")

    starts = [command for command in list_shells(client) if command[:2] == ["am", "start"]]
    assert starts == [
        ["am", "start", "-n", "com.example.demo/.MainActivity"],
        ["am", "start", "-n", "com.example.demo/.MainActivity"],
    ]
    assert fast_time.slept > 0


# Unlocking


def test_unlock_returns_after_waking_when_no_keyguard_is_showing():
    client = Client(
        answers(
            ("mWakefulness=", "mWakefulness=Awake"),
            ("isKeyguardShowing", "isKeyguardShowing=false"),
        )
    )

    Device(client=client).unlock()

    assert not [command for command in list_shells(client) if command[:2] == ["input", "swipe"]]


def test_unlock_refuses_a_secure_lock_screen(fast_time):
    client = Client(
        answers(
            ("mWakefulness=", "mWakefulness=Awake"),
            ("isKeyguardShowing", "isKeyguardShowing=true"),
            ("wm size", "Physical size: 1080x2340\n"),
        )
    )
    client.hierarchy = xml_screen(xml_node(id="com.android.systemui:id/passwordEntry"))
    device = Device(client=client)

    with pytest.raises(DeviceError, match="unlock it by hand"):
        device.unlock()

    assert fast_time.slept > 0
