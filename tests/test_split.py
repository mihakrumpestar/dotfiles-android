"""Splitting one export into the committable half and the private overlay."""

from __future__ import annotations

import json

import pytest

from uiu import artifacts
from uiu.split import SplitError, merge_export, parse_selection, split_export


@pytest.mark.parametrize(
    ("field", "path", "key", "value"),
    [
        ("token", ("token",), None, None),
        ("data.location.value", ("data", "location", "value"), None, None),
        ("apps[id=com.bank]", ("apps",), "id", "com.bank"),
        ("data.apps[package=com.bank]", ("data", "apps"), "package", "com.bank"),
    ],
)
def test_selection(field, path, key, value):
    chosen = parse_selection(field)
    assert chosen.path == path
    assert chosen.key == key
    assert chosen.value == value


@pytest.mark.parametrize("field", ["apps[id]", "apps[=x]", "apps[id=]", "apps[id=x", "apps[id=x]y"])
def test_selection_rejects_malformed(field):
    with pytest.raises(SplitError):
        parse_selection(field)


def test_value_goes_to_the_overlay_and_the_public_half_is_zeroed():
    body = artifacts.dumps({"token": "secret", "keep": 1})
    result = split_export(body, ["token"])
    assert json.loads(result.public) == {"token": "", "keep": 1}
    assert json.loads(result.private) == {"token": "secret"}


def test_a_zero_value_stays_out_of_the_overlay():
    body = artifacts.dumps({"token": "", "keep": 1})
    assert split_export(body, ["token"]).private == b"{}\n"


def test_identity_selector_lifts_one_entry():
    body = artifacts.dumps({"apps": [{"id": "a", "name": "A"}, {"id": "bank", "name": "B"}]})
    result = split_export(body, ["apps[id=bank]"])
    assert json.loads(result.public) == {"apps": [{"id": "a", "name": "A"}]}
    assert json.loads(result.private) == {"apps": [{"id": "bank", "name": "B"}]}


def test_several_identity_selectors_share_the_overlay_list():
    body = artifacts.dumps({"apps": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
    result = split_export(body, ["apps[id=a]", "apps[id=c]"])
    assert [item["id"] for item in json.loads(result.private)["apps"]] == ["a", "c"]


def test_missing_entry_is_an_error():
    body = artifacts.dumps({"apps": [{"id": "a"}]})
    with pytest.raises(SplitError, match="matches nothing"):
        split_export(body, ["apps[id=bank]"])


def test_value_path_that_names_a_list_is_an_error():
    body = artifacts.dumps({"apps": "not a list"})
    with pytest.raises(SplitError, match="names a value"):
        split_export(body, ["apps[id=a]"])


def test_non_json_artifact_is_an_error():
    with pytest.raises(artifacts.ArtifactError):
        split_export(b"\x00", ["a"])


def test_merge_puts_values_and_entries_back():
    public = artifacts.dumps({"token": "", "apps": [{"id": "a"}]})
    private = artifacts.dumps({"token": "secret", "apps": [{"id": "bank"}]})
    merged = json.loads(merge_export(public, private, ["token", "apps[id=bank]"]))
    assert merged == {"token": "secret", "apps": [{"id": "a"}, {"id": "bank"}]}


def test_merge_replaces_an_entry_already_present():
    public = artifacts.dumps({"apps": [{"id": "bank", "name": "old"}]})
    private = artifacts.dumps({"apps": [{"id": "bank", "name": "new"}]})
    merged = json.loads(merge_export(public, private, ["apps[id=bank]"]))
    assert merged == {"apps": [{"id": "bank", "name": "new"}]}


def test_merge_refuses_a_missing_entry():
    public = artifacts.dumps({"apps": [{"id": "a"}]})
    with pytest.raises(SplitError, match="needs the private overlay"):
        merge_export(public, b"{}\n", ["apps[id=bank]"])


def test_round_trip_is_lossless():
    body = artifacts.dumps(
        {
            "data": {"location": {"value": "somewhere"}},
            "apps": [{"id": "a"}, {"id": "bank", "name": "B"}],
        }
    )
    fields = ["data.location.value", "apps[id=bank]"]
    result = split_export(body, fields)
    assert merge_export(result.public, result.private, fields) == body


def test_plain_path_that_crosses_a_list_is_refused():
    body = artifacts.dumps({"apps": [{"token": "t1"}, {"token": "t2"}]})
    with pytest.raises(SplitError, match="cannot cross a list"):
        split_export(body, ["apps.token"])


def test_identity_selector_inside_a_list_is_refused():
    body = artifacts.dumps({"groups": [{"apps": [{"id": "x"}, {"id": "y"}]}]})
    with pytest.raises(SplitError, match="crosses a list"):
        split_export(body, ["groups.apps[id=x]"])


def test_unmatched_plain_path_is_reported_not_fatal():
    result = split_export(artifacts.dumps({"keep": 1}), ["data.location.value"])
    assert result.unmatched == ("data.location.value",)
    assert json.loads(result.public) == {"keep": 1}


def test_merge_refuses_a_non_zero_value_the_overlay_lacks():
    public = artifacts.dumps({"settings": {"token": "live"}})
    with pytest.raises(SplitError, match="needs the private overlay"):
        merge_export(public, b"{}\n", ["settings.token"])


def test_merge_allows_a_zero_value_the_overlay_lacks():
    public = artifacts.dumps({"settings": {"token": ""}})
    assert merge_export(public, b"{}\n", ["settings.token"]) == public
