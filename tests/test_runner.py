"""Step execution against a fake device: caching, targeting, transfer, waiting."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from support import ENVELOPE, VALID, FakeDevice, recipe_from, recipe_in, xml_node, xml_screen

from uiu import artifacts, runner, vault
from uiu.device import ShellResult
from uiu.recipe import Selector, Step
from uiu.runner import Runner, StepError, export_paths, merge_file, run
from uiu.vault import Vault, VaultError


def test_a_wait_that_matched_pays_for_one_dump():
    screen = xml_screen(xml_node(desc="Settings", clickable=True, bounds="[0,0,200,100]"))
    fake = FakeDevice(screen)
    recipe = recipe_in(Path("."))
    worker = Runner(fake, recipe)

    worker._wait(Step(kind="wait", selector=Selector(desc="Settings"), timeout=1))
    worker._tap(Step(kind="tap", selector=Selector(desc="Settings")))

    assert fake.dumps == 1
    assert fake.taps == [(100, 50)]


def test_optional_tap_that_misses_is_not_an_error():
    fake = FakeDevice(xml_screen(xml_node(text="Home")))
    worker = Runner(fake, recipe_in(Path(".")))

    worker._tap(Step(kind="tap", selector=Selector(desc="Dialog"), optional=True))

    assert fake.taps == []


def test_required_tap_that_misses_says_so():
    fake = FakeDevice(xml_screen(xml_node(text="Home")))
    worker = Runner(fake, recipe_in(Path(".")))

    with pytest.raises(Exception, match="no matching node"):
        worker._tap(Step(kind="tap", selector=Selector(desc="Dialog")))


def test_long_press_uses_the_duration():
    screen = xml_screen(xml_node(id="app:id/clock", clickable=True, bounds="[0,0,100,100]"))
    fake = FakeDevice(screen)
    worker = Runner(fake, recipe_in(Path(".")))

    worker._tap(Step(kind="tap", selector=Selector(resource_id="app:id/clock"), duration=800))

    assert fake.long_presses == [(50, 50, 800)]
    assert fake.taps == []


def test_typing_taps_the_field_then_sets_its_text():
    screen = xml_screen(
        xml_node(
            id="android:id/title",
            cls="android.widget.EditText",
            text="old.txt",
            bounds="[100,100,900,200]",
        )
    )
    fake = FakeDevice(screen)
    worker = Runner(fake, recipe_in(Path(".")))

    worker._type(
        Step(
            kind="type",
            selector=Selector(resource_id="android:id/title", class_name="android.widget.EditText"),
            text="demo.json",
        )
    )

    assert fake.taps == [(500, 150)]
    assert fake.texts == ["demo.json"]


def test_clearing_verifies_the_field_is_empty():
    dirty = xml_screen(
        xml_node(id="android:id/title", cls="android.widget.EditText", text="old.txt")
    )
    empty = xml_screen(xml_node(id="android:id/title", cls="android.widget.EditText", text=""))
    fake = FakeDevice([dirty, empty])
    worker = Runner(fake, recipe_in(Path(".")))

    worker._clear(
        Step(
            kind="clear",
            selector=Selector(resource_id="android:id/title", class_name="android.widget.EditText"),
        )
    )

    assert fake.cleared == 1
    assert fake.dumps == 2


def test_clearing_falls_back_to_deletes_and_reports_a_stuck_field():
    dirty = xml_screen(
        xml_node(id="android:id/title", cls="android.widget.EditText", text="old.txt")
    )
    fake = FakeDevice(dirty)
    worker = Runner(fake, recipe_in(Path(".")))

    with pytest.raises(Exception, match="still holds"):
        worker._clear(
            Step(
                kind="clear",
                selector=Selector(
                    resource_id="android:id/title", class_name="android.widget.EditText"
                ),
            )
        )

    assert ["input", "keyevent"] + ["KEYCODE_DEL"] * len("old.txt") in fake.shells


def test_newest_takes_the_match_this_run_produced(tmp_path):
    fake = FakeDevice(
        xml_screen(xml_node(text="x")),
        reply=lambda command: (
            ShellResult(0, "1500 /sdcard/Download/demo-7.json\n")
            if isinstance(command, str)
            else ShellResult(0, "10\n")
        ),
    )
    worker = Runner(fake, recipe_in(tmp_path))
    worker.started = 1000

    assert worker.newest("/sdcard/Download/demo-*.json") == "/sdcard/Download/demo-7.json"


def test_newest_refuses_a_path_outside_sdcard(tmp_path):
    fake = FakeDevice(
        xml_screen(xml_node(text="x")),
        reply=lambda command: (
            ShellResult(0, "1500 /data/local/tmp/demo.json\n")
            if isinstance(command, str)
            else ShellResult(0, "10\n")
        ),
    )
    worker = Runner(fake, recipe_in(tmp_path))
    worker.started = 1000

    with pytest.raises(Exception, match="not under /sdcard"):
        worker.newest("/sdcard/Download/demo-*.json")


def test_pull_splits_the_private_half_into_a_private_file(tmp_path):
    body = artifacts.dumps({"apps": [{"id": "a"}, {"id": "bank", "name": "B"}]})
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = body
    recipe = recipe_in(tmp_path, {**VALID, "private": ["apps[id=bank]"]})
    worker = Runner(fake, recipe)
    run_dir = tmp_path / "run"
    worker.tmp = run_dir
    run_dir.mkdir()
    worker.await_file = lambda path: None

    worker._pull(Step(kind="pull", source="/sdcard/Download/demo.json", target="demo.json"))

    assert json.loads((tmp_path / "demo.json").read_bytes()) == {"apps": [{"id": "a"}]}
    overlay = tmp_path / "demo.private.json"
    assert json.loads(overlay.read_bytes()) == {"apps": [{"id": "bank", "name": "B"}]}
    assert stat.S_IMODE(overlay.stat().st_mode) == 0o600


def test_push_sends_the_merged_export_from_a_private_temp_file(tmp_path):
    (tmp_path / "demo.json").write_bytes(artifacts.dumps({"apps": [{"id": "a"}]}))
    (tmp_path / "demo.private.json").write_bytes(
        artifacts.dumps({"apps": [{"id": "bank", "name": "B"}]})
    )
    recipe = recipe_in(tmp_path, {**VALID, "private": ["apps[id=bank]"]})
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    worker = Runner(fake, recipe)
    run_dir = tmp_path / "run"
    worker.tmp = run_dir
    run_dir.mkdir()

    worker._push(Step(kind="push", source="demo.json", target="/sdcard/Download/demo.json"))

    (sent, remote) = fake.pushes[0]
    assert remote == "/sdcard/Download/demo.json"
    assert json.loads(sent.read_bytes()) == {"apps": [{"id": "a"}, {"id": "bank", "name": "B"}]}
    assert stat.S_IMODE(sent.stat().st_mode) == 0o600


def test_push_refuses_when_the_private_half_is_missing(tmp_path):
    (tmp_path / "demo.json").write_bytes(artifacts.dumps({"apps": []}))
    recipe = recipe_in(tmp_path, {**VALID, "private": ["apps[id=bank]"]})
    worker = Runner(FakeDevice(xml_screen(xml_node(text="x"))), recipe)

    with pytest.raises(Exception, match="needs it beside the artifact"):
        worker._push(Step(kind="push", source="demo.json", target="/sdcard/Download/demo.json"))


def test_push_sends_the_artifact_from_the_export_root(tmp_path):
    app_dir = tmp_path / "apps" / "demo"
    app_dir.mkdir(parents=True)
    export = tmp_path / "secrets" / "demo"
    export.mkdir(parents=True)
    (export / "demo.json").write_bytes(b'{"keep": 1}')
    recipe = recipe_in(app_dir)
    recipe.blob_dir = export
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    worker = Runner(fake, recipe)

    worker._push(Step(kind="push", source="demo.json", target="/sdcard/Download/demo.json"))

    assert fake.pushes == [(export / "demo.json", "/sdcard/Download/demo.json")]


def test_push_stages_a_covered_artifact_as_plaintext(tmp_path):
    export = tmp_path / "secrets" / "demo"
    export.mkdir(parents=True)
    (export / "demo.json").write_bytes(ENVELOPE)
    recipe = recipe_from(VALID)
    recipe.dir = tmp_path / "apps" / "demo"
    recipe.blob_dir = export
    recipe.vault = Vault(
        export.parent,
        identity=lambda: "AGE-SECRET-KEY-1",
        run=lambda argv, body: b'{"keep": 1}',
    )
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    worker = Runner(fake, recipe)
    run_dir = tmp_path / "run"
    worker.tmp = run_dir
    run_dir.mkdir()

    worker._push(Step(kind="push", source="demo.json", target="/sdcard/Download/demo.json"))

    assert fake.pushed == [("/sdcard/Download/demo.json", b'{"keep": 1}')]
    staged = fake.pushes[0][0]
    assert staged.parent == run_dir
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600


def test_publish_writes_envelopes_into_the_private_tree(tmp_path):
    recipe = recipe_in(tmp_path, {**VALID, "private": ["settings.token"]})
    bodies: list[bytes] = []
    destinations: list[str] = []
    pending: list[bytes] = []

    def run(argv, body):
        # `vault.write` opens every envelope it makes, so the seam answers the
        # decrypt call with the body it just encrypted.
        if argv[0] == "decrypt":
            return pending[-1]
        pending.append(body)
        bodies.append(body)
        destinations.append(argv[-2])
        return ENVELOPE

    recipe.vault = Vault(tmp_path, identity=lambda: "AGE-SECRET-KEY-1", run=run)
    worker = Runner(FakeDevice(xml_screen(xml_node(text="x"))), recipe)
    public = artifacts.dumps({"settings": {"token": ""}})
    private = artifacts.dumps({"settings": {"token": "secret"}})

    worker.publish(recipe.artifact_path(), public, private)

    assert vault.encrypted(recipe.artifact_path().read_bytes())
    assert vault.encrypted(recipe.private_path().read_bytes())
    assert bodies == [public, private]
    assert destinations == [str(recipe.artifact_path()), str(recipe.private_path())]


def test_publish_renames_the_overlay_before_the_artifact(tmp_path, monkeypatch):
    recipe = recipe_in(tmp_path, {**VALID, "private": ["settings.token"]})
    pending: list[bytes] = []

    def run(argv, body):
        if argv[0] == "decrypt":
            return pending[-1]
        pending.append(body)
        return ENVELOPE

    recipe.vault = Vault(tmp_path, identity=lambda: "AGE-SECRET-KEY-1", run=run)
    worker = Runner(FakeDevice(xml_screen(xml_node(text="x"))), recipe)
    replaced: list[Path] = []
    overlay_seen: list[bytes] = []
    real = os.replace

    def replace(source, target):
        replaced.append(Path(target))
        if Path(target) == recipe.artifact_path():
            # The artifact rename must not be the first time a reader can see
            # the pair: the overlay is already in place when it happens.
            overlay_seen.append(recipe.private_path().read_bytes())
        real(source, target)

    monkeypatch.setattr(runner.os, "replace", replace)

    worker.publish(
        recipe.artifact_path(),
        artifacts.dumps({"settings": {"token": ""}}),
        artifacts.dumps({"settings": {"token": "secret"}}),
    )

    assert replaced == [recipe.private_path(), recipe.artifact_path()]
    assert overlay_seen == [ENVELOPE]
    assert vault.encrypted(recipe.artifact_path().read_bytes())


def test_a_failed_overlay_encryption_leaves_the_pair_untouched(tmp_path):
    app_dir = tmp_path / "apps" / "demo"
    app_dir.mkdir(parents=True)
    export = tmp_path / "secrets" / "demo"
    export.mkdir(parents=True)
    recipe = recipe_from({**VALID, "private": ["settings.token"]})
    recipe.dir = app_dir
    recipe.data_dir = export
    recipe.blob_dir = app_dir
    final = recipe.artifact_path()
    overlay = recipe.private_path()
    final.write_bytes(b"old artifact")
    overlay.write_bytes(b"old overlay")

    def run(argv, body):
        if str(argv[-2]) == str(overlay):
            raise VaultError("no identity matched any of the recipients")
        return ENVELOPE

    recipe.vault = Vault(export.parent, identity=lambda: "AGE-SECRET-KEY-1", run=run)
    worker = Runner(FakeDevice(xml_screen(xml_node(text="x"))), recipe)

    with pytest.raises(VaultError, match="no identity matched"):
        worker.publish(final, b"new artifact", b"new overlay")

    assert final.read_bytes() == b"old artifact"
    assert overlay.read_bytes() == b"old overlay"
    assert sorted(entry.name for entry in tmp_path.rglob("*.tmp")) == []


def test_merge_file_decrypts_both_halves(tmp_path):
    recipe = recipe_in(tmp_path, {**VALID, "private": ["apps[id=bank]"]})
    public = artifacts.dumps({"apps": [{"id": "a"}, {"id": "bank", "name": "B"}]})
    private = artifacts.dumps({"apps": [{"id": "bank", "name": "B"}]})
    (tmp_path / "demo.json").write_bytes(ENVELOPE)
    (tmp_path / "demo.private.json").write_bytes(ENVELOPE)

    def run(argv, body):
        return private if str(argv[-1]).endswith(".private.json") else public

    recipe.vault = Vault(tmp_path, identity=lambda: "AGE-SECRET-KEY-1", run=run)

    assert json.loads(merge_file(recipe)) == {"apps": [{"id": "a"}, {"id": "bank", "name": "B"}]}


def test_capture_writes_an_envelope_into_the_private_tree(tmp_path):
    export = tmp_path / "secrets" / "demo"
    recipe = recipe_from(VALID)
    recipe.dir = tmp_path / "apps" / "demo"
    recipe.data_dir = export
    recipe.blob_dir = export
    bodies: list[bytes] = []
    pending: list[bytes] = []

    def run(argv, body):
        # The envelope is opened again by `vault.write`, so a decrypt answers
        # with the plaintext that went in.
        if argv[0] == "decrypt":
            return pending[-1]
        pending.append(body)
        bodies.append(body)
        return ENVELOPE

    recipe.vault = Vault(export.parent, identity=lambda: "AGE-SECRET-KEY-1", run=run)
    worker = Runner(FakeDevice(xml_screen(xml_node(text="Home"))), recipe)

    worker._capture(Step(kind="capture", name="home"))

    path = export / "demo.d" / "home.xml"
    assert vault.encrypted(path.read_bytes())
    assert bodies and bodies[0].startswith(b"<hierarchy")
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["home.xml"]


def test_push_sends_a_payload_from_beside_the_app_file(tmp_path):
    app_dir = tmp_path / "apps" / "demo"
    app_dir.mkdir(parents=True)
    (app_dir / "payload.json").write_bytes(b'{"keep": 1}')
    export = tmp_path / "secrets" / "demo"
    export.mkdir(parents=True)
    recipe = recipe_from(
        {
            "package": "com.example.demo",
            "setup": [{"push": {"from": "payload.json", "to": "/sdcard/Download/payload.json"}}],
        }
    )
    recipe.dir = app_dir
    recipe.blob_dir = export
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    worker = Runner(fake, recipe)

    worker._push(Step(kind="push", source="payload.json", target="/sdcard/Download/payload.json"))

    assert fake.pushes == [(app_dir / "payload.json", "/sdcard/Download/payload.json")]


def test_push_of_a_payload_ignores_a_private_recipe_overlay(tmp_path):
    app_dir = tmp_path / "apps" / "demo"
    app_dir.mkdir(parents=True)
    (app_dir / "payload.json").write_bytes(b'{"keep": 1}')
    recipe = recipe_from({**VALID, "private": ["settings.token"]})
    recipe.dir = app_dir
    recipe.blob_dir = app_dir
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    worker = Runner(fake, recipe)

    # No overlay exists here, so a merge attempt would fail instead of pushing.
    worker._push(Step(kind="push", source="payload.json", target="/sdcard/Download/payload.json"))

    assert fake.pushes == [(app_dir / "payload.json", "/sdcard/Download/payload.json")]


def test_scroll_is_a_swipe_of_the_screen_height():
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    worker = Runner(fake, recipe_in(Path(".")))

    worker._scroll(Step(kind="scroll", direction="down", times=2))
    worker._scroll(Step(kind="scroll", direction="up", times=1))

    assert fake.swipes == [
        (540, 1755, 540, 585, 300),
        (540, 1755, 540, 585, 300),
        (540, 585, 540, 1755, 300),
    ]


def test_run_stops_the_app_and_cleans_the_device_paths(tmp_path):
    fake = FakeDevice(xml_screen(xml_node(text="Home", bounds="[0,0,10,10]")))
    recipe = recipe_in(tmp_path)
    (tmp_path / "demo.json").write_bytes(b"{}")
    steps = [
        Step(kind="key", code="KEYCODE_HOME"),
        Step(kind="push", source="demo.json", target="/sdcard/Download/demo.json"),
    ]

    run(fake, recipe, steps)

    assert fake.stopped == ["com.example.demo"]
    assert ["rm", "-f", "/sdcard/Download/demo.json"] in fake.shells
    assert fake.keys == ["KEYCODE_HOME"]
    assert fake.pushes == [(tmp_path / "demo.json", "/sdcard/Download/demo.json")]


def test_run_reports_the_step_that_failed(tmp_path):
    fake = FakeDevice(xml_screen(xml_node(text="Home")))
    recipe = recipe_in(tmp_path)

    with pytest.raises(StepError, match=r"step 1 \(tap\): no matching node"):
        run(fake, recipe, [Step(kind="tap", selector=Selector(desc="Missing"))])


def test_run_reads_the_device_clock_only_for_a_newest_pull(tmp_path):
    fake = FakeDevice(xml_screen(xml_node(text="Home")))
    recipe = recipe_in(tmp_path)
    run(fake, recipe, [Step(kind="key", code="KEYCODE_HOME")])
    assert fake.clock_reads == 0

    def reply(command):
        if isinstance(command, str):
            return ShellResult(0, "1500 /sdcard/Download/demo-7.json\n")
        return None

    fake = FakeDevice(xml_screen(xml_node(text="Home")), reply=reply)
    fake.pull_body = b"0123456789"
    run(
        fake,
        recipe,
        [Step(kind="pull", source="/sdcard/Download/demo-*.json", target="demo.json", newest=True)],
    )
    assert fake.clock_reads == 1


def test_export_paths_skips_a_newest_source(tmp_path):
    recipe = recipe_in(tmp_path)
    steps = [
        Step(kind="pull", source="/sdcard/Download/demo-*.json", target="demo.json", newest=True),
        Step(kind="pull", source="/sdcard/Download/demo.json", target="demo.json"),
        Step(kind="push", source="demo.json", target="/sdcard/Download/demo.json"),
    ]
    assert export_paths(steps, recipe) == [
        "/sdcard/Download/demo.json",
        "/sdcard/Download/demo.json",
    ]


def test_type_invalidates_the_screen_read(tmp_path):
    alpha = xml_screen(xml_node(text="Delete", clickable=True, bounds="[0,0,100,50]"))
    beta = xml_screen(xml_node(text="Other", clickable=True, bounds="[0,0,100,50]"))
    fake = FakeDevice([alpha, beta])
    worker = Runner(fake, recipe_in(tmp_path))

    worker._wait(Step(kind="wait", selector=Selector(text="Delete"), timeout=1))
    worker._type(Step(kind="type", text="new"))
    with pytest.raises(Exception, match="no matching node"):
        worker._tap(Step(kind="tap", selector=Selector(text="Delete")))

    assert fake.dumps == 2
    assert fake.taps == []


def test_failed_split_leaves_the_previous_artifact_untouched(tmp_path):
    old = artifacts.dumps({"apps": [{"id": "a"}]})
    (tmp_path / "demo.json").write_bytes(old)
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = artifacts.dumps({"apps": [{"id": "renamed"}]})
    recipe = recipe_in(tmp_path, {**VALID, "private": ["apps[id=bank]"]})
    worker = Runner(fake, recipe)
    run_dir = tmp_path / "run"
    worker.tmp = run_dir
    run_dir.mkdir()
    worker.await_file = lambda path: None

    with pytest.raises(Exception, match="matches nothing"):
        worker._pull(Step(kind="pull", source="/sdcard/Download/demo.json", target="demo.json"))

    assert (tmp_path / "demo.json").read_bytes() == old
    assert not (tmp_path / "demo.private.json").exists()
    assert list(run_dir.iterdir())


def test_pull_warns_about_a_private_path_the_export_lacks(tmp_path, capsys):
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = artifacts.dumps({"keep": 1})
    recipe = recipe_in(tmp_path, {**VALID, "private": ["data.location.value"]})
    worker = Runner(fake, recipe)
    run_dir = tmp_path / "run"
    worker.tmp = run_dir
    run_dir.mkdir()
    worker.await_file = lambda path: None

    worker._pull(Step(kind="pull", source="/sdcard/Download/demo.json", target="demo.json"))

    assert "data.location.value matches nothing" in capsys.readouterr().err
    assert json.loads((tmp_path / "demo.json").read_bytes()) == {"keep": 1}


def test_pull_refuses_a_private_file_without_loaded_selectors(tmp_path):
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    fake.pull_body = artifacts.dumps({"token": "secret"})
    recipe = recipe_in(tmp_path, {**VALID, "private": "private.json"})
    worker = Runner(fake, recipe)
    run_dir = tmp_path / "run"
    worker.tmp = run_dir
    run_dir.mkdir()
    worker.await_file = lambda path: None

    with pytest.raises(Exception, match="selectors were not loaded"):
        worker._pull(Step(kind="pull", source="/sdcard/Download/demo.json", target="demo.json"))

    assert not (tmp_path / "demo.json").exists()
