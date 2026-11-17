"""Recipe parsing and validation: the rejections the schema promises."""

from __future__ import annotations

import copy
import json

import pytest
from support import REPO, VALID, recipe_from, steps_file

from uiu import recipe as recipes
from uiu.recipe import RecipeError, Selector, load_private, parse


def test_all_committed_recipes_parse():
    for app in ("obtainium", "yamlauncher", "shizucallrecorder"):
        recipe = parse(REPO / "policy/data" / app / recipes.STEPS_FILE)
        assert recipe.name == app
        assert recipe.package


def test_private_list_is_loaded_from_the_private_tree(tmp_path):
    recipe = recipe_from({**VALID, "private": "private.json"})
    recipe.data_dir = tmp_path
    (tmp_path / "private.json").write_text(json.dumps(["data.token"]))
    loaded = load_private(recipe)
    assert loaded.private == ["data.token"]
    assert loaded.private_file == "private.json"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "reading"),
        ("[]", "no selectors"),
        ('{"a": 1}', "a list of selectors"),
    ],
)
def test_broken_private_file_is_fatal(tmp_path, content, message):
    recipe = recipe_from({**VALID, "private": "private.json"})
    recipe.data_dir = tmp_path
    if content is not None:
        (tmp_path / "private.json").write_text(content)
    with pytest.raises(RecipeError, match=message):
        load_private(recipe)


def test_placeholders_expand_in_every_field():
    recipe = recipe_from(
        {
            **VALID,
            "backup": [
                {"type": {"selector": {"text": "{{artifact}}"}, "text": "{{package}}"}},
                {"pull": {"from": "/sdcard/Download/demo.json", "to": "{{artifact}}"}},
            ],
        }
    )
    expanded = recipe.expand()
    assert expanded.backup[0].text == "com.example.demo"
    assert expanded.backup[0].selector is not None
    assert expanded.backup[0].selector.text == "demo.json"
    assert expanded.backup[1].target == "demo.json"


def test_expansion_does_not_mutate_the_recipe():
    recipe = recipe_from(VALID)
    recipe.expand()
    assert recipe.backup[1].target == "{{artifact}}"


def test_wait_and_scroll_defaults():
    step = json.loads('{"wait": {"selector": {"desc": "x"}}}')
    assert (
        recipe_from({**VALID, "backup": [step, {"pull": {"from": "/sdcard/a", "to": "demo.json"}}]})
        .backup[0]
        .timeout
        == 10
    )
    scroll = json.loads('{"scroll": {"direction": "down"}}')
    body = {**VALID, "backup": [scroll, *VALID["backup"]]}
    assert recipe_from(body).backup[0].times == 1


def mutate(**changes):
    body = copy.deepcopy(VALID)
    body.update(changes)
    return body


STEP_CASES = [
    ("unknown key", {"backup": [{"press": {}}]}, 'unknown key "press"'),
    (
        "two actions",
        {"backup": [{"launch": {"component": "a/.b"}, "sleep": {"seconds": 1}}]},
        "two actions",
    ),
    ("no action key", {"backup": [{}]}, "no action key"),
    ("not an object", {"backup": ["tap"]}, "not an object"),
    (
        "unknown argument",
        {"backup": [{"tap": {"selector": {"text": "x"}, "wait": 1}}]},
        'unknown key "wait"',
    ),
    (
        "wrong type",
        {"backup": [{"tap": {"selector": {"text": "x"}, "duration": "800"}}]},
        "a whole number",
    ),
    (
        "unknown selector key",
        {"backup": [{"tap": {"selector": {"name": "x"}}}]},
        'unknown selector key "name"',
    ),
    ("empty selector", {"backup": [{"tap": {"selector": {}}}]}, "selector names no field"),
    (
        "negative index",
        {"backup": [{"tap": {"selector": {"text": "x", "index": -1}}}]},
        "index is negative",
    ),
    (
        "negative duration",
        {"backup": [{"tap": {"selector": {"text": "x"}, "duration": -1}}]},
        "duration is negative",
    ),
    ("tap without selector", {"backup": [{"tap": {}}]}, "tap needs selector"),
    (
        "optional on a wait",
        {"backup": [{"wait": {"selector": {"text": "x"}, "optional": True}}]},
        "not an argument of wait",
    ),
    ("launch without component", {"backup": [{"launch": {}}]}, "launch needs component"),
    (
        "launch without activity",
        {"backup": [{"launch": {"component": "com.example"}}]},
        "not package/activity",
    ),
    ("type without text", {"backup": [{"type": {}}]}, "type needs text"),
    (
        "assert without checked",
        {"backup": [{"assert": {"selector": {"text": "x"}}}]},
        "assert needs checked",
    ),
    ("bad key code", {"backup": [{"key": {"code": "KEY CODE"}}]}, "invalid key code"),
    ("bad direction", {"backup": [{"scroll": {"direction": "left"}}]}, "not up or down"),
    ("negative times", {"backup": [{"scroll": {"direction": "up", "times": -2}}]}, "at least 1"),
    ("zero sleep", {"backup": [{"sleep": {"seconds": 0}}]}, "must be positive"),
    ("pull without to", {"backup": [{"pull": {"from": "/sdcard/a"}}]}, "needs from and to"),
    (
        "newest on a push",
        {"restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/a", "newest": True}}]},
        "not an argument of push",
    ),
    (
        "absolute local path",
        {"backup": [{"pull": {"from": "/sdcard/a", "to": "/tmp/a"}}]},
        "must be relative",
    ),
    (
        "parent directory",
        {"backup": [{"pull": {"from": "/sdcard/a", "to": "../a"}}]},
        "leaves the export directory",
    ),
    (
        "writing the recipe",
        {"backup": [{"pull": {"from": "/sdcard/a", "to": "steps.py"}}]},
        "would overwrite the recipe",
    ),
    (
        "newest off sdcard",
        {"backup": [{"pull": {"from": "/data/local/tmp/a-*", "to": "demo.json", "newest": True}}]},
        "under /sdcard",
    ),
    ("bad capture name", {"backup": [{"capture": {"name": "a b"}}]}, "invalid capture name"),
    (
        "push in backup",
        {"backup": [*VALID["backup"], {"push": {"from": "x", "to": "/sdcard/x"}}]},
        "push belongs in restore",
    ),
    (
        "pull in restore",
        {"restore": [*VALID["restore"], {"pull": {"from": "/sdcard/x", "to": "x"}}]},
        "pull belongs in backup",
    ),
]


@pytest.mark.parametrize(
    ("name", "change", "message"), STEP_CASES, ids=[case[0] for case in STEP_CASES]
)
def test_step_rejections(name, change, message):
    with pytest.raises(RecipeError, match=message):
        recipe_from(mutate(**change))


RECIPE_CASES = [
    ("empty backup", {"backup": []}, "backup is empty"),
    (
        "artifact without restore",
        {"restore": [], "backup": [{"capture": {"name": "main"}}]},
        "no restore flow",
    ),
    (
        "empty restore with a pull",
        {
            "restore": [],
            "artifact": "",
            "backup": [{"pull": {"from": "/sdcard/a", "to": "x.json"}}],
        },
        "restore is empty",
    ),
    (
        "neither pull nor capture",
        {"artifact": "", "restore": [], "backup": [{"launch": {"component": "a/.b"}}]},
        "neither exports nor captures",
    ),
    (
        "restore without artifact",
        {
            "artifact": "",
            "backup": [{"launch": {"component": "a/.b"}}],
            "restore": [{"launch": {"component": "a/.b"}}],
        },
        "restore is set but artifact is empty",
    ),
    (
        "backup does not pull it",
        {"backup": [{"pull": {"from": "/sdcard/a", "to": "other.json"}}]},
        "never pulls the artifact",
    ),
    (
        "restore does not push it",
        {"restore": [{"push": {"from": "other.json", "to": "/sdcard/a"}}]},
        "never pushes the artifact",
    ),
    (
        "private on a binary",
        {"artifact": "demo.bin", "private": ["a"]},
        "private fields need a JSON artifact",
    ),
    ("redacted and private", {"private": ["a"], "redact": ["a"]}, "both redacted and private"),
    ("private without json name", {"private": "private.txt"}, "a JSON file"),
    ("private outside the tree", {"private": "sub/private.json"}, "bare name"),
    ("bad package", {"package": "com.example/app"}, "invalid package"),
    ("bad artifact", {"artifact": "a b.json"}, "invalid artifact"),
    ("bad volatile", {"volatile": ["a/b"]}, "invalid volatile field"),
    ("bad sort", {"sort": [" a"]}, "invalid sort field"),
    ("bad private field", {"private": ["a b"]}, "invalid private field"),
    ("bad private selector", {"private": ["apps[id]"]}, "identity selector"),
]


@pytest.mark.parametrize(
    ("name", "change", "message"), RECIPE_CASES, ids=[case[0] for case in RECIPE_CASES]
)
def test_recipe_rejections(name, change, message):
    with pytest.raises(RecipeError, match=message):
        recipe_from(mutate(**change))


def test_a_recipe_that_does_not_set_recipe_is_refused(tmp_path):
    path = tmp_path / "steps.py"
    path.write_text("PACKAGE = 'com.example.demo'\n")
    with pytest.raises(RecipeError, match="set RECIPE"):
        parse(path)


def test_a_recipe_that_raises_names_the_python_error(tmp_path):
    path = tmp_path / "steps.py"
    path.write_text("raise ValueError('boom')\n")
    with pytest.raises(RecipeError, match="ValueError: boom"):
        parse(path)


def test_recipe_must_be_named_steps_py(tmp_path):
    path = tmp_path / "recipe.py"
    path.write_text(steps_file())
    with pytest.raises(RecipeError, match="must be named steps.py"):
        parse(path)


def test_folder_must_be_a_normalized_name(tmp_path):
    directory = tmp_path / "Not Normalized"
    directory.mkdir()
    path = directory / recipes.STEPS_FILE
    path.write_text(steps_file())
    with pytest.raises(RecipeError, match="normalized app name"):
        parse(path)


def test_selector_string_matches_probe_syntax():
    selector = Selector(desc="Settings", class_name="android.widget.Button", index=2)
    assert str(selector) == "{desc=Settings, class=android.widget.Button, index=2}"


def test_private_file_selectors_are_validated_like_inline_ones(tmp_path):
    recipe = recipe_from({**VALID, "private": "private.json"})
    recipe.data_dir = tmp_path
    (tmp_path / "private.json").write_text('["apps[id]"]')
    with pytest.raises(RecipeError, match="identity selector"):
        load_private(recipe)


def test_private_file_selector_cannot_also_be_redacted(tmp_path):
    recipe = recipe_from({**VALID, "private": "private.json", "redact": ["data.token"]})
    recipe.data_dir = tmp_path
    (tmp_path / "private.json").write_text('["data.token"]')
    with pytest.raises(RecipeError, match="both redacted and private"):
        load_private(recipe)


def test_a_duplicate_private_selector_is_refused():
    with pytest.raises(RecipeError, match="listed twice"):
        recipe_from({**VALID, "private": ["data.token", "data.token"]})


def test_an_argument_from_another_action_is_refused():
    with pytest.raises(RecipeError, match='key "seconds" is not an argument of tap'):
        recipe_from({"backup": [{"tap": {"selector": {"text": "x"}, "seconds": 3}}]})


def test_a_negative_wait_timeout_is_refused():
    with pytest.raises(RecipeError, match="wait timeout must be positive"):
        recipe_from({**VALID, "backup": [{"wait": {"selector": {"text": "x"}, "timeout": -1}}]})


def test_a_newest_glob_cannot_inject_shell_syntax():
    with pytest.raises(RecipeError, match="plain glob"):
        recipe_from(
            {
                **VALID,
                "backup": [
                    {
                        "pull": {
                            "from": "/sdcard/a;rm -rf /*",
                            "to": "demo.json",
                            "newest": True,
                        }
                    }
                ],
            }
        )


def test_a_recipe_must_be_utf8(tmp_path):
    path = tmp_path / "steps.py"
    path.write_bytes(b'RECIPE = "\xff"\n')
    with pytest.raises(RecipeError, match="not UTF-8"):
        parse(path)
