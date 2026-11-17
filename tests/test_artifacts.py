"""Artifact bytes: the stored encoding, and the digests of the committed files."""

from __future__ import annotations

import json

import pytest
from support import REPO

from uiu import artifacts
from uiu import recipe as recipes
from uiu import store as stores


def test_committed_artifacts_round_trip_byte_for_byte():
    """The committed artifacts are the reference, so re-encoding each one checks
    indent, key order, escaping and the trailing newline at once."""
    paths = sorted(
        path for path in (REPO / "policy/data").glob("*/*.json") if path.name != recipes.STEPS_FILE
    )
    assert paths
    for path in paths:
        assert artifacts.dumps(json.loads(path.read_bytes())) == path.read_bytes(), path


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
        "obtainium": "26e0e4aa5b64d394b6b8ae5685f9618b07b457273254592df0c0f70a2675ec47",
        "yamlauncher": "600ae3aaa52476844e201d7784092eb66a24169149c5e87d8052f14304d21526",
    }
    for app, digest in expected.items():
        recipe = recipes.parse(REPO / "policy/data" / app / recipes.STEPS_FILE)
        recipe.blob_dir = recipe.dir
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
