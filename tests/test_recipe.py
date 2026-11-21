"""Recipe parsing and validation: the rejections the schema promises."""

from __future__ import annotations

import copy
import json

import pytest
from support import ENVELOPE, REPO, VALID, app_file, recipe_from

from uiu import recipe as recipes
from uiu import spec
from uiu.dsl import if_absent, push, scroll, tap, wait
from uiu.recipe import RecipeError, Selector, load_private, validate_step
from uiu.vault import Vault


def test_all_committed_apps_with_a_walk_parse():
    for name in ("obtainium", "yamlauncher", "shizucallrecorder"):
        app = spec.parse(REPO / "policy/data" / name / recipes.APP_FILE)
        assert app.name == name
        assert app.package
        assert app.recipe is not None


def test_private_list_is_loaded_from_the_private_tree(tmp_path):
    recipe = recipe_from({**VALID, "private": "private.json"})
    recipe.data_dir = tmp_path
    (tmp_path / "private.json").write_text(json.dumps(["data.token"]))
    loaded = load_private(recipe)
    assert loaded.private == ["data.token"]
    assert loaded.private_file == "private.json"


def test_private_list_is_loaded_from_an_envelope(tmp_path):
    root = tmp_path / "secrets"
    directory = root / "demo"
    directory.mkdir(parents=True)
    (directory / "private.json").write_bytes(ENVELOPE)
    recipe = recipe_from({**VALID, "private": "private.json"})
    recipe.dir = tmp_path / "apps" / "demo"
    recipe.data_dir = directory
    recipe.vault = Vault(
        root,
        identity=lambda: "AGE-SECRET-KEY-1",
        run=lambda argv, body: json.dumps(["data.token"]).encode(),
    )

    loaded = load_private(recipe)

    assert loaded.private == ["data.token"]


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
        "writing the app file",
        {"backup": [{"pull": {"from": "/sdcard/a", "to": "app.py"}}]},
        "would overwrite the app file",
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


PUSH_ONLY = {
    "package": "com.example.demo",
    "artifact": "payload.zip",
    "restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/Download/payload.zip"}}],
}


def test_a_recipe_that_only_pushes_a_stored_artifact_is_valid():
    recipe = recipe_from(PUSH_ONLY)
    assert recipe.backup == []
    assert recipe.artifact == "payload.zip"
    assert [step.kind for step in recipe.restore] == ["push"]


def test_a_push_only_recipe_must_push_the_artifact():
    with pytest.raises(RecipeError, match="never pushes the artifact"):
        recipe_from({**PUSH_ONLY, "restore": [{"push": {"from": "other.zip", "to": "/sdcard/a"}}]})


def test_a_restore_without_artifact_or_backup_is_refused():
    with pytest.raises(RecipeError, match="restore is set but artifact is empty"):
        recipe_from(
            {
                "package": "com.example.demo",
                "restore": [{"push": {"from": "other.zip", "to": "/sdcard/a"}}],
            }
        )


RECIPE_CASES = [
    ("empty backup", {"backup": [], "artifact": "", "restore": []}, "backup is empty"),
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
    ("bad omit", {"omit": ["a/b"]}, "invalid omit field"),
    ("bad sort", {"sort": [" a"]}, "invalid sort field"),
    ("bad private field", {"private": ["a b"]}, "invalid private field"),
    ("bad private selector", {"private": ["apps[id]"]}, "identity selector"),
    (
        "push of an absolute payload",
        {"restore": [{"push": {"from": "/tmp/payload.json", "to": "/sdcard/a"}}]},
        "must be relative",
    ),
    (
        "push of a payload outside the tree",
        {"restore": [{"push": {"from": "../payload.json", "to": "/sdcard/a"}}]},
        "leaves the export directory",
    ),
    (
        "push of the app file",
        {"restore": [{"push": {"from": "app.py", "to": "/sdcard/a"}}]},
        "would overwrite the app file",
    ),
]


@pytest.mark.parametrize(
    ("name", "change", "message"), RECIPE_CASES, ids=[case[0] for case in RECIPE_CASES]
)
def test_recipe_rejections(name, change, message):
    with pytest.raises(RecipeError, match=message):
        recipe_from(mutate(**change))


def test_an_app_file_without_app_is_refused(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("PACKAGE = 'com.example.demo'\n")
    with pytest.raises(RecipeError, match="set APP = app"):
        spec.parse(path)


def test_an_app_file_that_raises_names_the_python_error(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("raise ValueError('boom')\n")
    with pytest.raises(RecipeError, match="ValueError: boom"):
        spec.parse(path)


def test_an_app_file_must_be_named_app_py(tmp_path):
    path = tmp_path / "recipe.py"
    path.write_text(app_file())
    with pytest.raises(RecipeError, match="must be named app.py"):
        spec.parse(path)


def test_folder_must_be_a_normalized_name(tmp_path):
    directory = tmp_path / "Not Normalized"
    directory.mkdir()
    path = directory / recipes.APP_FILE
    path.write_text(app_file())
    with pytest.raises(RecipeError, match="normalized app name"):
        spec.parse(path)


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


WAIT_OPTIONS = [
    ("absent", {"absent": True}, "absent", True),
    ("checked", {"checked": True}, "checked", True),
]


@pytest.mark.parametrize(
    ("name", "options", "field", "value"), WAIT_OPTIONS, ids=[case[0] for case in WAIT_OPTIONS]
)
def test_wait_accepts_absent_or_checked(name, options, field, value):
    step = wait(text="Dark theme", **options)
    assert step.kind == "wait"
    assert step.selector is not None
    assert step.selector.text == "Dark theme"
    assert getattr(step, field) is value
    validate_step(step)


WAIT_REJECTIONS = [
    (
        "absent and checked",
        {"text": "Dark theme", "absent": True, "checked": True},
        "wait is either absent or checked, not both",
    ),
    ("checked without a selector", {"checked": True}, "wait needs selector"),
    ("absent without a selector", {"absent": True}, "wait needs selector"),
]


@pytest.mark.parametrize(
    ("name", "options", "message"), WAIT_REJECTIONS, ids=[case[0] for case in WAIT_REJECTIONS]
)
def test_wait_rejections(name, options, message):
    with pytest.raises(RecipeError, match=message):
        validate_step(wait(**options))


def test_a_wait_reads_absent_and_checked_from_the_file():
    recipe = recipe_from(
        {
            "package": "com.example.demo",
            "setup": [
                {"wait": {"absent": True, "selector": {"text": "Dialog"}}},
                {"wait": {"checked": True, "selector": {"text": "Dark theme"}}},
            ],
        }
    )
    assert recipe.setup[0].absent is True
    assert recipe.setup[0].checked is None
    assert recipe.setup[1].absent is False
    assert recipe.setup[1].checked is True


def test_scroll_until_is_a_selector():
    step = scroll(times=3, until={"text": "Target"})
    assert step.times == 3
    assert step.selector is not None
    assert step.selector.text == "Target"
    validate_step(step)


def test_a_scroll_reads_its_target_from_the_file():
    recipe = recipe_from(
        {
            "package": "com.example.demo",
            "setup": [{"scroll": {"direction": "down", "selector": {"text": "Target"}}}],
        }
    )
    assert recipe.setup[0].selector is not None
    assert recipe.setup[0].selector.text == "Target"


SCROLL_REJECTIONS = [
    ("unknown until key", {"until": {"name": "Target"}}, 'unknown selector key "name"'),
    ("until is not an object", {"until": "Target"}, "an object of selector keys"),
]


@pytest.mark.parametrize(
    ("name", "options", "message"), SCROLL_REJECTIONS, ids=[case[0] for case in SCROLL_REJECTIONS]
)
def test_scroll_rejections(name, options, message):
    with pytest.raises(RecipeError, match=message):
        scroll(**options)


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


SHELL_CASES = [
    ("empty command", "", "shell needs command"),
    ("blank command", "   ", "shell command is empty"),
    ("two lines", "ime set a\ncmd statusbar add-tile b", "shell command spans lines"),
]


@pytest.mark.parametrize(
    ("name", "command", "message"), SHELL_CASES, ids=[c[0] for c in SHELL_CASES]
)
def test_shell_command_rejections(name, command, message):
    with pytest.raises(RecipeError, match=message):
        recipe_from({"package": "com.example.demo", "setup": [{"shell": {"command": command}}]})


def test_shell_expands_the_package_placeholder():
    recipe = recipe_from(
        {
            "package": "com.example.demo",
            "setup": [{"shell": {"command": "ime set {{package}}/.Ime"}}],
        }
    )
    assert recipe.expand().setup[0].command == "ime set com.example.demo/.Ime"


def test_a_recipe_with_only_setup_steps_is_valid():
    recipe = recipe_from(
        {
            "package": "com.example.demo",
            "setup": [
                {"shell": {"command": "cmd statusbar add-tile x"}},
                {"assert": {"selector": {"text": "On"}, "checked": True}},
            ],
        }
    )
    assert [step.kind for step in recipe.setup] == ["shell", "assert"]
    assert recipe.artifact == ""
    assert recipe.backup == []


def test_ensure_is_a_valid_step():
    recipe = recipe_from(
        {
            "package": "com.example.demo",
            "setup": [{"ensure": {"checked": True, "selector": {"text": "Dark theme"}}}],
        }
    )
    step = recipe.setup[0]
    assert step.kind == "ensure"
    assert step.checked is True
    assert step.selector is not None
    assert step.selector.text == "Dark theme"


def test_an_if_absent_block_holds_its_children():
    step = if_absent(steps=[tap(text="A"), tap(text="B")], text="Missing")
    validate_step(step)
    assert step.kind == "if_absent"
    assert step.selector is not None
    assert step.selector.text == "Missing"
    assert [child.kind for child in step.steps] == ["tap", "tap"]


IF_ABSENT_REJECTIONS = [
    (
        "no children",
        if_absent(steps=[], text="Missing"),
        "if_absent needs at least one step",
    ),
    (
        "no selector",
        if_absent(steps=[tap(text="A")]),
        "if_absent needs selector",
    ),
    (
        "a push child",
        if_absent(steps=[push("payload.json", "/sdcard/Download/payload.json")], text="Missing"),
        "if_absent may not contain push",
    ),
    (
        "a nested block",
        if_absent(steps=[if_absent(steps=[tap(text="A")], text="Inner")], text="Missing"),
        "if_absent may not contain if_absent",
    ),
    (
        "a bad child",
        if_absent(steps=[tap(text="A"), tap(text="B"), tap()], text="Missing"),
        "if_absent step 3: tap needs selector",
    ),
]


@pytest.mark.parametrize(
    ("name", "step", "message"),
    IF_ABSENT_REJECTIONS,
    ids=[case[0] for case in IF_ABSENT_REJECTIONS],
)
def test_if_absent_rejections(name, step, message):
    with pytest.raises(RecipeError, match=message):
        validate_step(step)


def test_placeholders_expand_inside_an_if_absent_block():
    recipe = recipe_from(
        {
            "package": "com.example.demo",
            "setup": [
                {
                    "if_absent": {
                        "selector": {"text": "{{package}}"},
                        "steps": [{"tap": {"selector": {"text": "{{package}}/.Settings"}}}],
                    }
                }
            ],
        }
    )
    step = recipe.expand().setup[0]
    assert step.selector is not None
    assert step.selector.text == "com.example.demo"
    assert step.steps[0].selector is not None
    assert step.steps[0].selector.text == "com.example.demo/.Settings"


def test_an_if_absent_block_is_read_from_the_file():
    recipe = recipe_from(
        {
            "package": "com.example.demo",
            "setup": [
                {
                    "if_absent": {
                        "selector": {"text": "Missing"},
                        "steps": [
                            {"tap": {"selector": {"text": "A"}}},
                            {"tap": {"selector": {"text": "B"}}},
                        ],
                    }
                }
            ],
        }
    )
    step = recipe.setup[0]
    assert step.kind == "if_absent"
    assert [child.kind for child in step.steps] == ["tap", "tap"]
    assert step.steps[1].selector is not None
    assert step.steps[1].selector.text == "B"


ENSURE_CASES = [
    ("no selector", {"checked": True}, "ensure needs selector"),
    ("no checked", {"selector": {"text": "Dark theme"}}, "ensure needs checked"),
]


@pytest.mark.parametrize(
    ("name", "args", "message"), ENSURE_CASES, ids=[c[0] for c in ENSURE_CASES]
)
def test_ensure_rejections(name, args, message):
    with pytest.raises(RecipeError, match=message):
        recipe_from({"package": "com.example.demo", "setup": [{"ensure": args}]})


def test_an_app_file_must_be_utf8(tmp_path):
    path = tmp_path / "app.py"
    path.write_bytes(b'APP = "\xff"\n')
    with pytest.raises(RecipeError, match="not UTF-8"):
        spec.parse(path)
