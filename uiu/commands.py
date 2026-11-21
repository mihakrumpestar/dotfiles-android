"""The six commands: list, probe, backup, setup, verify, restore.

Two rules live here. Every command that touches the device unlocks it first, and
every recipe run that fails prints the screen it failed on, because the next
question after "no matching node" is always what was there instead.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from . import artifacts, runner, vault
from . import store as stores
from .device import Device, DeviceError, UiDevice
from .recipe import (
    Recipe,
    RecipeError,
    Step,
    parse_selector,
    parse_step,
    validate_step,
)
from .spec import App
from .tree import Selector, TreeError, probe_rows, render_probe, unlabelled_clickable


class CommandError(RuntimeError):
    """A command that could not do what it was asked, with nothing left to try."""


def _recipe_for(app: App) -> Recipe:
    """The app's screen walk, refused when it has none.

    A policy-only app has no backup or restore, so a UI command naming it is a
    mistake worth one line rather than a None-shaped crash.
    """
    if app.recipe is None:
        raise CommandError(f"{app.package} has no screen walk: nothing to back up or restore")
    return app.recipe


def _format_chain(chain: Sequence[tuple[str, str]]) -> list[Selector | Step]:
    """Turn probe's arguments into an ordered list of taps and steps.

    A tap is the common case and stays shorthand. A step is the same JSON a
    recipe holds, so a long press or a key that a recipe already spells can be
    replayed here before it goes into the file.
    """
    import json

    prepared: list[Selector | Step] = []
    for kind, raw in chain:
        try:
            body = json.loads(raw)
        except ValueError as error:
            raise CommandError(f"--{kind} {raw}: {error}") from error
        if not isinstance(body, dict):
            raise CommandError(f"--{kind} {raw}: a JSON object")
        if kind == "tap":
            try:
                selector = parse_selector(body)
            except RecipeError as error:
                raise CommandError(f"--tap {raw}: {error}") from error
            if selector.empty:
                raise CommandError(f"--tap {raw} names nothing to match on")
            prepared.append(selector)
            continue
        try:
            step = parse_step(body)
            validate_step(step)
        except RecipeError as error:
            raise CommandError(f"--step {raw}: {error}") from error
        if step.kind in ("capture", "pull", "push"):
            # A probe prints a screen; it has no app directory and no artifact
            # name, so a file-writing step would land in the working directory.
            raise CommandError(f"--step {raw}: a probe cannot run {step.kind}, it writes files")
        prepared.append(step)
    return prepared


def _device(serial: str | None, device: UiDevice | None = None) -> UiDevice:
    chosen = device if device is not None else Device(serial)
    chosen.unlock()
    return chosen


def list_recipes(store: stores.Store, out: TextIO | None = None) -> int:
    """List every recipe: what it declares, and what is stored for it.

    It reads only the tree, so it answers without a device and without the key:
    the private selectors are loaded by `Store.load`, which a run needs.
    """
    out = out or sys.stdout
    apps, errors = store.all()
    for app in apps:
        artifact = (app.recipe.artifact if app.recipe else "") or "-"
        print(
            f"{app.name:<24} {app.package:<32} {artifact:<20} {store.state_of(app)}",
            file=out,
        )
    root = store.data_root()
    print(f"secrets  {root}{'' if root.is_dir() else ' (missing)'}", file=out)
    for error in errors:
        print(f"error    {error}", file=out)
    return 1 if errors else 0


def probe(
    serial: str | None,
    launch: str = "",
    clear: bool = False,
    chain: Sequence[tuple[str, str]] = (),
    show_all: bool = False,
    out: TextIO | None = None,
    device: UiDevice | None = None,
) -> int:
    """Print the screen as ready-to-paste selectors, tapping as it goes.

    Writing a recipe needs the tree at each step, so a probe that takes one
    `--tap` per navigation step turns a manual walk of the app into one command
    whose arguments are the recipe's own steps.
    """
    stream: TextIO = out or sys.stdout
    if clear and not launch:
        raise CommandError("--clear needs --launch: there is nothing to stop first")
    prepared = _format_chain(chain)
    target = _device(serial, device)
    if launch:
        target.launch(launch, clear)
    worker = runner.Runner(target, Recipe())
    for argument in prepared:
        try:
            if isinstance(argument, Selector):
                runner.tap(target, argument)
                # A tap that opens a screen has to land before the next read; the
                # dump itself waits for idle and the next tap waits for its selector.
                time.sleep(0.2)
            else:
                worker.step(argument)
        except TreeError:
            _print_screen(target, stream)
            raise
    focus = target.focus()
    nodes = runner.dump(target)
    rows = probe_rows(nodes, focus=focus, show_all=show_all)
    print(render_probe(focus, rows, unlabelled_clickable(nodes)), file=stream, end="")
    return 0


def _run(device: UiDevice, recipe: Recipe, steps: list[Step], out: TextIO, trace: bool) -> None:
    try:
        runner.run(device, recipe, steps, trace=trace)
    except runner.StepError:
        _print_screen(device, out)
        raise


def _print_screen(device: UiDevice, out: TextIO) -> None:
    """Print the controls that were on screen when a step failed.

    The next question after "no matching node" is always what was there instead,
    and fetching it by hand costs another adb round trip. A read that fails here
    is swallowed, because the step's own error is the one worth reporting.
    """
    try:
        nodes = runner.dump(device)
    except DeviceError:
        return
    try:
        focus = device.focus()
    except DeviceError:
        focus = ""
    print("screen when it failed", file=out)
    rows = probe_rows(nodes, focus=focus, show_all=False)
    print(render_probe(focus, rows, unlabelled_clickable(nodes)), file=out, end="")


def backup(
    store: stores.Store,
    package: str,
    serial: str | None,
    out: TextIO | None = None,
    trace: bool = False,
    device: UiDevice | None = None,
) -> int:
    """Run a recipe's backup steps and report the artifact's digest."""
    stream: TextIO = out or sys.stdout
    app = store.load(package)
    recipe = _recipe_for(app)
    if not recipe.backup:
        # A push-only recipe has a restore but nothing to export, so a backup of
        # it would run no steps and leave the stored file untouched.
        raise CommandError(f"{package} has no backup flow")
    target = _device(serial, device)

    if not recipe.artifact:
        # A capture-only recipe has no artifact to hash; the captures are the
        # output, and they are what a later review diffs.
        _run(target, recipe, recipe.backup, stream, trace)
        print(f"captured {recipe.capture_path()}", file=stream)
        return 0

    stored = recipe.artifact_path()
    began = time.time()
    _run(target, recipe, recipe.backup, stream, trace)
    if not stored.is_file():
        raise CommandError(f"the recipe finished but {stored} did not appear")
    # A pull that never ran leaves the previous artifact in place, so the
    # modification time is what proves this run produced the file. This holds
    # because a pull does not preserve the device mtime.
    if stored.stat().st_mtime < began - 1:
        raise CommandError(
            f"{stored} was not written by this run (modtime {datetime.fromtimestamp(stored.stat().st_mtime).isoformat()})"
        )
    # The digest is reported, not stored: it is a function of the artifact, so
    # writing it down would only create a second copy that can go stale.
    print(f"artifact {stored}\nsha256   {stores.artifact_digest(recipe, stored)}", file=stream)
    return 0


def setup(
    store: stores.Store,
    package: str,
    serial: str | None,
    out: TextIO | None = None,
    trace: bool = False,
    device: UiDevice | None = None,
) -> int:
    """Run a recipe's setup steps against the app.

    A setup walk flips in-app state that has no export to compare, such as a
    toggle or an imported config. It runs its steps and verifies itself through
    `assert_checked`, so it needs no artifact, no digest and no undo copy.
    """
    stream: TextIO = out or sys.stdout
    app = store.load(package)
    recipe = _recipe_for(app)
    if not recipe.setup:
        raise CommandError(f"{package} has no setup walk")
    target = _device(serial, device)
    _run(target, recipe, recipe.setup, stream, trace)
    print(f"setup {package} ({len(recipe.setup)} steps)", file=stream)
    return 0


def verify(
    store: stores.Store,
    package: str,
    serial: str | None,
    out: TextIO | None = None,
    trace: bool = False,
    device: UiDevice | None = None,
) -> int:
    """Compare a fresh export with the stored artifact.

    The fresh export goes to a temporary directory, so verifying changes
    nothing on disk. An unchanged result prints `same` and exits zero; drift
    prints what differed and raises, because a shell script must see it.
    """
    stream: TextIO = out or sys.stdout
    app = store.load(package)
    recipe = _recipe_for(app)
    if not recipe.artifact:
        raise CommandError(f"{package} is capture-only: there is no artifact to verify")
    target = _device(serial, device)

    # The whole state, overlay included: the public artifact alone cannot see a
    # value that changed inside the private half.
    want_state = stores.state_digest(recipe)
    want_public = stores.artifact_digest(recipe)
    got_state, path, temp, cleanup = export_temp(target, recipe, stream, trace)
    try:
        got_public = stores.artifact_digest(temp, path)
        if got_state == want_state:
            print(f"same     {package} {want_public}", file=stream)
            return 0
        if got_public == want_public:
            print(
                f"drift    {package} private values differ, public artifact unchanged ({want_public})",
                file=stream,
            )
            print(
                "run backup to record the new values, or restore --force to put the stored ones back",
                file=stream,
            )
        else:
            print(f"drift    {package} stored {want_public}, device {got_public}", file=stream)
            print(diff_artifacts(recipe, path), file=stream, end="")
        raise CommandError(f"{package} differs from the stored state")
    finally:
        cleanup()


def restore(
    store: stores.Store,
    package: str,
    serial: str | None,
    force: bool = False,
    out: TextIO | None = None,
    trace: bool = False,
    device: UiDevice | None = None,
) -> int:
    """Write a stored artifact back through the app's import UI, guarded.

    The phone is exported first and compared with the stored state. A difference
    is saved as an undo copy and refused unless `force` is set, so a restore
    cannot silently discard a value the recipe never exported.

    A recipe with no backup steps has no export to compare against, so it skips
    the guard and pushes the stored file as it is.
    """
    stream: TextIO = out or sys.stdout
    app = store.load(package)
    recipe = _recipe_for(app)
    if not recipe.artifact or not recipe.restore:
        raise CommandError(f"{package} has no restore flow")
    stored = recipe.artifact_path()
    if not recipe.backup:
        # There is no export, so the stored file is the only state there is:
        # nothing to compare it with and nothing to save as an undo copy.
        if not stored.is_file():
            raise CommandError(f"stored artifact {stored} does not exist")
        target = _device(serial, device)
        print(f"restoring {package} from {stored} (no export to compare against)", file=stream)
        _run(target, recipe, recipe.restore, stream, trace)
        print(f"restored {package} from {stored}", file=stream)
        return 0
    target = _device(serial, device)

    want_state = stores.state_digest(recipe)
    want_public = stores.artifact_digest(recipe)
    got_state, path, temp, cleanup = export_temp(target, recipe, stream, trace)
    try:
        got_public = stores.artifact_digest(temp, path)
        if got_state == want_state:
            print(f"already matches, nothing to restore ({package} {want_public})", file=stream)
            return 0
        if got_public == want_public:
            print(
                f"drift    {package} private values differ, public artifact unchanged ({want_public})",
                file=stream,
            )
        else:
            print(f"drift    {package} stored {want_public}, device {got_public}", file=stream)
            print(diff_artifacts(recipe, path), file=stream, end="")

        undo = store.undo_path(app)
        # The undo copy is the whole export, private entries included. Copying
        # the split artifact would leave a file that cannot put them back.
        stores.write_new_file(undo, runner.merge_file(temp))
        print(f"undo copy: {undo}", file=stream)

        if not force:
            raise CommandError(
                f"{package} differs from the stored state; re-run with --force to overwrite it"
                f" (undo copy: {undo})"
            )
        _run(target, recipe, recipe.restore, stream, trace)
        # The import is asynchronous, so "restored" is only true if a fresh
        # export now matches. A dialog the app left open would otherwise be
        # reported as success.
        after_state, _, _, after_cleanup = export_temp(target, recipe, stream, trace)
        try:
            if after_state != want_state:
                raise CommandError(
                    f"{package} was imported but the device state still differs from the stored state"
                    f" (undo copy: {undo})"
                )
        finally:
            after_cleanup()
        print(f"restored {package} from {stored}", file=stream)
        return 0
    finally:
        cleanup()


def export_temp(
    device: UiDevice, recipe: Recipe, out: TextIO, trace: bool
) -> tuple[str, Path, Recipe, Callable[[], None]]:
    """Run the backup steps into an empty temporary directory.

    The artifact name keeps its extension so the app's own file filter still
    shows it, and the marked stem keeps it from colliding with anything the app
    wrote itself.
    """
    directory = Path(tempfile.mkdtemp(prefix="uiu-verify-"))
    complete = False

    def cleanup() -> None:
        shutil.rmtree(directory, ignore_errors=True)

    temp = recipe.with_data_dir(directory).with_artifact(temp_artifact(recipe.artifact))
    try:
        _run(device, temp, temp.backup, out, trace)
        path = temp.artifact_path()
        digest = stores.state_digest(temp)
        complete = True
        return digest, path, temp, cleanup
    finally:
        # `finally` rather than `except Exception`: an interrupt leaves a
        # plaintext full export behind, and that is the one thing this
        # directory must never survive.
        if not complete:
            cleanup()


def temp_artifact(artifact: str) -> str:
    """Keep the extension and add a marked stem, e.g. yamlauncher.verify-20261110T173000.json."""
    suffix = Path(artifact).suffix
    stem = artifact[: -len(suffix)] if suffix else artifact
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stem}.verify-{stamp}{suffix}"


def diff_artifacts(recipe: Recipe, fresh: Path) -> str:
    """The meaningful difference between the stored artifact and a fresh export.

    Volatile fields are masked, so the timestamp noise is not what the reader
    sees. A binary artifact gets no diff: a line diff of bytes is noise, and the
    hashes already say they differ.
    """
    try:
        stored = vault.read(recipe, recipe.artifact_path())
        body = vault.read(recipe, fresh)
    except OSError:
        return ""
    if not _is_text(stored) or not _is_text(body):
        return ""
    old = artifacts.canonical(stored, recipe.volatile, recipe.sort, omit=recipe.omit)
    new = artifacts.canonical(body, recipe.volatile, recipe.sort, omit=recipe.omit)
    return artifacts.diff(old, new, recipe.artifact)


def _is_text(body: bytes) -> bool:
    if b"\x00" in body:
        return False
    try:
        body.decode()
    except UnicodeDecodeError:
        return False
    return True
