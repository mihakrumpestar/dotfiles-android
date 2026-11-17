"""Snapshots: the timestamped JSON files under .state that make a run undoable."""

from __future__ import annotations

import re

import pytest

from androidctl import snapshots
from androidctl.snapshots import SnapshotError


def test_a_package_snapshot_round_trips_in_the_go_shape(tmp_path):
    path = snapshots.write_package_snapshot(tmp_path, "SERIAL", {"b": "disabled", "a": "enabled"})
    body = path.read_bytes()

    assert re.fullmatch(r"\d{8}T\d{6}Z-SERIAL\.json", path.name)
    assert body.startswith(b'{\n "created": "20')
    assert b'"created"' in body and body.endswith(b"\n")
    # One-space indent, keys sorted, no spaces before the colon.
    assert (
        b'\n "packages": {\n  "a": "enabled",\n  "b": "disabled"\n },\n "serial": "SERIAL"\n}\n'
        in body
    )

    snapshot = snapshots.read_package_snapshot(path)
    assert snapshot.serial == "SERIAL"
    assert snapshot.packages == {"a": "enabled", "b": "disabled"}
    assert snapshot.enabled_in() == ["a"]
    assert snapshot.summary(path) == f"snapshot {path.name} ({snapshot.created})"


def test_the_newest_snapshot_is_the_last_name(tmp_path, monkeypatch):
    stamps = iter(
        [("20260101T000000Z", "2026-01-01T00:00:00Z"), ("20260202T000000Z", "2026-02-02T00:00:00Z")]
    )
    monkeypatch.setattr(snapshots, "stamp", lambda: next(stamps))
    older = snapshots.write_package_snapshot(tmp_path, "SERIAL", {})
    newer = snapshots.write_package_snapshot(tmp_path, "SERIAL", {})

    assert snapshots.newest_package_snapshot(tmp_path, "SERIAL") == newer
    assert older.name < newer.name


def test_no_snapshot_for_the_serial_says_so(tmp_path):
    with pytest.raises(SnapshotError, match="no snapshots for SERIAL"):
        snapshots.newest_package_snapshot(tmp_path, "SERIAL")


def test_a_file_that_is_not_a_package_snapshot_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"created": "x", "serial": "Y", "entries": {}}')
    with pytest.raises(SnapshotError, match="not a package snapshot"):
        snapshots.read_package_snapshot(path)


def test_missing_and_broken_files_raise_snapshot_errors(tmp_path):
    with pytest.raises(SnapshotError, match="reading"):
        snapshots.read_package_snapshot(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(SnapshotError, match="parsing"):
        snapshots.read_package_snapshot(broken)


def test_a_legacy_profile_snapshot_values_shape_is_readable(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(
        '{"created": "2026-01-01T00:00:00Z", "serial": "SERIAL", '
        '"values": {"system/a": "1", "system/b": null}}'
    )
    snapshot = snapshots.read_profile_snapshot(path)
    before = snapshot.before()

    assert before["system/a"].state is snapshots.State.EXPLICIT
    assert before["system/a"].value == "1"
    assert before["system/b"].state is snapshots.State.UNSET
    assert before["system/b"].value == ""


def test_a_profile_snapshot_with_an_unknown_state_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        '{"created": "x", "serial": "S", "entries": {"system/a": {"state": "maybe", "value": "1"}}}'
    )
    with pytest.raises(SnapshotError, match="maybe"):
        snapshots.read_profile_snapshot(path)


def test_a_file_with_neither_entries_nor_values_is_not_a_profile_snapshot(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"created": "x", "serial": "S"}')
    with pytest.raises(SnapshotError, match="not a profile snapshot"):
        snapshots.read_profile_snapshot(path)


def test_a_package_snapshot_with_a_non_string_state_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"created": "x", "serial": "S", "packages": {"com.a": 1}}')
    with pytest.raises(SnapshotError, match="not a package snapshot"):
        snapshots.read_package_snapshot(path)


def test_a_package_snapshot_with_an_unknown_state_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"created": "x", "serial": "S", "packages": {"com.a": "gone"}}')
    with pytest.raises(SnapshotError, match="not a package snapshot"):
        snapshots.read_package_snapshot(path)


def test_a_profile_entry_without_a_state_reads_as_unset(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"created": "x", "serial": "S", "entries": {"system/a": {"value": "1"}}}')
    entry = snapshots.read_profile_snapshot(path).before()["system/a"]
    assert entry.state is snapshots.State.UNSET
    assert entry.value == "1"


def test_a_profile_entry_with_a_non_string_value_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        '{"created": "x", "serial": "S", "entries": {"system/a": {"state": "explicit", "value": 1}}}'
    )
    with pytest.raises(SnapshotError, match="not a string"):
        snapshots.read_profile_snapshot(path)


def test_an_unwritable_state_directory_is_a_snapshot_error(tmp_path):
    blocked = tmp_path / "profile"
    blocked.write_text("not a directory")
    with pytest.raises(SnapshotError, match="creating"):
        snapshots.write_profile_snapshot(
            tmp_path, "SERIAL", {"system/a": snapshots.SnapshotEntry(snapshots.State.UNSET)}
        )


def test_an_entries_field_of_the_wrong_type_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"created": "x", "serial": "S", "entries": ["junk"], "values": {"a": "1"}}')
    with pytest.raises(SnapshotError, match="entries is not an object"):
        snapshots.read_profile_snapshot(path)
