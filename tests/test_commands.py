"""The commands: probe output, verify drift, and the restore guard."""

from __future__ import annotations

import io
import json
import re
import stat
from pathlib import Path

import pytest
from support import VALID, FakeDevice, steps_file, xml_node, xml_screen

from uiu import artifacts, commands
from uiu.store import Store
from uiu.tree import TreeError


def write_store(root, *, artifact: bytes | None):
    apps = root / "apps"
    secrets = root / "secrets"
    (apps / "demo").mkdir(parents=True)
    secrets.mkdir()
    (apps / "demo" / "steps.py").write_text(steps_file())
    if artifact is not None:
        (secrets / "demo").mkdir(parents=True, exist_ok=True)
        (secrets / "demo" / "demo.json").write_bytes(artifact)
    return Store(apps=apps, secrets=secrets, state=root / "state")


def test_temp_artifact_keeps_the_extension():
    name = commands.temp_artifact("yamlauncher.json")
    assert re.fullmatch(r"yamlauncher\.verify-\d{8}T\d{6}\.json", name)
    assert re.fullmatch(r"export\.verify-\d{8}T\d{6}", commands.temp_artifact("export"))


def test_probe_prints_the_screen_and_the_controls(tmp_path):
    fake = FakeDevice(
        xml_screen(
            xml_node(desc="Settings", clickable=True, bounds="[0,0,400,100]"),
            xml_node(text="Import / export", cls="android.widget.Button", bounds="[0,100,400,200]"),
        )
    )
    out = io.StringIO()

    assert commands.probe(None, out=out, device=fake) == 0

    text = out.getvalue()
    assert text.splitlines()[0] == "screen  com.example.demo/.MainActivity"
    assert '{"desc": "Settings"}' in text
    assert '{"text": "Import / export"}' in text


def test_probe_taps_the_chain_before_reading(tmp_path):
    fake = FakeDevice(xml_screen(xml_node(desc="Settings", clickable=True, bounds="[0,0,400,100]")))
    out = io.StringIO()

    commands.probe(None, chain=[("tap", '{"desc": "Settings"}')], out=out, device=fake)

    assert fake.taps == [(200, 50)]


def test_probe_miss_prints_the_screen_and_fails_cleanly():
    fake = FakeDevice(xml_screen(xml_node(text="Home", clickable=True, bounds="[0,0,100,50]")))
    out = io.StringIO()

    with pytest.raises(TreeError, match="no matching node"):
        commands.probe(
            None,
            chain=[("step", '{"tap": {"selector": {"desc": "Settings"}}}')],
            out=out,
            device=fake,
        )

    text = out.getvalue()
    assert text.startswith("screen when it failed")
    assert '{"text": "Home"}' in text


def test_probe_replays_a_recipe_step_including_a_long_press():
    fake = FakeDevice(
        xml_screen(xml_node(id="app:id/clock", clickable=True, bounds="[0,0,100,100]"))
    )
    out = io.StringIO()

    commands.probe(
        None,
        chain=[("step", '{"tap": {"selector": {"id": "app:id/clock"}, "duration": 800}}')],
        out=out,
        device=fake,
    )

    assert fake.long_presses == [(50, 50, 800)]


def test_probe_rejects_a_step_that_a_recipe_would_reject():
    fake = FakeDevice(xml_screen(xml_node(text="x")))

    with pytest.raises(commands.CommandError, match="unknown key"):
        commands.probe(None, chain=[("step", '{"press": {}}')], out=io.StringIO(), device=fake)


def test_verify_reports_the_same_digest(tmp_path):
    stored = artifacts.dumps({"apps": [], "exportedAt": 5})
    store = write_store(tmp_path, artifact=stored)
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = stored
    out = io.StringIO()

    assert commands.verify(store, "com.example.demo", None, out=out, device=fake) == 0
    assert out.getvalue().startswith("same     com.example.demo ")


def test_verify_reports_drift_with_a_diff(tmp_path):
    stored = artifacts.dumps({"apps": [], "keep": "old"})
    store = write_store(tmp_path, artifact=stored)
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = artifacts.dumps({"apps": [], "keep": "new"})
    out = io.StringIO()

    with pytest.raises(commands.CommandError, match="differs from the stored state"):
        commands.verify(store, "com.example.demo", None, out=out, device=fake)

    text = out.getvalue()
    assert "drift    com.example.demo" in text
    assert '- "keep": "old"' in text


def test_restore_refuses_drift_without_force_and_keeps_an_undo_copy(tmp_path):
    stored = artifacts.dumps({"apps": [], "keep": "old"})
    fresh = artifacts.dumps({"apps": [], "keep": "new"})
    store = write_store(tmp_path, artifact=stored)
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = fresh
    out = io.StringIO()

    with pytest.raises(commands.CommandError, match="re-run with --force"):
        commands.restore(store, "com.example.demo", None, out=out, device=fake)

    undo = next(iter((tmp_path / "state" / "ui" / "com.example.demo").iterdir()))
    assert undo.read_bytes() == fresh
    assert stat.S_IMODE(undo.stat().st_mode) == 0o600
    assert fake.pushes == []


def test_restore_force_pushes_the_stored_artifact(tmp_path):
    stored = artifacts.dumps({"apps": [], "keep": "old"})
    fresh = artifacts.dumps({"apps": [], "keep": "new"})
    store = write_store(tmp_path, artifact=stored)

    class RestoringFake(FakeDevice):
        """An app whose import takes effect: the next export reads what was pushed."""

        def push(self, local, remote):
            super().push(local, remote)
            self.pull_body = Path(local).read_bytes()

    fake = RestoringFake(xml_screen(xml_node(text="x")))
    fake.pull_body = fresh
    out = io.StringIO()

    assert commands.restore(store, "com.example.demo", None, force=True, out=out, device=fake) == 0

    assert fake.pushed[0] == ("/sdcard/Download/demo.json", stored)
    assert "restored com.example.demo" in out.getvalue()


def test_restore_says_nothing_to_do_when_the_device_matches(tmp_path):
    stored = artifacts.dumps({"apps": [], "keep": "old"})
    store = write_store(tmp_path, artifact=stored)
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = stored
    out = io.StringIO()

    assert commands.restore(store, "com.example.demo", None, out=out, device=fake) == 0

    assert out.getvalue().startswith("already matches")
    assert fake.pushes == []


def test_verify_refuses_a_capture_only_recipe(tmp_path):
    apps = tmp_path / "apps"
    (apps / "demo").mkdir(parents=True)
    (tmp_path / "secrets").mkdir()
    capture = {"package": "com.example.demo", "backup": [{"capture": {"name": "main"}}]}
    (apps / "demo" / "steps.py").write_text(steps_file(capture))
    store = Store(apps=apps, secrets=tmp_path / "secrets", state=tmp_path / "state")

    with pytest.raises(commands.CommandError, match="capture-only"):
        commands.verify(
            store, "com.example.demo", None, device=FakeDevice(xml_screen(xml_node(text="x")))
        )


def test_list_prints_each_recipe_and_the_secrets_root(tmp_path):
    store = write_store(tmp_path, artifact=b"{}")
    out = io.StringIO()

    assert commands.list_recipes(store, out=out) == 0

    text = out.getvalue()
    assert "demo" in text
    assert "com.example.demo" in text
    assert f"secrets  {tmp_path / 'secrets'}" in text


def test_list_returns_one_when_a_recipe_does_not_parse(tmp_path):
    store = write_store(tmp_path, artifact=None)
    (tmp_path / "apps" / "broken").mkdir()
    (tmp_path / "apps" / "broken" / "steps.py").write_text("RECIPE = 1")
    out = io.StringIO()

    assert commands.list_recipes(store, out=out) == 1
    assert "error    " in out.getvalue()


def write_private_store(root, *, public: bytes, overlay: bytes):
    apps = root / "apps"
    secrets = root / "secrets"
    (apps / "demo").mkdir(parents=True)
    (secrets / "demo").mkdir(parents=True)
    (apps / "demo" / "steps.py").write_text(steps_file({**VALID, "private": ["settings.token"]}))
    (apps / "demo" / "demo.json").write_bytes(public)
    (secrets / "demo" / "demo.private.json").write_bytes(overlay)
    return Store(apps=apps, secrets=secrets, state=root / "state")


def test_verify_sees_a_change_confined_to_the_private_half(tmp_path):
    public = artifacts.dumps({"settings": {"token": "", "keep": 1}})
    overlay = artifacts.dumps({"settings": {"token": "secret"}})
    store = write_private_store(tmp_path, public=public, overlay=overlay)
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = artifacts.dumps({"settings": {"token": "live", "keep": 1}})
    out = io.StringIO()

    with pytest.raises(commands.CommandError, match="differs from the stored state"):
        commands.verify(store, "com.example.demo", None, out=out, device=fake)

    text = out.getvalue()
    assert "private values differ" in text
    assert "secret" not in text and "live" not in text


def test_restore_force_puts_a_private_value_back(tmp_path):
    public = artifacts.dumps({"settings": {"token": "", "keep": 1}})
    overlay = artifacts.dumps({"settings": {"token": "secret"}})
    store = write_private_store(tmp_path, public=public, overlay=overlay)

    class RestoringFake(FakeDevice):
        def push(self, local, remote):
            super().push(local, remote)
            self.pull_body = Path(local).read_bytes()

    fake = RestoringFake(xml_screen(xml_node(text="x")))
    fake.pull_body = artifacts.dumps({"settings": {"token": "live", "keep": 1}})
    out = io.StringIO()

    assert commands.restore(store, "com.example.demo", None, force=True, out=out, device=fake) == 0

    assert fake.pushed[0] == (
        "/sdcard/Download/demo.json",
        artifacts.dumps({"settings": {"token": "secret", "keep": 1}}),
    )
    assert "restored com.example.demo" in out.getvalue()


def test_restore_without_force_refuses_a_private_only_drift(tmp_path):
    public = artifacts.dumps({"settings": {"token": "", "keep": 1}})
    overlay = artifacts.dumps({"settings": {"token": "secret"}})
    store = write_private_store(tmp_path, public=public, overlay=overlay)
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = artifacts.dumps({"settings": {"token": "live", "keep": 1}})
    out = io.StringIO()

    with pytest.raises(commands.CommandError, match="re-run with --force"):
        commands.restore(store, "com.example.demo", None, out=out, device=fake)

    assert fake.pushes == []
    undo = next(iter((tmp_path / "state" / "ui" / "com.example.demo").iterdir()))
    assert json.loads(undo.read_bytes())["settings"]["token"] == "live"


def test_probe_step_is_validated_and_cannot_write_files():
    fake = FakeDevice(xml_screen(xml_node(text="x")))

    with pytest.raises(commands.CommandError, match="tap needs selector"):
        commands.probe(None, chain=[("step", '{"tap": {}}')], out=io.StringIO(), device=fake)

    with pytest.raises(commands.CommandError, match="cannot run capture"):
        commands.probe(
            None,
            chain=[("step", '{"capture": {"name": "main"}}')],
            out=io.StringIO(),
            device=fake,
        )


def test_probe_clear_needs_a_launch():
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    with pytest.raises(commands.CommandError, match="needs --launch"):
        commands.probe(None, clear=True, out=io.StringIO(), device=fake)
