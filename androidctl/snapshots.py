"""The JSON files under .state that let a run be undone.

A package snapshot records which packages were enabled before a disable. A
profile snapshot records the value and state of every profile entry before an
apply, including entries that had no value at all. Both are timestamped UTC so
sorting by name sorts by time.

Files are written with `uiu.artifacts.dumps`, the encoder the exported app
artifacts use: one-space indent, sorted keys, one trailing newline. Nothing
reads these files by key order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uiu import artifacts

from .profile import SnapshotEntry, State


class SnapshotError(RuntimeError):
    """A snapshot file that is missing, unreadable, or the wrong shape."""


@dataclass(frozen=True)
class PackageSnapshot:
    created: str
    serial: str
    packages: dict[str, str]

    def enabled_in(self) -> list[str]:
        """The packages this snapshot saw as enabled, sorted."""
        return sorted(name for name, state in self.packages.items() if state == "enabled")

    def summary(self, path: Path) -> str:
        return f"snapshot {path.name} ({self.created})"


def write_json(path: Path, value: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SnapshotError(f"creating {path.parent}: {error}") from error
    try:
        body = artifacts.dumps(value)
    except Exception as error:  # the encoder refuses values JSON cannot hold
        raise SnapshotError(f"encoding {path}: {error}") from error
    try:
        path.write_bytes(body)
    except OSError as error:
        raise SnapshotError(f"writing {path}: {error}") from error


def read_json(path: Path) -> Any:
    try:
        body = path.read_bytes()
    except OSError as error:
        raise SnapshotError(f"reading {path}: {error}") from error
    try:
        return artifacts.loads(body)
    except (ValueError, json.JSONDecodeError) as error:
        raise SnapshotError(f"parsing {path}: {error}") from error


def write_package_snapshot(state: Path | str, serial: str, states: dict[str, str]) -> Path:
    """Save the states a caller already read from the device."""
    stamp_value, created = stamp()
    path = Path(state) / "snapshots" / f"{stamp_value}-{serial}.json"
    write_json(
        path,
        {"created": created, "serial": serial, "packages": states},
    )
    return path


def read_package_snapshot(path: Path | str) -> PackageSnapshot:
    body = read_json(Path(path))
    packages = body.get("packages") if isinstance(body, dict) else None
    if not isinstance(packages, dict) or not all(
        isinstance(name, str) and state in {"enabled", "disabled"}
        for name, state in packages.items()
    ):
        # A snapshot is a map of package to state. A corrupt file is an error:
        # accepting it here would turn corruption into "nothing to restore".
        raise SnapshotError(f"{path} is not a package snapshot")
    return PackageSnapshot(
        created=str(body.get("created", "")),
        serial=str(body.get("serial", "")),
        packages=dict(packages),
    )


def newest_package_snapshot(state: Path | str, serial: str) -> Path:
    return newest_file(Path(state) / "snapshots", serial)


def newest_file(directory: Path, serial: str) -> Path:
    """The lexically last `<stamp>-<serial>.json`, which is the newest one."""
    matches = sorted(directory.glob(f"*-{serial}.json"))
    if not matches:
        raise SnapshotError(f"no snapshots for {serial} under {directory}")
    return matches[-1]


def stamp() -> tuple[str, str]:
    """The filename stamp and the RFC3339 timestamp for one instant."""
    now = datetime.now(UTC).replace(microsecond=0)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ProfileSnapshot:
    """A profile run's undo record: every touched key and its state before."""

    created: str
    serial: str
    entries: dict[str, SnapshotEntry] | None = None
    values: dict[str, str | None] | None = None

    def before(self) -> dict[str, SnapshotEntry]:
        """The entries to restore, including the older values-only shape."""
        if self.entries is not None:
            return self.entries
        if self.values is None:
            raise SnapshotError("not a profile snapshot")
        return {
            key: SnapshotEntry(
                state=State.EXPLICIT if value is not None else State.UNSET,
                value=value or "",
            )
            for key, value in self.values.items()
        }


def write_profile_snapshot(
    state: Path | str, serial: str, entries: dict[str, SnapshotEntry]
) -> Path:
    stamp_value, created = stamp()
    path = Path(state) / "profile" / f"{stamp_value}-{serial}.json"
    write_json(
        path,
        {
            "created": created,
            "serial": serial,
            "entries": {
                key: {"state": item.state.value, "value": item.value}
                for key, item in entries.items()
            },
        },
    )
    return path


def read_profile_snapshot(path: Path | str) -> ProfileSnapshot:
    body = read_json(Path(path))
    if not isinstance(body, dict):
        raise SnapshotError(f"{path} is not a profile snapshot")
    raw_entries = body.get("entries")
    raw_values = body.get("values")
    if raw_entries is not None and not isinstance(raw_entries, dict):
        raise SnapshotError(f"{path}: entries is not an object")
    if raw_values is not None and not isinstance(raw_values, dict):
        raise SnapshotError(f"{path}: values is not an object")
    entries = None
    if isinstance(raw_entries, dict):
        entries = {}
        for key, item in raw_entries.items():
            if not isinstance(item, dict):
                raise SnapshotError(f"{path}: malformed entry {key!r}")
            raw_state = item.get("state")
            if raw_state is None:
                # An omitted state means the key had no value: Unset.
                state = State.UNSET
            else:
                try:
                    state = State(raw_state)
                except ValueError as error:
                    raise SnapshotError(f"{path}: {key}: {error}") from error
            value = item.get("value")
            if value is not None and not isinstance(value, str):
                raise SnapshotError(f"{path}: {key}: value is not a string")
            entries[key] = SnapshotEntry(state=state, value=value or "")
    values = None
    if isinstance(raw_values, dict):
        values = {}
        for key, value in raw_values.items():
            if value is not None and not isinstance(value, str):
                raise SnapshotError(f"{path}: {key}: value is not a string")
            values[key] = value
    if entries is None and values is None:
        raise SnapshotError(f"{path} is not a profile snapshot")
    return ProfileSnapshot(
        created=str(body.get("created", "")),
        serial=str(body.get("serial", "")),
        entries=entries,
        values=values,
    )
