"""Step execution against a fake device: caching, targeting, transfer, waiting."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from support import VALID, FakeDevice, recipe_in, xml_node, xml_screen

from uiu import artifacts
from uiu.device import ShellResult
from uiu.recipe import Selector, Step
from uiu.runner import Runner, StepError, export_paths, run


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
