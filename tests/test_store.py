"""Discovery: which recipe is which app, and which tree holds its bytes."""

from __future__ import annotations

import json
import stat
from datetime import datetime

import pytest
from support import VALID, recipe_from, steps_file

from uiu import artifacts
from uiu import store as stores
from uiu.recipe import parse
from uiu.store import Store, StoreError, write_new_file


def make_tree(root, *, private: bool = False) -> Store:
    apps = root / "apps"
    secrets = root / "secrets"
    (apps / "demo").mkdir(parents=True)
    (apps / "demo" / "steps.py").write_text(steps_file())
    if private:
        (secrets / "demo").mkdir(parents=True)
        (secrets / "demo" / "private.json").write_text(json.dumps(["token"]))
        (apps / "demo" / "steps.py").write_text(steps_file({**VALID, "private": "private.json"}))
    else:
        secrets.mkdir()
    return Store(apps=apps, secrets=secrets, state=root / "state")


def test_a_public_recipe_keeps_its_artifact_in_the_private_tree(tmp_path):
    store = make_tree(tmp_path)
    recipes, errors = store.all()

    assert errors == []
    assert [recipe.name for recipe in recipes] == ["demo"]
    assert recipes[0].blob_dir == tmp_path / "secrets" / "demo"
    assert recipes[0].data_dir == tmp_path / "secrets" / "demo"


def test_a_private_recipe_commits_its_artifact_beside_the_recipe(tmp_path):
    store = make_tree(tmp_path, private=True)
    recipes, errors = store.all()

    assert errors == []
    assert recipes[0].blob_dir == tmp_path / "apps" / "demo"
    assert recipes[0].private_path() == tmp_path / "secrets" / "demo" / "demo.private.json"


def test_load_finds_a_recipe_by_package(tmp_path):
    store = make_tree(tmp_path)
    assert store.load("com.example.demo").name == "demo"
    with pytest.raises(StoreError, match="no recipe declares"):
        store.load("com.example.other")
    with pytest.raises(StoreError, match="not a package name"):
        store.load("../demo")


def test_two_recipes_for_one_package_are_refused(tmp_path):
    store = make_tree(tmp_path)
    other = tmp_path / "apps" / "copy"
    other.mkdir()
    (other / "steps.py").write_text(steps_file())

    with pytest.raises(StoreError, match="more than one recipe"):
        store.load("com.example.demo")


def test_export_root_inside_the_recipe_tree_is_refused(tmp_path):
    store = Store(
        apps=tmp_path / "apps", secrets=tmp_path / "apps" / "secrets", state=tmp_path / "state"
    )
    with pytest.raises(StoreError, match="inside the recipe tree"):
        store.data_root()


def test_missing_secrets_tree_says_how_to_fix_it(tmp_path):
    store = Store(apps=tmp_path / "apps", secrets=tmp_path / "secrets", state=tmp_path / "state")
    (tmp_path / "apps" / "demo").mkdir(parents=True)
    (tmp_path / "apps" / "demo" / "steps.py").write_text(steps_file())

    with pytest.raises(StoreError, match="clone the secrets repository"):
        store.load("com.example.demo")


def test_state_of_reports_the_private_half(tmp_path):
    store = make_tree(tmp_path, private=True)
    recipe = store.load("com.example.demo")

    assert store.state_of(recipe) == "missing no private half"

    recipe.artifact_path().write_bytes(b"{}")
    assert store.state_of(recipe) == "stored no private half"

    recipe.private_path().parent.mkdir(parents=True, exist_ok=True)
    recipe.private_path().write_bytes(b"{}\n")
    assert store.state_of(recipe) == "stored +private"


def test_undo_path_is_keyed_on_the_package_id(tmp_path):
    store = make_tree(tmp_path)
    recipe = store.load("com.example.demo")

    path = store.undo_path(recipe)

    assert path.parent == tmp_path / "state" / "ui" / "com.example.demo"
    assert path.name.endswith("-pre-restore.json")


def test_undo_copy_is_new_and_owner_only(tmp_path):
    target = tmp_path / "undo.json"
    write_new_file(target, b"{}")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(StoreError, match="already exists"):
        write_new_file(target, b"{}")


def test_env_var_names_the_secrets_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_SECRETS_DIR", str(tmp_path / "from-env"))
    assert Store(apps=tmp_path / "apps").secrets == tmp_path / "from-env"


def test_empty_secrets_flag_falls_back_to_the_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_SECRETS_DIR", str(tmp_path / "from-env"))
    assert Store(apps=tmp_path / "apps", secrets="").secrets == tmp_path / "from-env"


def test_parse_sets_the_folder_name(tmp_path):
    apps = tmp_path / "apps"
    (apps / "demo").mkdir(parents=True)
    (apps / "demo" / "steps.py").write_text(steps_file())

    recipe = parse(apps / "demo" / "steps.py")

    assert recipe.name == "demo"
    assert recipe.dir == apps / "demo"


def test_state_digest_includes_the_private_half(tmp_path):
    recipe = recipe_from({**VALID, "private": ["settings.token"]})
    recipe.dir = tmp_path
    recipe.data_dir = tmp_path
    recipe.blob_dir = tmp_path
    (tmp_path / "demo.json").write_bytes(artifacts.dumps({"settings": {"token": "", "keep": 1}}))
    overlay = tmp_path / "demo.private.json"
    overlay.write_bytes(artifacts.dumps({"settings": {"token": "secret"}}))

    before = stores.state_digest(recipe)

    overlay.write_bytes(artifacts.dumps({"settings": {"token": "changed"}}))
    assert stores.state_digest(recipe) != before


def test_state_digest_refuses_a_missing_private_half(tmp_path):
    recipe = recipe_from({**VALID, "private": ["settings.token"]})
    recipe.dir = tmp_path
    recipe.data_dir = tmp_path
    recipe.blob_dir = tmp_path
    (tmp_path / "demo.json").write_bytes(artifacts.dumps({"settings": {"token": ""}}))

    with pytest.raises(StoreError, match="private half"):
        stores.state_digest(recipe)


def test_an_export_root_that_contains_the_recipe_tree_is_refused(tmp_path):
    apps = tmp_path / "repo" / "policy" / "data"
    apps.mkdir(parents=True)
    store = Store(apps=apps, secrets=tmp_path / "repo", state=tmp_path / "state")
    with pytest.raises(StoreError, match="contains the recipe tree"):
        store.data_root()


def test_a_state_root_inside_the_recipe_tree_is_refused(tmp_path):
    store = make_tree(tmp_path)
    recipe = store.load("com.example.demo")
    store.state = tmp_path / "apps" / "undo"
    with pytest.raises(StoreError, match="inside the recipe tree"):
        store.undo_path(recipe)


def test_two_undo_copies_in_one_second_do_not_collide(tmp_path, monkeypatch):
    store = make_tree(tmp_path)
    recipe = store.load("com.example.demo")

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(stores, "datetime", Frozen)
    first = store.undo_path(recipe)
    write_new_file(first, b"{}")
    second = store.undo_path(recipe)

    assert second != first
    assert second.name.endswith("-pre-restore-1.json")


def test_a_package_id_cannot_traverse_the_undo_tree(tmp_path):
    store = make_tree(tmp_path)
    with pytest.raises(StoreError, match="not a package name"):
        store.load("..")


def test_a_recipe_that_is_not_utf8_is_reported_beside_the_good_ones(tmp_path):
    store = make_tree(tmp_path)
    broken = tmp_path / "apps" / "broken"
    broken.mkdir()
    (broken / "steps.py").write_bytes(b'RECIPE = "\xff"')

    recipes, errors = store.all()

    assert [recipe.name for recipe in recipes] == ["demo"]
    assert len(errors) == 1
    assert "not UTF-8" in str(errors[0])
