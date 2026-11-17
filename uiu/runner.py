"""Step execution: the interpreter that turns a recipe into gestures and files.

Every step that reads the screen goes through one `snapshot`, so the rule "dump
immediately before acting" cannot be skipped by a new step kind, and a `wait`
that matched leaves its dump for the tap that follows. One dump costs about
150 ms here, against 2.5 s for a `uiautomator dump` process (an XML copy of the
screen) plus a pull, so a wait-then-tap pair is one read rather than two.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree

from . import artifacts, split
from .device import DeviceError, UiDevice
from .recipe import Recipe, Step
from .tree import Node, Selector, TreeError, hit_target, parse_dump, resolve


def selector_of(step: Step) -> Selector:
    """The selector a step of this kind must carry.

    Validation rejects `tap`, `clear`, `wait` and `assert` without one, so a
    missing selector here means a step was built by hand. Naming that beats a
    None-shaped crash inside the tree walk.
    """
    if step.selector is None:
        raise DeviceError(f"{step.kind} has no selector")
    return step.selector


class StepError(RuntimeError):
    """A step that could not run, carrying its position and the step itself."""

    def __init__(self, position: int, step: Step, cause: Exception):
        self.position: int = position
        self.step: Step = step
        self.cause: Exception = cause
        super().__init__(f"step {position + 1} ({step.kind}): {cause}")


def run(device: UiDevice, recipe: Recipe, steps: list[Step], trace: bool = False) -> None:
    """Execute steps in order.

    A run starts from the app's own root screen: the package is stopped first,
    because a resumed app lands on whatever page the last run left open. Device
    files the run will read or write are removed before it starts and again when
    it ends, so a stale export cannot be pulled as if fresh and the next run
    starts without leftovers of its own.
    """
    worker = Runner(device, recipe)
    worker.tmp = Path(tempfile.mkdtemp(prefix="uiu-"))
    try:
        if worker.has_newest(steps):
            # Read before any step can write an export, so "newer than this run"
            # is measured on the phone's own clock.
            worker.started = device.device_clock()
        device.force_stop(recipe.package)
        device.wait_gone(recipe.package)
        worker.clear_exports(steps)
        try:
            for position, step in enumerate(steps):
                step = step.expand(recipe.artifact, recipe.package)
                began = time.monotonic()
                try:
                    worker.step(step)
                except Exception as error:
                    raise StepError(position, step, error) from error
                if trace:
                    elapsed = time.monotonic() - began
                    print(
                        f"trace {position + 1:2d} {step.kind:<7} {elapsed:6.2f}s {step.summary()}",
                        file=sys.stderr,
                    )
        finally:
            worker.clear_exports(steps, strict=False)
    finally:
        shutil.rmtree(worker.tmp, ignore_errors=True)


def export_paths(steps: list[Step], recipe: Recipe) -> list[str]:
    """The device paths a run reads or writes.

    A newest pull is skipped: its source is the app's own backup folder, which
    must not be deleted.
    """
    paths = []
    for step in steps:
        step = step.expand(recipe.artifact, recipe.package)
        if step.kind == "pull" and not step.newest:
            paths.append(step.source)
        elif step.kind == "push":
            paths.append(step.target)
    return paths


def merge_file(recipe: Recipe) -> bytes:
    """The artifact with its private overlay put back.

    A restore saves an undo copy before it overwrites anything, and that copy
    has to be this file. The artifact alone is missing whatever the recipe keeps
    out of the repository, so undoing with it would drop those entries from the
    phone.
    """
    public = recipe.artifact_path().read_bytes()
    if not recipe.is_private:
        return public
    overlay = recipe.private_path()
    try:
        private = overlay.read_bytes()
    except OSError as error:
        raise DeviceError(
            f"reading {overlay}: {error.strerror or error}; the undo copy would be missing every private entry"
        ) from error
    return split.merge_export(public, private, recipe.private)


class Runner:
    def __init__(self, device: UiDevice, recipe: Recipe):
        self.device: UiDevice = device
        self.recipe: Recipe = recipe
        # The run's private staging directory, created by `run`. A pull stages
        # here and is renamed into place, so the final path never holds a
        # partial or raw export.
        self.tmp: Path | None = None
        # The phone's clock when the run began, in seconds.
        self.started: int = 0
        self._pulls: int = 0
        self._nodes: list[Node] | None = None
        self._body: str | None = None

    def has_newest(self, steps: list[Step]) -> bool:
        return any(
            step.expand(self.recipe.artifact, self.recipe.package).kind == "pull" and step.newest
            for step in steps
        )

    def stale(self) -> None:
        """Drop the cached dump after anything that can change the screen."""
        self._nodes = None
        self._body = None

    def snapshot(self) -> tuple[list[Node], str]:
        if self._nodes is not None and self._body is not None:
            return self._nodes, self._body
        body = self.device.dump()
        try:
            nodes = parse_dump(body)
        except TreeError as error:
            raise DeviceError(str(error)) from error
        self._nodes, self._body = nodes, body
        return nodes, body

    def clear_exports(self, steps: list[Step], strict: bool = True) -> None:
        for path in export_paths(steps, self.recipe):
            if not path.startswith("/sdcard/") or ".." in path:
                error = DeviceError(f'export path "{path}" is not under /sdcard')
                if strict:
                    raise error
                continue
            result = self.device.shell(["rm", "-f", path])
            if strict and not result.ok:
                raise DeviceError(f"removing {path}: {result.output.strip()}")

    def step(self, step: Step) -> None:
        handler = getattr(self, "_" + step.kind, None)
        if handler is None:
            raise DeviceError(f'unknown step "{step.kind}"')
        handler(step)

    # Steps

    def _launch(self, step: Step) -> None:
        self.stale()
        self.device.launch(step.component, step.clear)

    def _tap(self, step: Step) -> None:
        nodes, _ = self.snapshot()
        try:
            node = resolve(nodes, selector_of(step))
        except TreeError:
            if step.optional:
                # An optional tap dismisses a dialog: the dialog being absent is
                # the outcome the run wanted, so the screen it found stays cached
                # for the next step.
                return
            raise
        target = hit_target(nodes, node)
        x, y = target.bounds.centre
        self.stale()
        self._press(x, y, step.duration)

    def _press(self, x: int, y: int, duration: int) -> None:
        if duration > 0:
            self.device.long_press(x, y, duration)
        else:
            self.device.tap(x, y)

    def _type(self, step: Step) -> None:
        if step.selector is not None:
            nodes, _ = self.snapshot()
            node = resolve(nodes, step.selector)
            x, y = hit_target(nodes, node).bounds.centre
            self.stale()
            self.device.tap(x, y)
        # The field's content changed, and its label or the row layout may have
        # followed, so the next screen read must not reuse the pre-typing dump.
        self.device.set_text(step.text)
        self.stale()

    def _clear(self, step: Step) -> None:
        nodes, _ = self.snapshot()
        node = resolve(nodes, selector_of(step))
        x, y = hit_target(nodes, node).bounds.centre
        self.stale()
        self.device.tap(x, y)
        self.device.clear_text()
        nodes, _ = self.snapshot()
        node = resolve(nodes, selector_of(step))
        if not node.text:
            return
        # The focused-field call was refused and the key fallback did not finish
        # the job: the field's own text says how many deletes are left.
        left = len(node.text)
        self.device.shell(["input", "keyevent"] + ["KEYCODE_DEL"] * left)
        self.stale()
        nodes, _ = self.snapshot()
        node = resolve(nodes, selector_of(step))
        if node.text:
            raise DeviceError(f'field still holds "{node.text}" after deleting {left} characters')

    def _wait(self, step: Step) -> None:
        deadline = time.monotonic() + step.timeout
        last: Exception = DeviceError("no dump yet")
        while True:
            try:
                nodes, _ = self.snapshot()
                resolve(nodes, selector_of(step))
                return
            except (TreeError, DeviceError) as error:
                last = error
            if time.monotonic() > deadline:
                raise DeviceError(f"waited {step.timeout:.0f}s: {last}") from last
            # The screen does not match yet, so the next attempt reads it again.
            # A short pause keeps a 30 s wait from issuing two hundred dumps and
            # loading the UI thread of the app it waits on.
            self.stale()
            time.sleep(0.15)

    def _key(self, step: Step) -> None:
        self.stale()
        self.device.key(step.code)

    def _scroll(self, step: Step) -> None:
        self.stale()
        width, height = self.device.screen_size()
        x = width // 2
        near, far = height * 3 // 4, height // 4
        start, end = (far, near) if step.direction == "up" else (near, far)
        for _ in range(step.times):
            self.device.swipe(x, start, x, end, 300)
            # A lazy-loading list needs a beat after the gesture before its next
            # page exists, or the next swipe outruns it.
            time.sleep(1.0)

    def _sleep(self, step: Step) -> None:
        self.stale()
        time.sleep(step.seconds)

    def _pull(self, step: Step) -> None:
        final = self.local_path(step.target)
        if step.newest:
            remote = self.newest(step.source)
        else:
            remote = step.source
            self.await_file(remote)
        # The copy lands in the run's temporary directory, never at the final
        # path. For a private recipe the final path is in the committed tree, and
        # a failure between the pull and the split must not leave the raw export
        # there where a commit could publish it.
        self._pulls += 1
        staged = self.staging() / f"{self._pulls}-{final.name}"
        self.fetch(remote, staged)
        body = staged.read_bytes()
        if self.recipe.redact:
            body = artifacts.redact(body, self.recipe.redact)
        overlay = None
        if self.recipe.is_private:
            if not self.recipe.private:
                raise DeviceError(
                    "the recipe declares a private file but its selectors were not loaded;"
                    " publishing this export would leak every value it hides"
                )
            result = split.split_export(body, self.recipe.private)
            body, overlay = result.public, result.private
            for field in result.unmatched:
                # Not fatal: an app may omit a field entirely, and the recipe
                # keeps a guard for the day it appears. A typo looks the same,
                # so the author must see that the guard did not fire.
                print(
                    f"warning: private field {field} matches nothing in this export",
                    file=sys.stderr,
                )
        self.publish(final, body, overlay)

    def staging(self) -> Path:
        if self.tmp is None:
            raise DeviceError(
                "this run has no staging directory; pulls need one that is not the tree"
            )
        return self.tmp

    def publish(self, final: Path, body: bytes, overlay: bytes | None) -> None:
        """Replace the artifact and its overlay only once both are complete.

        Each file is written under a temporary name in its own directory, then
        renamed into place, so a failure leaves the previous pair untouched and
        a reader never sees half a file. The artifact keeps the mode a public
        file has; the overlay is owner-only at 0600 because it is the half that
        may not be published.
        """
        final.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(final, body, 0o644)
        if overlay is not None:
            path = self.recipe.private_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_atomic(path, overlay, 0o600)

    @staticmethod
    def _write_atomic(path: Path, body: bytes, mode: int) -> None:
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(body)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def fetch(self, remote: str, local: Path) -> None:
        """Pull a file, retrying until the copy is provably the whole file.

        A pull can report success having written nothing. That happened here: an
        app had created its export but not flushed it, the size held steady for
        one poll, and the copy came back empty, so an artifact once hashed as
        the empty string. The remote size is read again after the pull, so a
        short, empty or mid-write copy is retried instead of committed.
        """
        size = -1
        copied = 0
        for _ in range(4):
            self.device.pull(remote, local)
            size = self.size_of(remote)
            copied = local.stat().st_size if local.exists() else 0
            if size > 0 and copied == size:
                return
            time.sleep(0.4)
        raise DeviceError(f"pulled {copied} of {size} bytes from {remote} after 4 attempts")

    def _push(self, step: Step) -> None:
        local = self.local_path(step.source)
        self.device.push(self.pushable(local), step.target)

    def _capture(self, step: Step) -> None:
        _, body = self.snapshot()
        directory = self.recipe.capture_path()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (step.name + ".xml")).write_text(indent_xml(body))

    def _assert(self, step: Step) -> None:
        nodes, _ = self.snapshot()
        target = hit_target(nodes, resolve(nodes, selector_of(step)))
        if target.checked != step.checked:
            raise DeviceError(
                f"checked is {str(target.checked).lower()}, want {str(step.checked).lower()}"
            )

    # Files

    def local_path(self, path: str) -> Path:
        if path.startswith("/"):
            raise DeviceError(
                f"{path} is absolute; a recipe only names files in the app's own directory"
            )
        if self.recipe.blob_dir is None:
            raise DeviceError(f"{path} needs the app's blob directory")
        return self.recipe.blob_dir / path

    def pushable(self, local: Path) -> Path:
        """The file a push sends: the artifact with its private overlay merged back in.

        The merged copy is written to the run's temporary directory, so the
        committed artifact never carries the values. A recipe with private fields
        must have the overlay: pushing the zeroed half would reset the phone to
        blanks instead of restoring it.
        """
        if not self.recipe.is_private:
            return local
        overlay = self.recipe.private_path()
        try:
            private = overlay.read_bytes()
        except OSError as error:
            raise DeviceError(
                f"reading {overlay}: {error.strerror or error}; a recipe with private fields needs it beside the artifact"
            ) from error
        merged = split.merge_export(local.read_bytes(), private, self.recipe.private)
        out = self.staging() / ("push-" + local.name)
        out.write_bytes(merged)
        os.chmod(out, 0o600)
        return out

    # Waiting for the device to finish writing

    def await_file(self, path: str) -> None:
        self.await_ready(path, time.monotonic() + 15)

    def size_of(self, path: str) -> int:
        result = self.device.shell(["stat", "-c", "%s", path])
        if not result.ok:
            return -1
        try:
            return int(result.output.strip())
        except ValueError:
            return -1

    def await_ready(self, path: str, deadline: float) -> None:
        """Wait for a non-zero size that is unchanged since the previous look.

        An app creates its export before it finishes writing it, and a pull in
        between reads a short file. That is how an artifact once came back empty.
        """
        last = -1
        while True:
            size = self.size_of(path)
            if size > 0 and size == last:
                return
            last = size
            if time.monotonic() > deadline:
                raise DeviceError(f"{path} was still being written when the wait ran out")
            time.sleep(0.15)

    def newest(self, pattern: str) -> str:
        """Wait for the file this run produced and return the most recent match.

        The app writes a timestamped name, so the run's own export is the one
        whose mtime is at or after the run started. An older export from a
        previous run is left alone and ignored.
        """
        deadline = time.monotonic() + 30
        command = "stat -c '%Y %n' " + pattern + " 2>/dev/null | sort -n | tail -1"
        while True:
            result = self.device.shell(command)
            if result.ok:
                fields = result.output.strip().split(" ", 1)
                if len(fields) == 2 and fields[0].isdigit() and int(fields[0]) >= self.started:
                    path = fields[1]
                    if not path.startswith("/sdcard/"):
                        raise DeviceError(
                            f'{pattern} resolved to "{path}", which is not under /sdcard'
                        )
                    self.await_ready(path, deadline)
                    return path
            if time.monotonic() > deadline:
                raise DeviceError(f"no file matching {pattern} appeared within 30s")
            time.sleep(0.25)


def dump(device: UiDevice) -> list[Node]:
    """The current screen, parsed, for a probe that runs no recipe."""
    body = device.dump()
    try:
        return parse_dump(body)
    except TreeError as error:
        raise DeviceError(str(error)) from error


def tap(device: UiDevice, selector: Selector, timeout: float = 15.0) -> None:
    """Resolve one selector and tap the node a finger would hit, waiting for it.

    A probe chain taps a screen the previous tap has only started to open, and a
    cold app paints its tree seconds after its window takes focus.
    """
    deadline = time.monotonic() + timeout
    while True:
        nodes = dump(device)
        try:
            node = resolve(nodes, selector)
        except TreeError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.25)
            continue
        x, y = hit_target(nodes, node).bounds.centre
        device.tap(x, y)
        return


def indent_xml(body: str) -> str:
    """Re-indent a dump so a capture is readable and diffable."""
    root = ElementTree.fromstring(body)
    ElementTree.indent(root, space="  ")
    return ElementTree.tostring(root, encoding="unicode") + "\n"
