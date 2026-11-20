"""Discovery: which app is which app, and which tree holds its bytes."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest
from support import ENVELOPE, VALID, app_file, recipe_from

from uiu import artifacts, spec, split
from uiu import store as stores
from uiu.store import Store, StoreError, write_new_file
from uiu.vault import Vault, VaultError


def make_tree(root, *, private: bool = False) -> Store:
    apps = root / "apps"
    secrets = root / "secrets"
    (apps / "demo").mkdir(parents=True)
    (apps / "demo" / "app.py").write_text(app_file())
    if private:
        (secrets / "demo").mkdir(parents=True)
        (secrets / "demo" / "private.json").write_text(json.dumps(["token"]))
        (apps / "demo" / "app.py").write_text(app_file({**VALID, "private": "private.json"}))
    else:
        secrets.mkdir()
    return Store(apps=apps, secrets=secrets, state=root / "state")


def test_a_public_recipe_keeps_its_artifact_in_the_private_tree(tmp_path):
    store = make_tree(tmp_path)
    apps, errors = store.all()

    assert errors == []
    assert [app.name for app in apps] == ["demo"]
    recipe = apps[0].recipe
    assert recipe is not None
    assert recipe.blob_dir == tmp_path / "secrets" / "demo"
    assert recipe.data_dir == tmp_path / "secrets" / "demo"


def test_a_private_recipe_commits_its_artifact_beside_the_app_file(tmp_path):
    store = make_tree(tmp_path, private=True)
    apps, errors = store.all()

    assert errors == []
    recipe = apps[0].recipe
    assert recipe is not None
    assert recipe.blob_dir == tmp_path / "apps" / "demo"
    assert recipe.private_path() == tmp_path / "secrets" / "demo" / "demo.private.json"


def test_an_app_file_in_the_private_tree_is_discovered(tmp_path):
    store = make_tree(tmp_path)
    (tmp_path / "secrets" / "hidden").mkdir()
    (tmp_path / "secrets" / "hidden" / "app.py").write_text(
        app_file({**VALID, "package": "com.example.hidden"})
    )

    apps, errors = store.all()

    assert errors == []
    assert [app.name for app in apps] == ["demo", "hidden"]
    hidden = store.load("com.example.hidden")
    assert hidden.recipe is not None
    assert hidden.recipe.dir == tmp_path / "secrets" / "hidden"
    assert hidden.recipe.data_dir == tmp_path / "secrets" / "hidden"


def test_the_same_app_folder_in_both_trees_is_refused(tmp_path):
    store = make_tree(tmp_path)
    (tmp_path / "secrets" / "demo").mkdir(parents=True)
    (tmp_path / "secrets" / "demo" / "app.py").write_text(app_file())

    with pytest.raises(StoreError, match="exists in both trees"):
        store.all()


def test_the_store_attaches_a_vault_covering_the_export_root(tmp_path):
    store = make_tree(tmp_path, private=True)
    recipe = store.load("com.example.demo").recipe

    assert recipe is not None
    assert recipe.vault is not None
    assert recipe.vault.covers(recipe.private_path())
    assert not recipe.vault.covers(tmp_path / "apps" / "demo" / "app.py")


def test_a_temporary_data_dir_stays_outside_the_vault(tmp_path):
    store = make_tree(tmp_path)
    recipe = store.load("com.example.demo").recipe
    assert recipe is not None

    temp = recipe.with_data_dir(tmp_path / "temp")

    assert temp.vault is recipe.vault
    assert temp.vault is not None
    assert not temp.vault.covers(temp.artifact_path())


def test_load_finds_a_recipe_by_package(tmp_path):
    store = make_tree(tmp_path)
    found = store.load("com.example.demo")
    assert found.name == "demo"
    assert found.recipe is not None
    with pytest.raises(StoreError, match="no app file declares package"):
        store.load("com.example.other")
    with pytest.raises(StoreError, match="not a package name"):
        store.load("../demo")


def test_listing_does_not_read_the_private_selectors(tmp_path, monkeypatch):
    store = make_tree(tmp_path, private=True)
    (tmp_path / "secrets" / "demo" / "private.json").write_bytes(ENVELOPE)

    def refuse() -> str:
        raise AssertionError("the age key was read before a covered file needed it")

    def refuse_run(argv: Sequence[str], body: bytes) -> bytes:
        raise AssertionError("sops ran before a covered file needed it")

    def no_key(root: Path, **_options: object) -> Vault:
        return Vault(root, identity=refuse, run=refuse_run)

    monkeypatch.setattr(stores.vault, "Vault", no_key)

    apps, errors = store.all()

    assert errors == []
    assert [app.name for app in apps] == ["demo"]
    recipe = apps[0].recipe
    assert recipe is not None
    assert recipe.private_file == "private.json"
    assert recipe.private == []
    with pytest.raises(VaultError, match="secretspec set SOPS_AGE_KEY"):
        store.load("com.example.demo")


def test_two_app_files_for_one_package_are_refused(tmp_path):
    store = make_tree(tmp_path)
    other = tmp_path / "apps" / "copy"
    other.mkdir()
    (other / "app.py").write_text(app_file())

    with pytest.raises(StoreError, match="more than one app file"):
        store.load("com.example.demo")


def test_an_app_with_no_walk_lists_as_policy_only(tmp_path):
    store = make_tree(tmp_path)
    (tmp_path / "apps" / "demo" / "app.py").write_text(app_file({"package": "com.example.demo"}))
    (tmp_path / "secrets" / "demo").mkdir(parents=True)

    apps, errors = store.all()

    assert errors == []
    assert [app.name for app in apps] == ["demo"]
    assert apps[0].recipe is None
    assert store.state_of(apps[0]) == "policy only"
    with pytest.raises(StoreError, match="has no screen walk"):
        store.undo_path(apps[0])


def test_export_root_inside_the_app_tree_is_refused(tmp_path):
    store = Store(
        apps=tmp_path / "apps", secrets=tmp_path / "apps" / "secrets", state=tmp_path / "state"
    )
    with pytest.raises(StoreError, match="inside the policy tree"):
        store.data_root()


def test_missing_secrets_tree_says_how_to_fix_it(tmp_path):
    store = Store(apps=tmp_path / "apps", secrets=tmp_path / "secrets", state=tmp_path / "state")
    (tmp_path / "apps" / "demo").mkdir(parents=True)
    (tmp_path / "apps" / "demo" / "app.py").write_text(app_file())

    with pytest.raises(StoreError, match="clone the secrets repository"):
        store.load("com.example.demo")


def test_state_of_reports_the_private_half(tmp_path):
    store = make_tree(tmp_path, private=True)
    app = store.load("com.example.demo")
    recipe = app.recipe
    assert recipe is not None

    assert store.state_of(app) == "missing no private half"

    recipe.artifact_path().write_bytes(b"{}")
    assert store.state_of(app) == "stored no private half"

    recipe.private_path().parent.mkdir(parents=True, exist_ok=True)
    recipe.private_path().write_bytes(ENVELOPE)
    assert store.state_of(app) == "stored +private"


def test_state_of_marks_a_plaintext_artifact(tmp_path):
    store = make_tree(tmp_path)
    app = store.load("com.example.demo")
    recipe = app.recipe
    assert recipe is not None

    recipe.artifact_path().parent.mkdir(parents=True, exist_ok=True)
    recipe.artifact_path().write_bytes(b"{}")
    assert store.state_of(app) == "stored plaintext"


def test_state_of_does_not_mark_an_envelope(tmp_path):
    store = make_tree(tmp_path)
    app = store.load("com.example.demo")
    recipe = app.recipe
    assert recipe is not None

    recipe.artifact_path().parent.mkdir(parents=True, exist_ok=True)
    recipe.artifact_path().write_bytes(ENVELOPE)
    assert store.state_of(app) == "stored"


def test_state_of_marks_a_plaintext_private_half(tmp_path):
    store = make_tree(tmp_path, private=True)
    app = store.load("com.example.demo")
    recipe = app.recipe
    assert recipe is not None

    # The public half beside app.py is committed plaintext by design; only the
    # overlay sits under the vault, so only the overlay can be unsealed.
    recipe.artifact_path().write_bytes(b"{}")
    recipe.private_path().write_bytes(b"{}\n")
    assert store.state_of(app) == "stored plaintext +private"


def test_state_of_marks_a_plaintext_private_half_without_its_artifact(tmp_path):
    store = make_tree(tmp_path, private=True)
    app = store.load("com.example.demo")
    recipe = app.recipe
    assert recipe is not None

    recipe.private_path().parent.mkdir(parents=True, exist_ok=True)
    recipe.private_path().write_bytes(b"{}\n")
    assert store.state_of(app) == "missing plaintext +private"


def test_undo_path_is_keyed_on_the_package_id(tmp_path):
    store = make_tree(tmp_path)
    app = store.load("com.example.demo")

    path = store.undo_path(app)

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
    (apps / "demo" / "app.py").write_text(app_file())

    app = spec.parse(apps / "demo" / "app.py")

    assert app.name == "demo"
    assert app.dir == apps / "demo"


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


def test_digests_read_the_stored_halves_through_the_vault(tmp_path):
    recipe = recipe_from({**VALID, "private": ["settings.token"]})
    recipe.dir = tmp_path
    recipe.data_dir = tmp_path
    recipe.blob_dir = tmp_path
    public = artifacts.dumps({"settings": {"token": "", "keep": 1}})
    private = artifacts.dumps({"settings": {"token": "secret"}})
    (tmp_path / "demo.json").write_bytes(ENVELOPE)
    (tmp_path / "demo.private.json").write_bytes(ENVELOPE)

    def run(argv, body):
        return private if str(argv[-1]).endswith(".private.json") else public

    recipe.vault = Vault(tmp_path, identity=lambda: "AGE-SECRET-KEY-1", run=run)
    merged = split.merge_export(public, private, recipe.private)

    assert (
        stores.artifact_digest(recipe)
        == hashlib.sha256(artifacts.canonical(public, [], [])).hexdigest()
    )
    assert (
        stores.state_digest(recipe)
        == hashlib.sha256(artifacts.canonical(merged, [], [])).hexdigest()
    )


def test_state_digest_refuses_a_missing_private_half(tmp_path):
    recipe = recipe_from({**VALID, "private": ["settings.token"]})
    recipe.dir = tmp_path
    recipe.data_dir = tmp_path
    recipe.blob_dir = tmp_path
    (tmp_path / "demo.json").write_bytes(artifacts.dumps({"settings": {"token": ""}}))

    with pytest.raises(StoreError, match="private half"):
        stores.state_digest(recipe)


def test_an_export_root_that_contains_the_app_tree_is_refused(tmp_path):
    apps = tmp_path / "repo" / "policy" / "data"
    apps.mkdir(parents=True)
    store = Store(apps=apps, secrets=tmp_path / "repo", state=tmp_path / "state")
    with pytest.raises(StoreError, match="contains the policy tree"):
        store.data_root()


def test_a_state_root_inside_the_app_tree_is_refused(tmp_path):
    store = make_tree(tmp_path)
    app = store.load("com.example.demo")
    store.state = tmp_path / "apps" / "undo"
    with pytest.raises(StoreError, match="inside the policy tree"):
        store.undo_path(app)


def test_a_state_root_inside_the_private_tree_is_refused(tmp_path):
    store = make_tree(tmp_path)
    store.state = tmp_path / "secrets" / ".state"

    with pytest.raises(StoreError, match="inside the private tree.*plaintext"):
        store.state_root()


def test_a_state_root_containing_the_private_tree_is_refused(tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    store = Store(apps=apps, secrets=tmp_path / "work" / "secrets", state=tmp_path / "work")

    with pytest.raises(StoreError, match="contains the private tree.*plaintext"):
        store.state_root()


def test_a_state_root_beside_the_private_tree_is_fine(tmp_path):
    store = Store(apps=tmp_path / "apps", secrets=tmp_path / "secrets")
    assert store.state_root() == Path(".state").resolve()

    store.state = tmp_path / ".state"
    assert store.state_root() == tmp_path / ".state"


def test_two_undo_copies_in_one_second_do_not_collide(tmp_path, monkeypatch):
    store = make_tree(tmp_path)
    app = store.load("com.example.demo")

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(stores, "datetime", Frozen)
    first = store.undo_path(app)
    write_new_file(first, b"{}")
    second = store.undo_path(app)

    assert second != first
    assert second.name.endswith("-pre-restore-1.json")


def test_a_package_id_cannot_traverse_the_undo_tree(tmp_path):
    store = make_tree(tmp_path)
    with pytest.raises(StoreError, match="not a package name"):
        store.load("..")


def test_an_app_file_that_is_not_utf8_is_reported_beside_the_good_ones(tmp_path):
    store = make_tree(tmp_path)
    broken = tmp_path / "apps" / "broken"
    broken.mkdir()
    (broken / "app.py").write_bytes(b'APP = "\xff"')

    apps, errors = store.all()

    assert [app.name for app in apps] == ["demo"]
    assert len(errors) == 1
    assert "not UTF-8" in str(errors[0])
