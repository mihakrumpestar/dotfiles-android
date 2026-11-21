"""Artifact bytes: the canonical form two exports are compared on.

The byte format is fixed by the artifacts already committed, so the encoder
keeps their indent, key order, escaping and float notation. Change any of those
and every stored file compares as drift.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import math
import re
import zipfile
from typing import Any

# The escape set of the committed files: quotes, backslashes, the five short
# control escapes (\b, \f, \n, \r, \t), \u00xx for every other byte below 0x20,
# the HTML trio, and the two JavaScript line separators. Hex is lowercase.
_ESCAPES = {code: f"\\u{code:04x}" for code in range(0x20)}
_ESCAPES.update(
    {
        0x08: "\\b",
        0x09: "\\t",
        0x0A: "\\n",
        0x0C: "\\f",
        0x0D: "\\r",
        0x22: '\\"',
        0x5C: "\\\\",
        0x3C: "\\u003c",
        0x3E: "\\u003e",
        0x26: "\\u0026",
        0x2028: "\\u2028",
        0x2029: "\\u2029",
    }
)

# UTF-8 cannot encode a lone surrogate, so each becomes U+FFFD.
_SURROGATES = re.compile("[\ud800-\udfff]")


class ArtifactError(ValueError):
    """An artifact that is not JSON where the recipe needs JSON."""


def _escape(text: str) -> str:
    """Quote a string in the stored format, surrounding quotes included."""
    return '"' + _SURROGATES.sub("\ufffd", text).translate(_ESCAPES) + '"'


def _float(value: float) -> str:
    """Render a float the way the stored artifacts render it.

    The digits are the shortest form that reads back as the same value. Fixed
    notation covers 1e-6 <= abs(x) < 1e21, exponent notation the rest. An
    exponent keeps at least two digits, but drops the leading zero when a
    negative exponent has only one.
    """
    if not math.isfinite(value):
        raise ArtifactError(f"unsupported float: {value!r}")
    sign = "-" if math.copysign(1.0, value) < 0 else ""
    magnitude = abs(value)
    if magnitude == 0:
        return sign + "0"
    mantissa, _, exponent = repr(magnitude).partition("e")
    whole, _, fraction = mantissa.partition(".")
    combined = whole + fraction
    digits = combined.lstrip("0")
    power = (int(exponent) if exponent else 0) + len(whole) - 1 - (len(combined) - len(digits))
    digits = digits.rstrip("0")
    if 1e-6 <= magnitude < 1e21:
        if power >= len(digits) - 1:
            return sign + digits + "0" * (power - len(digits) + 1)
        if power >= 0:
            return sign + digits[: power + 1] + "." + digits[power + 1 :]
        return sign + "0." + "0" * (-power - 1) + digits
    if len(digits) > 1:
        digits = digits[0] + "." + digits[1:]
    rendered = str(abs(power))
    if len(rendered) < 2:
        rendered = "0" + rendered
    if power < 0:
        return sign + digits + "e-" + rendered.removeprefix("0")
    return sign + digits + "e+" + rendered


def _encode(value: Any, out: list[str], indent: str | None, depth: int) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_escape(value))
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_float(value))
    elif isinstance(value, dict):
        if not value:
            out.append("{}")
            return
        for key in value:
            if not isinstance(key, str):
                raise TypeError(f"object key must be a string, not {type(key).__name__}")
        out.append("{")
        for index, key in enumerate(sorted(value)):
            if index:
                out.append(",")
            if indent is not None:
                out.append("\n")
                out.append(indent * (depth + 1))
            out.append(_escape(key))
            out.append(": " if indent is not None else ":")
            _encode(value[key], out, indent, depth + 1)
        if indent is not None:
            out.append("\n")
            out.append(indent * depth)
        out.append("}")
    elif isinstance(value, list):
        if not value:
            out.append("[]")
            return
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            if indent is not None:
                out.append("\n")
                out.append(indent * (depth + 1))
            _encode(item, out, indent, depth + 1)
        if indent is not None:
            out.append("\n")
            out.append(indent * depth)
        out.append("]")
    else:
        raise TypeError(f"unsupported type: {type(value).__name__}")


def _compact(value: Any) -> str:
    out: list[str] = []
    _encode(value, out, None, 0)
    return "".join(out)


def dumps(value: Any) -> bytes:
    """Encode a value as the bytes a stored artifact holds."""
    out: list[str] = []
    _encode(value, out, " ", 0)
    return ("".join(out) + "\n").encode()


def _reject_constant(text: str) -> Any:
    raise ArtifactError(f"not JSON: {text}")


def _parse_float(text: str) -> float:
    value = float(text)
    if math.isinf(value):
        raise ArtifactError(f"number overflows float64: {text}")
    return value


def loads(body: bytes | str) -> Any:
    """Parse JSON strictly: NaN, Infinity and float overflow are errors."""
    return json.loads(body, parse_constant=_reject_constant, parse_float=_parse_float)


def mask(value: Any, path: list[str]) -> None:
    """Replace a volatile value with a placeholder, creating nothing.

    A path the export omits stays absent, so masking never invents a field that
    would then show up as drift.
    """
    if not path:
        return
    if isinstance(value, list):
        for item in value:
            mask(item, path)
        return
    if not isinstance(value, dict):
        return
    if len(path) == 1:
        if path[0] in value:
            value[path[0]] = "<volatile>"
        return
    mask(value.get(path[0]), path[1:])


def remove(value: Any, path: list[str]) -> None:
    """Drop a field, creating nothing.

    A path the export omits stays absent: removing is idempotent, so a stored
    artifact that already lacks the field and a fresh export that still has it
    compare equal.
    """
    if not path:
        return
    if isinstance(value, list):
        for item in value:
            remove(item, path)
        return
    if not isinstance(value, dict):
        return
    if len(path) == 1:
        value.pop(path[0], None)
        return
    remove(value.get(path[0]), path[1:])


def zero(value: Any, path: list[str]) -> None:
    """Replace a value with the zero of its type, so a restore resets it."""
    if not path:
        return
    if isinstance(value, list):
        for item in value:
            zero(item, path)
        return
    if not isinstance(value, dict):
        return
    if len(path) == 1:
        current = value.get(path[0])
        if isinstance(current, str):
            value[path[0]] = ""
        elif isinstance(current, bool):
            value[path[0]] = False
        elif isinstance(current, (int, float)):
            value[path[0]] = 0
        elif isinstance(current, list):
            value[path[0]] = []
        elif isinstance(current, dict):
            value[path[0]] = {}
        return
    zero(value.get(path[0]), path[1:])


def sort_list(value: Any, path: list[str]) -> None:
    """Order a list so app ordering is not drift.

    An element carrying a string `id` sorts by that id, because it is the
    stable identity and does not move when the element's settings change. Any
    other element sorts by its canonical bytes, which keeps lists without an
    identity deterministic.
    """
    if not path:
        return
    if isinstance(value, list):
        for item in value:
            sort_list(item, path)
        return
    if not isinstance(value, dict):
        return
    if len(path) == 1:
        items = value.get(path[0])
        if isinstance(items, list):
            value[path[0]] = sorted(items, key=_list_key)
        return
    sort_list(value.get(path[0]), path[1:])


def _list_key(item: Any) -> tuple[int, str]:
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return (0, item["id"])
    return (1, _compact(item))


_ZIP_MAGIC = b"PK\x03\x04"


def _zip_form(body: bytes) -> bytes | None:
    """The content form of a zip: one line per member, ordered by name.

    A line is the member name, its size and the sha256 of its bytes, so two
    archives holding the same members compare equal whatever their timestamps,
    attributes, compression or member order. None for bytes that only claim to
    be a zip and do not open as one.
    """
    if not body.startswith(_ZIP_MAGIC):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            lines = []
            for info in archive.infolist():
                data = archive.read(info)
                lines.append(f"{info.filename} {len(data)} {hashlib.sha256(data).hexdigest()}")
    except zipfile.BadZipFile:
        return None
    return ("zip\n" + "".join(f"{line}\n" for line in sorted(lines))).encode()


def canonical(
    body: bytes | str,
    volatile: list[str],
    sort_lists: list[str],
    *,
    omit: list[str] | tuple[str, ...] = (),
) -> bytes:
    """The bytes two exports are compared on.

    Omitted values are removed, volatile values masked, and lists ordered, so a
    field that is deliberately not stored and an app order the phone chooses
    are not drift. A zip has a content form instead: one ``<name> <size>
    <sha256>`` line per member, sorted by name, so timestamps and member order
    are not drift. A body that is neither JSON nor a readable zip has no
    canonical form, so it is returned unchanged; the caller's diff then says the
    bytes differ. ``volatile``, ``sort_lists`` and ``omit`` mean nothing for a
    zip.
    """
    raw = body.encode() if isinstance(body, str) else body
    zipped = _zip_form(raw)
    if zipped is not None:
        return zipped
    try:
        value = loads(raw)
    except ValueError:
        return raw
    for field in omit:
        remove(value, field.split("."))
    for field in volatile:
        mask(value, field.split("."))
    for field in sort_lists:
        sort_list(value, field.split("."))
    return dumps(value)


def normalize(
    body: bytes | str,
    omit: list[str] | tuple[str, ...],
    sort_lists: list[str],
) -> bytes:
    """The bytes an export may be stored as: omitted fields gone, lists ordered.

    This is the stored form, not the comparison form: nothing is masked, so a
    field that is not kept is absent rather than a placeholder. A non-JSON body
    is returned unchanged, because omission and ordering are JSON operations.
    """
    raw = body.encode() if isinstance(body, str) else body
    try:
        value = loads(raw)
    except ValueError:
        return raw
    for field in omit:
        remove(value, field.split("."))
    for field in sort_lists:
        sort_list(value, field.split("."))
    return dumps(value)


def redact(body: bytes | str, fields: list[str]) -> bytes:
    """The bytes an export may be stored as, with every redact path zeroed."""
    try:
        value = loads(body)
    except ValueError:
        return body.encode() if isinstance(body, str) else body
    for field in fields:
        zero(value, field.split("."))
    return dumps(value)


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def diff(old: bytes, new: bytes, name: str) -> str:
    """A unified diff, or a note when the bytes are not text."""
    try:
        old_text = old.decode()
        new_text = new.decode()
    except UnicodeDecodeError:
        return f"binary artifacts differ: {digest(old)[:12]} -> {digest(new)[:12]}\n"
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )
