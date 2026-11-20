"""The commands: probe output, verify drift, and the restore guard."""

from __future__ import annotations

import io
import json
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import ENVELOPE, VALID, FakeDevice, app_file, recipe_from, xml_node, xml_screen

from uiu import artifacts, commands
from uiu.device import DeviceError
from uiu.runner import StepError
from uiu.store import Store
from uiu.tree import TreeError


def write_store(root, *, artifact: bytes | None):
    apps = root / "apps"
    secrets = root / "secrets"
    (apps / "demo").mkdir(parents=True)
    secrets.mkdir()
    (apps / "demo" / "app.py").write_text(app_file())
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


def write_push_only_store(root, *, artifact: bytes | None):
    apps = root / "apps"
    secrets = root / "secrets"
    (apps / "demo").mkdir(parents=True)
    secrets.mkdir()
    recipe = {
        "package": "com.example.demo",
        "artifact": "payload.zip",
        "restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/Download/payload.zip"}}],
    }
    (apps / "demo" / "app.py").write_text(app_file(recipe))
    if artifact is not None:
        (secrets / "demo").mkdir(parents=True)
        (secrets / "demo" / "payload.zip").write_bytes(artifact)
    return Store(apps=apps, secrets=secrets, state=root / "state")


def test_restore_without_a_backup_pushes_the_stored_file_and_never_exports(tmp_path):
    store = write_push_only_store(tmp_path, artifact=b"stored zip")
    fake = FakeDevice(xml_screen(xml_node(text="x")))
    out = io.StringIO()

    assert commands.restore(store, "com.example.demo", None, out=out, device=fake) == 0

    stored = tmp_path / "secrets" / "demo" / "payload.zip"
    assert out.getvalue() == (
        f"restoring com.example.demo from {stored} (no export to compare against)\n"
        f"restored com.example.demo from {stored}\n"
    )
    assert fake.pushed == [("/sdcard/Download/payload.zip", b"stored zip")]
    # No export: nothing was pulled and no undo copy was written.
    assert fake.pulls == []
    assert not (tmp_path / "state").exists()


def test_restore_without_a_backup_refuses_a_missing_stored_file(tmp_path):
    store = write_push_only_store(tmp_path, artifact=None)
    fake = FakeDevice(xml_screen(xml_node(text="x")))

    with pytest.raises(commands.CommandError, match="payload.zip does not exist") as caught:
        commands.restore(store, "com.example.demo", None, out=io.StringIO(), device=fake)

    stored = tmp_path / "secrets" / "demo" / "payload.zip"
    assert str(stored) in str(caught.value)
    assert fake.pushes == []


def test_backup_refuses_a_recipe_without_backup_steps(tmp_path):
    store = write_push_only_store(tmp_path, artifact=b"stored zip")
    fake = FakeDevice(xml_screen(xml_node(text="x")))

    with pytest.raises(commands.CommandError, match="has no backup flow"):
        commands.backup(store, "com.example.demo", None, device=fake)

    assert fake.stopped == []


def test_verify_refuses_a_capture_only_recipe(tmp_path):
    apps = tmp_path / "apps"
    (apps / "demo").mkdir(parents=True)
    (tmp_path / "secrets").mkdir()
    capture = {"package": "com.example.demo", "backup": [{"capture": {"name": "main"}}]}
    (apps / "demo" / "app.py").write_text(app_file(capture))
    store = Store(apps=apps, secrets=tmp_path / "secrets", state=tmp_path / "state")

    with pytest.raises(commands.CommandError, match="capture-only"):
        commands.verify(
            store, "com.example.demo", None, device=FakeDevice(xml_screen(xml_node(text="x")))
        )


def test_backup_refuses_a_policy_only_app(tmp_path):
    apps = tmp_path / "apps"
    (apps / "demo").mkdir(parents=True)
    (tmp_path / "secrets").mkdir()
    (apps / "demo" / "app.py").write_text(app_file({"package": "com.example.demo"}))
    store = Store(apps=apps, secrets=tmp_path / "secrets", state=tmp_path / "state")

    with pytest.raises(commands.CommandError, match="has no screen walk"):
        commands.backup(store, "com.example.demo", None, device=FakeDevice(xml_screen()))


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
    (tmp_path / "apps" / "broken" / "app.py").write_text("APP = 1")
    out = io.StringIO()

    assert commands.list_recipes(store, out=out) == 1
    text = out.getvalue()
    assert "error    " in text
    assert "com.example.demo" in text


def test_list_does_not_need_the_private_selectors(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    secrets = tmp_path / "secrets"
    (apps / "demo").mkdir(parents=True)
    (secrets / "demo").mkdir(parents=True)
    (apps / "demo" / "app.py").write_text(app_file({**VALID, "private": "private.json"}))
    (secrets / "demo" / "private.json").write_bytes(ENVELOPE)
    # Any attempt to resolve the key would fail: the manifest does not exist.
    monkeypatch.setenv("SECRETSPEC_FILE", str(tmp_path / "missing.toml"))
    store = Store(apps=apps, secrets=secrets, state=tmp_path / "state")
    out = io.StringIO()

    assert commands.list_recipes(store, out=out) == 0
    assert "com.example.demo" in out.getvalue()


def test_export_temp_removes_its_directory_after_an_interrupt(tmp_path, monkeypatch):
    directory = tmp_path / "uiu-verify"
    directory.mkdir()
    monkeypatch.setattr(
        commands, "tempfile", SimpleNamespace(mkdtemp=lambda prefix: str(directory))
    )

    class Interrupted(FakeDevice):
        def shell(self, command, timeout=60):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        commands.export_temp(
            Interrupted(xml_screen(xml_node(text="x"))), recipe_from(VALID), io.StringIO(), False
        )

    assert not directory.exists()


def write_private_store(root, *, public: bytes, overlay: bytes):
    apps = root / "apps"
    secrets = root / "secrets"
    (apps / "demo").mkdir(parents=True)
    (secrets / "demo").mkdir(parents=True)
    (apps / "demo" / "app.py").write_text(app_file({**VALID, "private": ["settings.token"]}))
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


def write_setup_store(root, steps):
    apps = root / "apps"
    secrets = root / "secrets"
    (apps / "demo").mkdir(parents=True)
    secrets.mkdir()
    (apps / "demo" / "app.py").write_text(app_file({"package": "com.example.demo", "setup": steps}))
    return Store(apps=apps, secrets=secrets, state=root / "state")


def test_setup_runs_the_walk_in_order_and_prints_the_summary(tmp_path):
    store = write_setup_store(
        tmp_path,
        [
            {"shell": {"command": "cmd statusbar add-tile x"}},
            {"assert": {"selector": {"text": "On"}, "checked": True}},
        ],
    )
    fake = FakeDevice(xml_screen(xml_node(text="On", checkable=True, checked=True)))
    out = io.StringIO()

    assert commands.setup(store, "com.example.demo", None, out=out, device=fake) == 0

    assert fake.shells == ["cmd statusbar add-tile x"]
    assert out.getvalue() == "setup com.example.demo (2 steps)\n"


def test_setup_pushes_a_payload_committed_beside_the_app_file(tmp_path):
    store = write_setup_store(
        tmp_path,
        [{"push": {"from": "payload.json", "to": "/sdcard/Download/payload.json"}}],
    )
    (tmp_path / "apps" / "demo" / "payload.json").write_bytes(b'{"keep": 1}')
    fake = FakeDevice(xml_screen(xml_node(text="x")))

    assert commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake) == 0

    assert fake.pushes == [
        (tmp_path / "apps" / "demo" / "payload.json", "/sdcard/Download/payload.json")
    ]


def test_setup_refuses_an_app_without_a_setup_walk(tmp_path):
    store = write_store(tmp_path, artifact=None)
    fake = FakeDevice(xml_screen(xml_node(text="x")))

    with pytest.raises(commands.CommandError, match="has no setup walk"):
        commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake)

    assert fake.stopped == []


def test_setup_needs_no_artifact(tmp_path):
    store = write_setup_store(tmp_path, [{"assert": {"selector": {"text": "On"}, "checked": True}}])
    fake = FakeDevice(xml_screen(xml_node(text="On", checkable=True, checked=True)))
    out = io.StringIO()

    assert commands.setup(store, "com.example.demo", None, out=out, device=fake) == 0
    assert not (tmp_path / "secrets" / "demo" / "demo.json").exists()
    assert "artifact" not in out.getvalue()


def test_ensure_leaves_a_row_that_already_matches_alone(tmp_path):
    store = write_setup_store(
        tmp_path,
        [{"ensure": {"checked": True, "selector": {"text": "Dark theme"}}}],
    )
    fake = FakeDevice(xml_screen(xml_node(text="Dark theme", checkable=True, checked=True)))

    assert commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake) == 0

    assert fake.taps == []
    assert fake.dumps == 1


def test_ensure_taps_a_mismatched_row_once_and_waits_for_it_to_flip(tmp_path):
    store = write_setup_store(
        tmp_path,
        [{"ensure": {"checked": True, "selector": {"text": "Dark theme"}}}],
    )
    off = xml_screen(xml_node(text="Dark theme", checkable=True, checked=False))
    on = xml_screen(xml_node(text="Dark theme", checkable=True, checked=True))
    fake = FakeDevice([off, on])

    assert commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake) == 0

    assert fake.taps == [(50, 50)]
    assert fake.dumps == 2


def switch_screen(*, checked: bool) -> str:
    """A settings row whose TextView label sits left of a sibling switch."""
    return xml_screen(
        xml_node(text="Sync", bounds="[0,0,200,100]"),
        xml_node(
            cls="android.widget.Switch",
            checkable=True,
            checked=checked,
            bounds="[800,0,1000,100]",
        ),
    )


def test_a_wait_for_absence_passes_when_nothing_matches(tmp_path):
    store = write_setup_store(
        tmp_path, [{"wait": {"absent": True, "selector": {"text": "Dialog"}}}]
    )
    fake = FakeDevice(xml_screen(xml_node(text="Home")))

    assert commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake) == 0

    assert fake.dumps == 1


def test_a_wait_for_absence_fails_while_the_node_is_there(tmp_path):
    store = write_setup_store(
        tmp_path,
        [{"wait": {"absent": True, "selector": {"text": "Dialog"}, "timeout": 0.2}}],
    )
    fake = FakeDevice(xml_screen(xml_node(text="Dialog")))

    with pytest.raises(StepError, match="is still on screen"):
        commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake)


def test_a_wait_for_checked_reads_the_sibling_switch(tmp_path):
    store = write_setup_store(
        tmp_path, [{"wait": {"checked": True, "selector": {"text": "Sync"}, "timeout": 1}}]
    )
    fake = FakeDevice(switch_screen(checked=True))

    assert commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake) == 0

    assert fake.dumps == 1


def test_a_wait_for_checked_fails_while_the_switch_is_off(tmp_path):
    store = write_setup_store(
        tmp_path, [{"wait": {"checked": True, "selector": {"text": "Sync"}, "timeout": 0.2}}]
    )
    fake = FakeDevice(switch_screen(checked=False))

    with pytest.raises(StepError, match="checked is still false, want true"):
        commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake)


def test_scroll_until_returns_after_the_swipe_that_reveals_it(tmp_path):
    store = write_setup_store(
        tmp_path,
        [{"scroll": {"direction": "down", "times": 3, "selector": {"text": "Target"}}}],
    )
    fake = FakeDevice([xml_screen(xml_node(text="Top")), xml_screen(xml_node(text="Target"))])

    assert commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake) == 0

    assert len(fake.swipes) == 2


def test_scroll_until_fails_after_the_swipes_it_was_allowed(tmp_path):
    store = write_setup_store(
        tmp_path,
        [{"scroll": {"direction": "down", "times": 2, "selector": {"text": "Target"}}}],
    )
    fake = FakeDevice(xml_screen(xml_node(text="Top")))

    with pytest.raises(StepError, match=r"scrolled 2 time\(s\) down without finding"):
        commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake)

    assert len(fake.swipes) == 2


def test_a_failed_read_does_not_satisfy_a_wait_for_absence(tmp_path):
    class BlindDevice(FakeDevice):
        """A dump that fails, as a locked or mid-transition screen does."""

        def dump(self) -> str:
            raise DeviceError("dump failed")

    store = write_setup_store(
        tmp_path,
        [{"wait": {"absent": True, "selector": {"text": "Dialog"}, "timeout": 0.2}}],
    )
    fake = BlindDevice(xml_screen(xml_node(text="Home")))

    with pytest.raises(StepError, match="dump failed"):
        commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake)


IF_ABSENT_DEVICE_CASES = [
    ("selector absent", ["A", "B"], [(50, 50), (150, 50)], 2),
    ("selector present", ["Brave", "A", "B"], [], 1),
]


@pytest.mark.parametrize(
    ("name", "labels", "taps", "dumps"),
    IF_ABSENT_DEVICE_CASES,
    ids=[case[0] for case in IF_ABSENT_DEVICE_CASES],
)
def test_if_absent_runs_its_children_only_when_the_selector_misses(
    tmp_path, name, labels, taps, dumps
):
    store = write_setup_store(
        tmp_path,
        [
            {
                "if_absent": {
                    "selector": {"text": "Brave"},
                    "steps": [
                        {"tap": {"selector": {"text": "A"}}},
                        {"tap": {"selector": {"text": "B"}}},
                    ],
                }
            }
        ],
    )
    screen = xml_screen(
        *(
            xml_node(
                text=label, clickable=True, bounds=f"[{index * 100},0,{index * 100 + 100},100]"
            )
            for index, label in enumerate(labels)
        )
    )
    fake = FakeDevice(screen)

    assert commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake) == 0

    assert fake.taps == taps
    assert fake.dumps == dumps


def test_a_failed_dump_inside_an_if_absent_block_is_not_an_absence(tmp_path):
    class BlindDevice(FakeDevice):
        def dump(self) -> str:
            raise DeviceError("dump failed")

    store = write_setup_store(
        tmp_path,
        [
            {
                "if_absent": {
                    "selector": {"text": "Brave"},
                    "steps": [{"tap": {"selector": {"text": "A"}}}],
                }
            }
        ],
    )
    fake = BlindDevice(xml_screen(xml_node(text="A")))

    with pytest.raises(StepError, match="dump failed"):
        commands.setup(store, "com.example.demo", None, out=io.StringIO(), device=fake)

    assert fake.taps == []
