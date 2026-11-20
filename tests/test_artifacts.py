"""Artifact bytes: the stored encoding, and the digests of the committed files."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from support import REPO

from uiu import artifacts, spec
from uiu import store as stores


def test_committed_artifacts_round_trip_byte_for_byte():
    """The committed artifacts are the reference, so re-encoding each one checks
    indent, key order, escaping and the trailing newline at once.

    Only an app's own artifact is checked: a file beside `app.py` that is a
    payload for the app to import keeps whatever shape that app reads.
    """
    checked = 0
    for app_file in sorted((REPO / "policy/data").glob("*/app.py")):
        recipe = spec.parse(app_file).recipe
        if recipe is None or not recipe.artifact:
            continue
        path = app_file.parent / recipe.artifact
        if path.suffix != ".json":
            # A zip artifact is compared by member, not re-encoded as JSON.
            continue
        assert path.is_file(), path
        assert artifacts.dumps(json.loads(path.read_bytes())) == path.read_bytes(), path
        checked += 1
    assert checked


def test_go_escaping():
    body = artifacts.dumps({"a": "<&>", "b": "\u2028", "c": "é"})
    assert body == '{\n "a": "\\u003c\\u0026\\u003e",\n "b": "\\u2028",\n "c": "é"\n}\n'.encode()


FLOAT_CASES = [
    (1.0, "1"),
    (-0.0, "-0"),
    (1e-6, "0.000001"),
    (1e-5, "0.00001"),
    (1e16, "10000000000000000"),
    (8.049999999999999, "8.049999999999999"),
    (1e-7, "1e-7"),
    (1.5e-7, "1.5e-7"),
    (1e21, "1e+21"),
]


@pytest.mark.parametrize(("value", "text"), FLOAT_CASES)
def test_go_float_encoding(value, text):
    assert artifacts.dumps({"v": value}) == f'{{\n "v": {text}\n}}\n'.encode()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_raise(value):
    with pytest.raises(ValueError):
        artifacts.dumps({"v": value})


def test_go_control_escapes():
    # The stored format writes the five short control escapes, \u00xx for every
    # other byte below 0x20, the HTML trio, and U+2028.
    body = artifacts.dumps({"v": '\b\f"\\\x01\u2028<&>'})
    assert body == (b'{\n "v": "\\b\\f\\"\\\\\\u0001\\u2028\\u003c\\u0026\\u003e"\n}\n')


def test_empty_containers_stay_compact():
    assert artifacts.dumps({"a": {}, "b": []}) == b'{\n "a": {},\n "b": []\n}\n'


def test_lone_surrogate_becomes_replacement_character():
    value = json.loads('"\\ud800"')
    assert artifacts.dumps(value) == b'"\xef\xbf\xbd"\n'


def test_large_integers_stay_exact_unlike_go():
    """Known divergence: a decoder that turns every JSON number into a 64-bit
    float loses digits above 2^53. Python keeps big integers exact instead."""
    assert artifacts.dumps(json.loads("9007199254740993")) == b"9007199254740993\n"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_numbers_go_refuses_are_not_json(literal):
    body = f'{{"a": {literal}}}'.encode()
    with pytest.raises(ValueError):
        artifacts.loads(body)
    assert artifacts.canonical(body, [], []) == body
    assert artifacts.redact(body, ["a"]) == body


def test_sort_uses_compact_go_encoding():
    # The sort key is the compact stored encoding. A raw '<' is 0x3c and would
    # sort before "A", but the encoder escapes it to \u003c, which starts with
    # backslash 0x5c, so "A" (0x41) sorts first.
    body = b'{"apps": [{"k": "<"}, {"k": "A"}]}'
    assert artifacts.canonical(body, [], ["apps"]) == (
        b'{\n "apps": [\n  {\n   "k": "A"\n  },\n  {\n   "k": "\\u003c"\n  }\n ]\n}\n'
    )


def test_sort_keeps_utf8_like_go():
    # A non-ASCII letter sorts after ASCII by codepoint, so "z" (0x7a) comes
    # first. A \u00e9 escape would start with backslash and win instead.
    body = '{"apps": [{"name": "é"}, {"name": "z"}]}'.encode()
    assert json.loads(artifacts.canonical(body, [], ["apps"])) == {
        "apps": [{"name": "z"}, {"name": "é"}]
    }


def test_compact_form_has_no_separator_whitespace():
    # The sort key must be the compact stored bytes, not json.dumps with its
    # default ", " and ": " separators or a pretty encoder's newlines.
    assert artifacts._compact({"a": [1, {}], "b": []}) == '{"a":[1,{}],"b":[]}'


def test_digests_match_the_go_tool():
    expected = {
        "obtainium": "d19c7010033fd5269ddeeae7422bc96860de4b780eb02a61fbd51402901c12e5",
        "yamlauncher": "600ae3aaa52476844e201d7784092eb66a24169149c5e87d8052f14304d21526",
    }
    for app, digest in expected.items():
        recipe = spec.parse(REPO / "policy/data" / app / "app.py").recipe
        assert recipe is not None
        recipe.blob_dir = REPO / "policy/data" / app
        assert stores.artifact_digest(recipe) == digest, app


def test_canonical_masks_volatile_and_orders_lists():
    body = b'{"ts": 5, "apps": [{"id": "b"}, {"id": "a"}]}'
    assert artifacts.canonical(body, ["ts"], ["apps"]) == artifacts.dumps(
        {"ts": "<volatile>", "apps": [{"id": "a"}, {"id": "b"}]}
    )


def test_volatile_walks_lists():
    body = b'{"apps": [{"check": 1}, {"check": 2}]}'
    got = json.loads(artifacts.canonical(body, ["apps.check"], []))
    assert got == {"apps": [{"check": "<volatile>"}, {"check": "<volatile>"}]}


def test_volatile_does_not_create_a_missing_field():
    body = b'{"a": 1}'
    assert json.loads(artifacts.canonical(body, ["b.c"], [])) == {"a": 1}


def test_redact_zeroes_each_type():
    body = json.dumps({"s": "x", "i": 3, "b": True, "l": [1], "d": {"k": 1}}).encode()
    got = json.loads(artifacts.redact(body, ["s", "i", "b", "l", "d"]))
    assert got == {"s": "", "i": 0, "b": False, "l": [], "d": {}}


def test_non_json_is_its_own_canonical_form():
    body = b"not json at all"
    assert artifacts.canonical(body, ["a"], []) == body


def test_diff_masks_and_reports():
    old = artifacts.dumps({"ts": 1, "keep": "a"})
    new = artifacts.dumps({"ts": 2, "keep": "a"})
    assert artifacts.canonical(old, ["ts"], []) == artifacts.canonical(new, ["ts"], [])
    changed = artifacts.dumps({"ts": 2, "keep": "b"})
    text = artifacts.diff(
        artifacts.canonical(old, ["ts"], []), artifacts.canonical(changed, ["ts"], []), "demo.json"
    )
    assert '- "keep": "a"' in text
    assert '+ "keep": "b"' in text


def test_binary_diff_is_a_note():
    text = artifacts.diff(b"\xff\xfe", b"\xff\xfd", "demo.bin")
    assert text.startswith("binary artifacts differ:")


def make_zip(
    members: list[tuple[str, bytes]], date_time: tuple[int, int, int, int, int, int]
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=date_time)
            archive.writestr(info, data)
    return buffer.getvalue()


ZIP_MEMBERS = [("app/settings.json", b'{"a": 1}'), ("app/notes.txt", b"one\n")]


def test_zip_member_order_and_timestamps_are_not_drift():
    old = make_zip(ZIP_MEMBERS, (2020, 1, 1, 0, 0, 0))
    new = make_zip(list(reversed(ZIP_MEMBERS)), (2024, 6, 2, 3, 4, 6))
    assert old != new
    assert artifacts.canonical(old, [], []) == artifacts.canonical(new, [], [])


def test_zip_digest_is_stable_across_equivalent_archives():
    old = make_zip(ZIP_MEMBERS, (2020, 1, 1, 0, 0, 0))
    new = make_zip(list(reversed(ZIP_MEMBERS)), (2024, 6, 2, 3, 4, 6))
    assert artifacts.digest(artifacts.canonical(old, [], [])) == artifacts.digest(
        artifacts.canonical(new, [], [])
    )


@pytest.mark.parametrize(
    ("members", "name"),
    [
        ([("app/settings.json", b'{"a": 2}'), ("app/notes.txt", b"one\n")], "app/settings.json"),
        ([("app/settings.json", b'{"a": 1}'), ("app/extra.bin", b"x")], "app/extra.bin"),
    ],
)
def test_zip_content_change_names_the_member(members, name):
    old = make_zip(ZIP_MEMBERS, (2020, 1, 1, 0, 0, 0))
    new = make_zip(members, (2020, 1, 1, 0, 0, 0))
    old_form = artifacts.canonical(old, [], [])
    new_form = artifacts.canonical(new, [], [])
    assert old_form != new_form
    text = artifacts.diff(old_form, new_form, "demo.zip")
    assert f"-{name} " in text or f"+{name} " in text


def test_zip_canonical_form_is_one_line_per_member():
    body = make_zip(ZIP_MEMBERS, (2020, 1, 1, 0, 0, 0))
    lines = artifacts.canonical(body, ["ignored"], ["ignored"]).decode().splitlines()
    assert lines[0] == "zip"
    assert [line.split()[0] for line in lines[1:]] == ["app/notes.txt", "app/settings.json"]
    for line in lines[1:]:
        name, size, sha = line.split()
        assert size == str(len(dict(ZIP_MEMBERS)[name]))
        assert sha == hashlib.sha256(dict(ZIP_MEMBERS)[name]).hexdigest()


def test_zip_magic_without_an_archive_is_unchanged():
    body = b"PK\x03\x04 not really a zip"
    assert artifacts.canonical(body, ["a"], []) == body


def test_non_zip_bytes_are_unchanged():
    body = b"PK\x05\x06 barely anything"
    assert artifacts.canonical(body, ["a"], ["a.b"]) == body
