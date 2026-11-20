"""Shared test support: XML dump builders, a fake device, recipe fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from uiu.device import ShellResult
from uiu.dsl import recipe as build
from uiu.recipe import Recipe, parse_step
from uiu.spec import App
from uiu.spec import build as build_app

REPO = Path(__file__).resolve().parent.parent

# A SOPS envelope in binary mode, shaped the way `sops --output-type binary`
# writes one. Nothing in the suite can decrypt it: the vault's `run` seam and
# the identity are injected, so only the shape is ever read.
ENVELOPE = json.dumps(
    {
        "data": "ENC[AES256_GCM,data:YWJj,iv:Zm9v,tag:YmFy,type:str]",
        "sops": {"mac": "ENC[AES256_GCM,data:bWFj,iv:Zm9v,tag:YmFy,type:str]"},
    }
).encode()

VALID = {
    "package": "com.example.demo",
    "artifact": "demo.json",
    "backup": [
        {"launch": {"component": "com.example.demo/.MainActivity"}},
        {"pull": {"from": "/sdcard/Download/demo.json", "to": "{{artifact}}"}},
    ],
    "restore": [
        {"push": {"from": "{{artifact}}", "to": "/sdcard/Download/{{artifact}}"}},
    ],
}


def recipe_from(mapping: dict) -> Recipe:
    """Build a recipe from the dict form tests use for mutations.

    Data files are Python now, but a test that changes one field reads better as
    a dict than as a string of code.
    """
    return build(
        package=mapping.get("package", ""),
        artifact=mapping.get("artifact", ""),
        volatile=mapping.get("volatile", []),
        redact=mapping.get("redact", []),
        private=mapping.get("private", []),
        sort=mapping.get("sort", []),
        backup=[parse_step(step) for step in mapping.get("backup", [])],
        restore=[parse_step(step) for step in mapping.get("restore", [])],
        setup=[parse_step(step) for step in mapping.get("setup", [])],
    )


def _pairs(raw: dict | None) -> dict[str, tuple[str, str]]:
    """A JSON object of `name: [value, reason]` back into the tuple form."""
    return {name: (pair[0], pair[1]) for name, pair in (raw or {}).items()}


def app_from(mapping: dict[str, Any]) -> App:
    """Build an app from the dict form tests use for mutations.

    The app file is Python now, but a test that changes one field reads better
    as a dict than as a string of code. JSON turns every tuple into a list, so
    the device-state pairs are restored here.
    """
    doze = mapping.get("doze")
    return build_app(
        package=mapping.get("package", ""),
        install=mapping.get("install"),
        permissions=_pairs(mapping.get("permissions")),
        appops=_pairs(mapping.get("appops")),
        roles=mapping.get("roles", {}),
        doze=tuple(doze) if doze is not None else None,
        artifact=mapping.get("artifact", ""),
        backup=[parse_step(step) for step in mapping.get("backup", [])],
        restore=[parse_step(step) for step in mapping.get("restore", [])],
        setup=[parse_step(step) for step in mapping.get("setup", [])],
        volatile=mapping.get("volatile", []),
        redact=mapping.get("redact", []),
        private=mapping.get("private", []),
        sort=mapping.get("sort", []),
    )


def app_file(mapping: dict | None = None) -> str:
    """An `app.py` body that builds the given app through the dict adapter."""
    body = json.dumps(mapping if mapping is not None else VALID)
    return f"import json\nfrom support import app_from\nAPP = app_from(json.loads({body!r}))\n"


def recipe_in(directory: Path, mapping: dict | None = None) -> Recipe:
    recipe = recipe_from(mapping or VALID)
    recipe.dir = directory
    recipe.data_dir = directory
    recipe.blob_dir = directory
    return recipe


def xml_node(
    *,
    text: str = "",
    id: str = "",
    desc: str = "",
    cls: str = "android.widget.TextView",
    package: str = "com.example.demo",
    bounds: str = "[0,0,100,100]",
    clickable: bool = False,
    checkable: bool = False,
    checked: bool = False,
) -> str:
    attributes = {
        "index": "0",
        "text": text,
        "resource-id": id,
        "class": cls,
        "package": package,
        "content-desc": desc,
        "checkable": str(checkable).lower(),
        "checked": str(checked).lower(),
        "clickable": str(clickable).lower(),
        "enabled": "true",
        "focusable": "true",
        "focused": "false",
        "scrollable": "false",
        "long-clickable": "false",
        "password": "false",
        "selected": "false",
        "bounds": bounds,
    }
    rendered = " ".join(f'{key}="{value}"' for key, value in attributes.items())
    return f"<node {rendered} />"


def xml_screen(*nodes: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
        + "".join(nodes)
        + "</hierarchy>"
    )


class FakeDevice:
    """The UI surface the runner and probe use, with every call recorded.

    `screens` is one dump or a sequence served in order; the last one repeats.
    `reply` lets a test answer a shell command the way the real device would.
    """

    def __init__(
        self,
        screens: str | Sequence[str],
        *,
        clock: int = 1000,
        size: tuple[int, int] = (1080, 2340),
        reply: Callable[[str | Sequence[str]], ShellResult | None] | None = None,
    ):
        self.screens = [screens] if isinstance(screens, str) else list(screens)
        self.clock = clock
        self.size = size
        self.reply = reply
        self.dumps = 0
        self.taps: list[tuple[int, int]] = []
        self.long_presses: list[tuple[int, int, int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []
        self.keys: list[str] = []
        self.texts: list[str] = []
        self.cleared = 0
        self.pushes: list[tuple[Path, str]] = []
        self.pushed: list[tuple[str, bytes]] = []
        self.pulls: list[tuple[str, Path]] = []
        self.shells: list[str | Sequence[str]] = []
        self.stopped: list[str] = []
        self.launched: list[tuple[str, bool]] = []
        self.clock_reads = 0
        self.pull_body: bytes | None = None
        self.pull_bodies: list[bytes] | None = None
        self.sizes: dict[str, int] = {}
        self.unlocks = 0

    def dump(self) -> str:
        index = min(self.dumps, len(self.screens) - 1)
        self.dumps += 1
        return self.screens[index]

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def long_press(self, x: int, y: int, millis: int) -> None:
        self.long_presses.append((x, y, millis))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, millis: int = 300) -> None:
        self.swipes.append((x1, y1, x2, y2, millis))

    def key(self, code: str) -> None:
        self.keys.append(code)

    def set_text(self, text: str) -> None:
        self.texts.append(text)

    def clear_text(self) -> None:
        self.cleared += 1

    def push(self, local: Path, remote: str) -> None:
        self.pushes.append((Path(local), remote))
        self.pushed.append((remote, Path(local).read_bytes()))

    def pull(self, remote: str, local: Path) -> None:
        self.pulls.append((remote, Path(local)))
        if self.pull_bodies is not None:
            body = self.pull_bodies.pop(0) if self.pull_bodies else b""
        else:
            body = self.pull_body or b""
        Path(local).write_bytes(body)
        self.sizes[remote] = len(body)

    def shell(self, command: str | Sequence[str], timeout: float = 60) -> ShellResult:
        self.shells.append(command)
        if self.reply is not None:
            answer = self.reply(command)
            if answer is not None:
                return answer
        if isinstance(command, list) and command[:2] == ["stat", "-c"]:
            return ShellResult(0, str(self.sizes.get(str(command[-1]), 10)) + "\n")
        return ShellResult(0, "")

    def force_stop(self, package: str) -> None:
        self.stopped.append(package)

    def wait_gone(self, package: str, within: float = 3.0) -> None:
        return None

    def device_clock(self) -> int:
        self.clock_reads += 1
        return self.clock

    def screen_size(self) -> tuple[int, int]:
        return self.size

    def focus(self) -> str:
        return "com.example.demo/.MainActivity"

    def launch(self, component: str, clear: bool = False) -> None:
        self.launched.append((component, clear))

    def unlock(self) -> None:
        self.unlocks += 1
