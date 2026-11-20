"""The app file: the facts it accepts, the rows it produces, what it refuses."""

from __future__ import annotations

import pytest
from support import VALID, app_file

from uiu import spec
from uiu.dsl import ensure, pull, push, shell, tap
from uiu.recipe import RecipeError


def test_build_accepts_the_documented_facts():
    app = spec.build(
        package="com.example.demo",
        install={
            "release": "https://example.invalid/releases/latest",
            "checksum": "asset",
            "assets": {"universal": "demo.apk"},
        },
        permissions={"android.permission.CAMERA": ("granted", "scan codes")},
        appops={"REQUEST_INSTALL_PACKAGES": ("allow", "self update")},
        roles={"android.app.role.HOME": "the launcher"},
        doze=("allow", "stay awake for alarms"),
        artifact="demo.json",
        backup=[tap(desc="Settings"), pull("/sdcard/Download/a.json", "{{artifact}}")],
        restore=[push("{{artifact}}", "/sdcard/Download/{{artifact}}")],
    )
    assert app.package == "com.example.demo"
    assert app.install is not None
    assert app.permissions == {"android.permission.CAMERA": ("granted", "scan codes")}
    assert app.appops == {"REQUEST_INSTALL_PACKAGES": ("allow", "self update")}
    assert app.roles == {"android.app.role.HOME": "the launcher"}
    assert app.doze == ("allow", "stay awake for alarms")
    assert app.recipe is not None
    assert app.recipe.artifact == "demo.json"


def test_rows_names_every_namespace():
    app = spec.build(
        package="com.example.demo",
        permissions={"android.permission.CAMERA": ("granted", "scan codes")},
        appops={"REQUEST_INSTALL_PACKAGES": ("allow", "self update")},
        roles={"android.app.role.HOME": "the launcher"},
        doze=("allow", "stay awake for alarms"),
    )
    assert app.rows() == [
        ("permissions", "com.example.demo:android.permission.CAMERA", "granted", "scan codes"),
        ("appops", "com.example.demo:REQUEST_INSTALL_PACKAGES", "allow", "self update"),
        ("role", "android.app.role.HOME", "com.example.demo", "the launcher"),
        ("deviceidle", "com.example.demo", "allow", "stay awake for alarms"),
    ]


def test_a_policy_only_app_has_no_recipe_and_no_rows():
    app = spec.build(package="com.example.demo")
    assert app.recipe is None
    assert app.rows() == []


def test_a_permission_without_a_reason_is_refused():
    with pytest.raises(RecipeError, match="needs a reason"):
        spec.build(
            package="com.example.demo", permissions={"android.permission.CAMERA": ("granted", " ")}
        )


def test_a_permission_with_an_unknown_value_is_refused():
    with pytest.raises(RecipeError, match="is one of granted, revoked"):
        spec.build(
            package="com.example.demo", permissions={"android.permission.CAMERA": ("allow", "x")}
        )


def test_a_permission_name_with_a_space_is_refused():
    with pytest.raises(RecipeError, match="letters, digits, dot, asterisk"):
        spec.build(package="com.example.demo", permissions={"a b": ("granted", "x")})


def test_a_permission_value_must_be_the_whole_word():
    with pytest.raises(RecipeError, match="has invalid value"):
        spec.build(
            package="com.example.demo", permissions={"android.permission.CAMERA": ("grantedX", "x")}
        )


def test_a_role_name_must_end_at_the_role():
    with pytest.raises(RecipeError, match="android.app.role name"):
        spec.build(
            package="com.example.demo", roles={"android.app.role.HOME.extra": "the launcher"}
        )


def test_an_invalid_package_id_is_refused():
    for package in ("", "demo", "com.example/app", 1):
        with pytest.raises(RecipeError):
            spec.build(package=package)


def test_an_unknown_install_field_is_refused():
    with pytest.raises(RecipeError, match='install: unknown field "nope"'):
        spec.build(package="com.example.demo", install={"nope": 1})


def test_roles_must_be_android_app_role_names():
    with pytest.raises(RecipeError, match="android.app.role name"):
        spec.build(package="com.example.demo", roles={"HOME": "the launcher"})


def test_a_role_without_a_reason_is_refused():
    with pytest.raises(RecipeError, match="needs a reason"):
        spec.build(package="com.example.demo", roles={"android.app.role.HOME": ""})


def test_doze_is_allow_or_ignore():
    with pytest.raises(RecipeError, match="allow, ignore"):
        spec.build(package="com.example.demo", doze=("yes", "why"))


@pytest.mark.parametrize("name", ["Not Normalized", "Under_score", ".hidden", "a b"])
def test_parse_refuses_a_folder_that_is_not_normalized(tmp_path, name):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "app.py").write_text(
        "from uiu.spec import build\nAPP = build(package='com.example.demo')\n"
    )
    with pytest.raises(RecipeError, match="not a normalized app name"):
        spec.parse(directory / "app.py")


def test_parse_refuses_a_file_not_named_app_py(tmp_path):
    path = tmp_path / "policy.py"
    path.write_text("from uiu.spec import build\nAPP = build(package='com.example.demo')\n")
    with pytest.raises(RecipeError, match="must be named app.py"):
        spec.parse(path)


def test_parse_refuses_a_file_without_app(tmp_path):
    path = tmp_path / "demo" / "app.py"
    path.parent.mkdir()
    path.write_text("PACKAGE = 'com.example.demo'\n")
    with pytest.raises(RecipeError) as caught:
        spec.parse(path)
    assert str(path) in str(caught.value)
    assert "set APP = app" in str(caught.value)


def test_parse_refuses_an_app_that_is_not_an_app(tmp_path):
    path = tmp_path / "demo" / "app.py"
    path.parent.mkdir()
    path.write_text("APP = {'package': 'com.example.demo'}\n")
    with pytest.raises(RecipeError) as caught:
        spec.parse(path)
    assert str(path) in str(caught.value)
    assert "set APP = app" in str(caught.value)


def test_parse_sets_the_name_and_directory(tmp_path):
    path = tmp_path / "demo" / "app.py"
    path.parent.mkdir()
    path.write_text("from uiu.spec import build\nAPP = build(package='com.example.demo')\n")
    app = spec.parse(path)
    assert app.name == "demo"
    assert app.dir == path.parent
    assert app.path == path


def test_an_app_file_round_trips_every_fact_through_json(tmp_path):
    path = tmp_path / "demo" / "app.py"
    path.parent.mkdir()
    path.write_text(
        app_file(
            {
                **VALID,
                "install": {
                    "release": "https://example.invalid/releases/latest",
                    "checksum": "asset",
                    "assets": {"universal": "demo.apk"},
                },
                "permissions": {"android.permission.CAMERA": ["granted", "scan codes"]},
                "appops": {"REQUEST_INSTALL_PACKAGES": ["allow", "self update"]},
                "roles": {"android.app.role.HOME": "the launcher"},
                "doze": ["allow", "stay awake for alarms"],
            }
        )
    )
    app = spec.parse(path)
    assert app.permissions == {"android.permission.CAMERA": ("granted", "scan codes")}
    assert app.appops == {"REQUEST_INSTALL_PACKAGES": ("allow", "self update")}
    assert app.roles == {"android.app.role.HOME": "the launcher"}
    assert app.doze == ("allow", "stay awake for alarms")
    assert app.install is not None
    assert app.recipe is not None


def write_app(tmp_path, body: str):
    path = tmp_path / "demo" / "app.py"
    path.parent.mkdir()
    path.write_text(body)
    return spec.parse(path)


def test_an_app_file_with_a_setup_walk_parses(tmp_path):
    app = write_app(
        tmp_path,
        "from uiu.dsl import app, shell\n"
        "APP = app(package='com.example.demo', setup=[shell('cmd statusbar add-tile x')])\n",
    )
    assert app.recipe is not None
    assert len(app.recipe.setup) == 1
    assert app.recipe.setup[0].command == "cmd statusbar add-tile x"
    assert app.recipe.artifact == ""


def test_an_app_file_with_only_setup_still_has_a_recipe(tmp_path):
    app = write_app(
        tmp_path,
        "from uiu.dsl import app, ensure\n"
        "APP = app(package='com.example.demo', setup=[ensure(True, text='X')])\n",
    )
    assert app.recipe is not None
    assert app.recipe.backup == []
    assert app.recipe.restore == []
    assert app.recipe.setup[0].kind == "ensure"


def test_build_accepts_a_setup_walk():
    app = spec.build(
        package="com.example.demo",
        setup=[ensure(True, text="X"), shell("cmd statusbar add-tile x")],
    )
    assert app.recipe is not None
    assert [step.kind for step in app.recipe.setup] == ["ensure", "shell"]


def test_a_shell_step_with_an_empty_command_is_refused(tmp_path):
    path = tmp_path / "demo" / "app.py"
    path.parent.mkdir()
    path.write_text(
        "from uiu.dsl import app, shell\nAPP = app(package='com.example.demo', setup=[shell('')])\n"
    )
    with pytest.raises(RecipeError, match="shell needs command"):
        spec.parse(path)


def test_an_app_file_with_an_if_absent_block_parses(tmp_path):
    app = write_app(
        tmp_path,
        "from uiu.dsl import app, if_absent, tap\n"
        "APP = app(\n"
        "    package='com.example.demo',\n"
        "    setup=[if_absent(steps=[tap(text='A')], text='Missing')],\n"
        ")\n",
    )
    assert app.recipe is not None
    assert len(app.recipe.setup) == 1
    step = app.recipe.setup[0]
    assert step.kind == "if_absent"
    assert len(step.steps) == 1
    assert step.steps[0].kind == "tap"
