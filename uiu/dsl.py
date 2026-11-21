"""The builder for an app file: plain Python, one function call per fact.

An app is one `app.py`, and its steps read like the gestures they perform:

    from uiu.dsl import app, key, launch, pull, tap, wait

    APP = app(
        package="com.example.demo",
        artifact="demo.json",
        backup=[
            key("KEYCODE_HOME"),
            launch("com.example.demo/.MainActivity", clear=True),
            wait(desc="Settings"),
            tap(desc="Settings"),
            pull("/sdcard/Download/demo.json", "{{artifact}}"),
        ],
    )

The builder runs the same validation as the engine that executes the steps: an
empty selector, a path that leaves the export directory or a missing artifact is
an error, never a silently ignored field.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .recipe import Recipe, RecipeError, Selector, Step, parse_private, validate
from .spec import App, build


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise RecipeError(f"{where}: a string")
    return value


def _whole(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeError(f"{where}: a whole number")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecipeError(f"{where}: a number")
    return float(value)


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise RecipeError(f"{where}: true or false")
    return value


# The names a recipe writes, mapped to the `Selector` fields they set.
_SELECTOR_FIELDS = {
    "text": "text",
    "id": "resource_id",
    "desc": "desc",
    "text_contains": "text_contains",
    "class_name": "class_name",
    "index": "index",
}


def _selector(where: str, fields: dict[str, Any]) -> Selector | None:
    """Turn selector keyword arguments into a `Selector`, or None when empty.

    The same names the probe prints are the names a recipe writes, so a value
    can move from the screen to the recipe without translation.
    """
    unknown = sorted(set(fields) - set(_SELECTOR_FIELDS))
    if unknown:
        raise RecipeError(f'{where}: unknown selector key "{unknown[0]}"')
    values: dict[str, Any] = {}
    for name, field_name in _SELECTOR_FIELDS.items():
        if name not in fields:
            continue
        if name == "index":
            values[field_name] = _whole(fields[name], f"{where}: {name}")
        else:
            values[field_name] = _text(fields[name], f"{where}: {name}")
    if not values:
        return None
    selector = Selector(**values)
    if selector.empty:
        raise RecipeError("selector names no field")
    if selector.index < 0:
        raise RecipeError("selector index is negative")
    return selector


def _step(kind: str, **values: Any) -> Step:
    return Step(kind=kind, **values)


def tap(*, duration: int = 0, optional: bool = False, **selector: Any) -> Step:
    return _step(
        "tap",
        selector=_selector("tap", selector),
        duration=_whole(duration, "tap: duration"),
        optional=_flag(optional, "tap: optional"),
    )


def wait(
    *, timeout: float = 10.0, checked: bool | None = None, absent: bool = False, **selector: Any
) -> Step:
    return _step(
        "wait",
        selector=_selector("wait", selector),
        timeout=_number(timeout, "wait: timeout"),
        checked=None if checked is None else _flag(checked, "wait: checked"),
        absent=_flag(absent, "wait: absent"),
    )


def type_text(text: str, **selector: Any) -> Step:
    return _step("type", text=_text(text, "type: text"), selector=_selector("type", selector))


def clear(**selector: Any) -> Step:
    return _step("clear", selector=_selector("clear", selector))


def assert_checked(checked: bool, **selector: Any) -> Step:
    return _step(
        "assert",
        checked=_flag(checked, "assert: checked"),
        selector=_selector("assert", selector),
    )


def ensure(checked: bool, **selector: Any) -> Step:
    return _step(
        "ensure",
        checked=_flag(checked, "ensure: checked"),
        selector=_selector("ensure", selector),
    )


def launch(component: str, *, clear: bool = False) -> Step:
    return _step(
        "launch",
        component=_text(component, "launch: component"),
        clear=_flag(clear, "launch: clear"),
    )


def key(code: str) -> Step:
    return _step("key", code=_text(code, "key: code"))


def scroll(direction: str = "down", *, times: int = 1, until: Any = None) -> Step:
    target = None
    if until is not None:
        if not isinstance(until, dict):
            raise RecipeError("scroll: until is an object of selector keys")
        target = _selector("scroll: until", until)
    return _step(
        "scroll",
        direction=_text(direction, "scroll: direction"),
        times=_whole(times, "scroll: times"),
        selector=target,
    )


def shell(command: str) -> Step:
    return _step("shell", command=_text(command, "shell: command"))


def sleep(seconds: float) -> Step:
    return _step("sleep", seconds=_number(seconds, "sleep: seconds"))


def pull(source: str, target: str, *, newest: bool = False) -> Step:
    return _step(
        "pull",
        source=_text(source, "pull: from"),
        target=_text(target, "pull: to"),
        newest=_flag(newest, "pull: newest"),
    )


def push(source: str, target: str) -> Step:
    return _step(
        "push",
        source=_text(source, "push: from"),
        target=_text(target, "push: to"),
    )


def capture(name: str) -> Step:
    return _step("capture", name=_text(name, "capture: name"))


def _steps(values: Iterable[Any], where: str) -> list[Step]:
    steps = list(values)
    for position, step in enumerate(steps):
        if not isinstance(step, Step):
            raise RecipeError(f"{where} step {position + 1}: a DSL step, not {type(step).__name__}")
    return steps


def if_absent(*, steps: Iterable[Any], **selector: Any) -> Step:
    return _step(
        "if_absent",
        selector=_selector("if_absent", selector),
        steps=tuple(_steps(steps, "if_absent")),
    )


def recipe(
    *,
    package: str,
    backup: Iterable[Any],
    artifact: str = "",
    restore: Iterable[Any] = (),
    setup: Iterable[Any] = (),
    volatile: Iterable[str] = (),
    omit: Iterable[str] = (),
    redact: Iterable[str] = (),
    private: Iterable[str] | str = (),
    sort: Iterable[str] = (),
) -> Recipe:
    """Build and validate one app's recipe.

    `private` is either a list of paths or the name of a file holding one. The
    file name is the safer form, because the list itself says which entries
    exist.
    """
    private_fields, private_file = parse_private(private or None)
    built = Recipe(
        package=_text(package, "package"),
        artifact=_text(artifact, "artifact"),
        volatile=[_text(item, "volatile") for item in volatile],
        omit=[_text(item, "omit") for item in omit],
        redact=[_text(item, "redact") for item in redact],
        private=private_fields,
        private_file=private_file,
        sort=[_text(item, "sort") for item in sort],
        backup=_steps(backup, "backup"),
        restore=_steps(restore, "restore"),
        setup=_steps(setup, "setup"),
    )
    validate(built)
    return built


def app(**facts: Any) -> App:
    """Build one app: its package, install facts, device state and screen walk.

    Every argument is keyword-only and checked here, so an app file that names a
    permission or a step wrongly fails before the phone is touched.
    """
    return build(**facts)
