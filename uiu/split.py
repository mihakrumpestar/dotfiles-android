"""Splitting one export into the part that may be committed and the part that may not.

A recipe's `private` list names either a value (`settings.token`, zeroed in the
public half and kept in the overlay file) or one list element (`apps[id=com.x]`,
moved whole into the overlay while every other element stays published). The
overlay is written even when empty, because its absence has to mean the private
half was lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import artifacts


class SplitError(ValueError):
    pass


@dataclass(frozen=True)
class Selection:
    path: tuple[str, ...]
    key: str | None = None
    value: str | None = None

    @property
    def keyed(self) -> bool:
        return self.key is not None

    def __str__(self) -> str:
        joined = ".".join(self.path)
        return f"{joined}[{self.key}={self.value}]" if self.keyed else joined


def parse_selection(field: str) -> Selection:
    """Parse `path.to.value` or `path.to.list[key=value]` into its parts."""
    open_bracket = field.rfind("[")
    if open_bracket < 0:
        return Selection(path=tuple(field.split(".")))
    if not field.endswith("]"):
        raise SplitError(f"{field!r}: an identity selector ends with ]")
    body = field[open_bracket + 1 : -1]
    key, sep, value = body.partition("=")
    if not sep or not key or not value:
        raise SplitError(f"{field!r}: an identity selector is written list[key=value]")
    return Selection(path=tuple(field[:open_bracket].split(".")), key=key, value=value)


def value_at(value: Any, path: tuple[str, ...]) -> tuple[Any, bool]:
    """Read the value at path without creating anything, walking lists in place."""
    if not path:
        return None, False
    if isinstance(value, list):
        for item in value:
            found, present = value_at(item, path)
            if present:
                return found, True
        return None, False
    if not isinstance(value, dict):
        return None, False
    if len(path) == 1:
        if path[0] in value:
            return value[path[0]], True
        return None, False
    return value_at(value.get(path[0]), path[1:])


def put_at(value: Any, path: tuple[str, ...], taken: Any) -> None:
    """Write a value at path, creating the objects on the way.

    A list on the way is refused rather than replaced. Reading and zeroing walk
    lists, but there is no single place to write one value back, so turning a
    list into an object would destroy every element on restore.
    """
    if not path or not isinstance(value, dict):
        raise SplitError(f"cannot write {'.'.join(path)}: its parent is not an object")
    if len(path) == 1:
        value[path[0]] = taken
        return
    nxt = value.get(path[0])
    if nxt is None:
        nxt = {}
        value[path[0]] = nxt
    elif not isinstance(nxt, dict):
        raise SplitError(f"cannot write {'.'.join(path)}: it crosses a list")
    put_at(nxt, path[1:], taken)


def crosses_list(value: Any, path: tuple[str, ...]) -> bool:
    """Whether a plain path would walk into a list rather than only into objects."""
    node = value
    for segment in path[:-1]:
        if isinstance(node, list):
            return True
        if not isinstance(node, dict):
            return False
        node = node.get(segment)
    return isinstance(node, list)


def is_zero(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def identity(item: Any, key: str) -> str:
    if not isinstance(item, dict):
        return ""
    found = item.get(key)
    return found if isinstance(found, str) else ""


@dataclass(frozen=True)
class SplitResult:
    public: bytes
    private: bytes
    # A plain path the export does not have. It is not fatal: an app may omit a
    # field entirely (yamlauncher's location is one), so the recipe keeps a guard
    # for the day it appears. The caller reports these, because a typo looks the
    # same and would leave the intended value in the public half.
    unmatched: tuple[str, ...] = ()


def split_export(body: bytes | str, fields: list[str]) -> SplitResult:
    """Divide an export into the committable half and the overlay."""
    try:
        value = artifacts.loads(body)
    except ValueError as error:
        raise artifacts.ArtifactError(f"artifact is not JSON: {error}") from error
    overlay: dict[str, Any] = {}
    unmatched: list[str] = []
    for field in fields:
        chosen = parse_selection(field)
        if chosen.keyed:
            _lift_entry(value, overlay, chosen)
            continue
        taken, present = value_at(value, chosen.path)
        if not present:
            unmatched.append(field)
            continue
        if crosses_list(value, chosen.path):
            raise SplitError(
                f"{field}: a private path cannot cross a list;"
                " use list[key=value] to move one element into the overlay"
            )
        if not is_zero(taken):
            put_at(overlay, chosen.path, taken)
        artifacts.zero(value, list(chosen.path))
    return SplitResult(artifacts.dumps(value), artifacts.dumps(overlay), tuple(unmatched))


def _lift_entry(value: Any, overlay: dict[str, Any], chosen: Selection) -> None:
    if chosen.key is None or chosen.value is None:
        raise SplitError(f"{chosen}: an identity selector needs a key and a value")
    items, present = value_at(value, chosen.path)
    if not present:
        raise SplitError(f"{chosen} matches nothing: the export has no {'.'.join(chosen.path)}")
    if not isinstance(items, list):
        raise SplitError(f"{chosen} names a value, not a list")
    kept, lifted = [], []
    for item in items:
        (lifted if identity(item, chosen.key) == chosen.value else kept).append(item)
    if not lifted:
        raise SplitError(f"{chosen} matches nothing in {len(items)} element(s)")
    put_at(value, chosen.path, kept)
    existing, _ = value_at(overlay, chosen.path)
    batch = list(existing) if isinstance(existing, list) else []
    put_at(overlay, chosen.path, batch + lifted)


def merge_export(public: bytes | str, private: bytes | str, fields: list[str]) -> bytes:
    """Put the overlay's values back, so a restore pushes the whole export."""
    try:
        value = artifacts.loads(public)
        overlay = artifacts.loads(private)
    except ValueError as error:
        raise artifacts.ArtifactError(f"artifact is not JSON: {error}") from error
    for field in fields:
        chosen = parse_selection(field)
        if chosen.keyed:
            _restore_entry(value, overlay, chosen)
            continue
        taken, present = value_at(overlay, chosen.path)
        if not present:
            # The overlay has no value. That is normal when the value was already
            # zero at split time, and a broken pair when the artifact still holds
            # one, since pushing the zero would wipe the phone's value.
            current, held = value_at(value, chosen.path)
            if held and not is_zero(current):
                raise SplitError(f"{field} needs the private overlay, and it holds no such value")
            continue
        if crosses_list(value, chosen.path):
            raise SplitError(
                f"{field}: the public half has a list where the overlay expects objects"
            )
        put_at(value, chosen.path, taken)
    return artifacts.dumps(value)


def _restore_entry(value: Any, overlay: Any, chosen: Selection) -> None:
    if chosen.key is None or chosen.value is None:
        raise SplitError(f"{chosen}: an identity selector needs a key and a value")
    taken, present = value_at(overlay, chosen.path)
    if not present:
        raise SplitError(f"{chosen} needs the private overlay, and it holds no such entry")
    if not isinstance(taken, list):
        raise SplitError(f"{chosen}: the overlay's {'.'.join(chosen.path)} is not a list")
    matching = [item for item in taken if identity(item, chosen.key) == chosen.value]
    if not matching:
        raise SplitError(f"{chosen} needs the private overlay, and it holds no such entry")

    current, _ = value_at(value, chosen.path)
    items = list(current) if isinstance(current, list) else []
    for item in matching:
        replaced = False
        for index, existing in enumerate(items):
            if identity(existing, chosen.key) == chosen.value:
                items[index] = item
                replaced = True
                break
        if not replaced:
            items.append(item)
    put_at(value, chosen.path, items)
