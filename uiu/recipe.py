"""The recipe model: one app's walk through its own screens, and its rules.

A recipe is a `backup` list of steps that exports an app's state and a `restore`
list that writes it back. A third list, `setup`, flips in-app state that has no
export to compare, and may carry a `shell` step for a device command. It is
built with the functions in `uiu.dsl`, so a misspelled selector is a Python
error before the phone is touched. Validation rejects a step that cannot run, a
bad selector or a path outside the app's own directory. An app file that
carries a recipe is loaded by `uiu.spec`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .split import SplitError, parse_selection
from .tree import Selector
from .vault import Vault
from .vault import read as vault_read

APP_FILE = "app.py"

_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Android package ids: at least two dot-separated identifiers, each starting with
# a letter or underscore. A package id also becomes a path component in the undo
# tree, so ".." must not pass.
_PACKAGE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
_DEVICE_NEWEST_RE = re.compile(r"^/sdcard/[A-Za-z0-9._/*?\[\]-]+$")


def valid_package(name: str) -> bool:
    """Whether a package id is safe to use as an id and as a path component."""
    return bool(_PACKAGE_RE.match(name))


_ACTIONS = (
    "assert",
    "capture",
    "clear",
    "ensure",
    "if_absent",
    "key",
    "launch",
    "pull",
    "push",
    "scroll",
    "shell",
    "sleep",
    "tap",
    "type",
    "wait",
)

# Argument key -> (attribute, kind). `from` and `to` are renamed because Python
# takes both words, and the rest stay as close to the file as possible.
_ARGUMENTS: dict[str, tuple[str, str]] = {
    "absent": ("absent", "bool"),
    "clear": ("clear", "bool"),
    "code": ("code", "str"),
    "command": ("command", "str"),
    "component": ("component", "str"),
    "checked": ("checked", "bool?"),
    "direction": ("direction", "str"),
    "duration": ("duration", "int"),
    "from": ("source", "str"),
    "name": ("name", "str"),
    "newest": ("newest", "bool"),
    "optional": ("optional", "bool"),
    "seconds": ("seconds", "float"),
    "selector": ("selector", "selector"),
    "steps": ("steps", "steps"),
    "text": ("text", "str"),
    "timeout": ("timeout", "float"),
    "times": ("times", "int"),
    "to": ("target", "str"),
}

_ACTION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "assert": ("checked", "selector"),
    "capture": ("name",),
    "clear": ("selector",),
    "ensure": ("checked", "selector"),
    "if_absent": ("selector", "steps"),
    "key": ("code",),
    "launch": ("clear", "component"),
    "pull": ("from", "newest", "to"),
    "push": ("from", "to"),
    "scroll": ("direction", "selector", "times"),
    "shell": ("command",),
    "sleep": ("seconds",),
    "tap": ("duration", "optional", "selector"),
    "type": ("selector", "text"),
    "wait": ("absent", "checked", "selector", "timeout"),
}


class RecipeError(ValueError):
    """A recipe file that cannot be run or cannot be compared."""


@dataclass(frozen=True)
class Step:
    kind: str
    component: str = ""
    clear: bool = False
    selector: Selector | None = None
    optional: bool = False
    duration: int = 0
    text: str = ""
    timeout: float = 0.0
    code: str = ""
    direction: str = ""
    times: int = 0
    seconds: float = 0.0
    source: str = ""
    target: str = ""
    newest: bool = False
    name: str = ""
    checked: bool | None = None
    absent: bool = False
    command: str = ""
    steps: tuple[Step, ...] = ()

    def expand(self, artifact: str, package: str) -> Step:
        def fill(value: str) -> str:
            return value.replace("{{artifact}}", artifact).replace("{{package}}", package)

        chosen = self.selector
        if chosen is not None:
            chosen = Selector(
                text=fill(chosen.text),
                resource_id=fill(chosen.resource_id),
                desc=fill(chosen.desc),
                text_contains=fill(chosen.text_contains),
                class_name=fill(chosen.class_name),
                index=chosen.index,
            )
        return replace(
            self,
            component=fill(self.component),
            text=fill(self.text),
            code=fill(self.code),
            direction=fill(self.direction),
            source=fill(self.source),
            target=fill(self.target),
            name=fill(self.name),
            command=fill(self.command),
            selector=chosen,
            steps=tuple(step.expand(artifact, package) for step in self.steps),
        )

    def summary(self) -> str:
        """The part of a step that a trace line or an error message needs."""
        if self.selector is not None:
            return str(self.selector)
        for value in (self.component, self.source, self.code, self.name, self.command):
            if value:
                return value
        if self.kind == "scroll":
            return f"{self.direction} x{self.times}"
        if self.kind == "sleep":
            return f"{self.seconds:g}s"
        return ""


@dataclass
class Recipe:
    package: str = ""
    artifact: str = ""
    volatile: list[str] = field(default_factory=list)
    redact: list[str] = field(default_factory=list)
    private: list[str] = field(default_factory=list)
    private_file: str = ""
    sort: list[str] = field(default_factory=list)
    backup: list[Step] = field(default_factory=list)
    restore: list[Step] = field(default_factory=list)
    setup: list[Step] = field(default_factory=list)

    # Filled by the store, never by the file, so a recipe cannot claim another
    # app's directory. The vault covers the export root, and None reads and
    # writes plaintext.
    dir: Path | None = None
    data_dir: Path | None = None
    blob_dir: Path | None = None
    vault: Vault | None = None

    @property
    def name(self) -> str:
        return self.dir.name if self.dir is not None else ""

    @property
    def is_private(self) -> bool:
        return bool(self.private) or bool(self.private_file)

    def artifact_path(self) -> Path:
        return _join(self.blob_dir, self.artifact)

    def private_path(self) -> Path:
        stem = _stem(self.artifact)
        return _join(self.data_dir, stem + ".private.json")

    def capture_folder(self) -> str:
        if self.artifact:
            return _stem(self.artifact) + ".d"
        return "captures"

    def capture_path(self) -> Path:
        return _join(self.data_dir, self.capture_folder())

    def expand(self) -> Recipe:
        return replace(
            self,
            backup=[step.expand(self.artifact, self.package) for step in self.backup],
            restore=[step.expand(self.artifact, self.package) for step in self.restore],
            setup=[step.expand(self.artifact, self.package) for step in self.setup],
        )

    def with_data_dir(self, directory: Path) -> Recipe:
        # The vault stays attached: a temporary directory is outside its root,
        # so a verify export is written and read as plaintext.
        return replace(self, data_dir=directory, blob_dir=directory)

    def with_artifact(self, name: str) -> Recipe:
        return replace(self, artifact=name)


def _join(root: Path | None, name: str) -> Path:
    return (root / name) if root is not None else Path(name)


def _stem(artifact: str) -> str:
    if "." not in artifact:
        return artifact
    return artifact.rsplit(".", 1)[0]


def _text(recipe: Recipe, path: Path) -> str:
    """Read a UTF-8 file through the recipe's vault, naming the path when it is not."""
    body = vault_read(recipe, path)
    try:
        return body.decode()
    except UnicodeDecodeError as error:
        raise RecipeError(f"{path}: not UTF-8: {error}") from error


def load_private(recipe: Recipe) -> Recipe:
    """Read the selector list a recipe keeps out of the repository.

    The file must exist and hold at least one selector. Treating a missing
    overlay as "no private fields" would publish exactly what it hides. The
    loaded list is validated like an inline one, so a malformed selector, or a
    path that is also redacted, cannot slip past the checks `parse` runs.
    """
    if not recipe.private_file:
        return recipe
    path = _join(recipe.data_dir, recipe.private_file)
    try:
        body = _text(recipe, path)
    except OSError as error:
        raise RecipeError(f"reading {path}: {error.strerror or error}") from error
    try:
        raw = json.loads(body)
    except ValueError as error:
        raise RecipeError(f"{path}: not JSON: {error}") from error
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RecipeError(f"{path}: a list of selectors in the private tree")
    if not raw:
        raise RecipeError(f"{path}: no selectors, so the recipe would publish every field")
    loaded = replace(recipe, private=list(raw))
    validate_private_paths(loaded)
    return loaded


def parse_private(raw: Any) -> tuple[list[str], str]:
    """Split `private` into an inline list and a file name, or reject it."""
    if raw is None:
        return [], ""
    if isinstance(raw, list):
        if not all(isinstance(item, str) for item in raw):
            raise RecipeError('"private" is a list of selectors, or the name of a file holding one')
        return list(raw), ""
    if not isinstance(raw, str):
        raise RecipeError('"private" is a list of selectors, or the name of a file holding one')
    if not raw or raw.startswith("/") or "/" in raw or " " in raw or "\t" in raw or ".." in raw:
        raise RecipeError(f'invalid private file "{raw}": a bare name inside the export directory')
    if not raw.endswith(".json"):
        raise RecipeError(f'invalid private file "{raw}": a JSON file')
    return [], raw


def parse_step(body: Any) -> Step:
    if not isinstance(body, dict):
        raise RecipeError("not an object")
    kind = ""
    for key in sorted(body):
        if key not in _ACTIONS:
            raise RecipeError(f'unknown key "{key}"')
        if kind:
            raise RecipeError(f'two actions: "{kind}" and "{key}"')
        kind = key
    if not kind:
        raise RecipeError("no action key")

    args = body[kind]
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise RecipeError(f"{kind}: argument block is an object")
    values: dict[str, Any] = {}
    allowed = _ACTION_ARGUMENTS[kind]
    for key in sorted(args):
        if key not in _ARGUMENTS:
            raise RecipeError(f'{kind}: unknown key "{key}"')
        if key not in allowed:
            raise RecipeError(f'{kind}: key "{key}" is not an argument of {kind}')
        attribute, expected = _ARGUMENTS[key]
        values[attribute] = _coerce(args[key], expected, f"{kind}: {key}")
    step = Step(kind=kind, **values)
    if step.kind == "wait" and not step.timeout:
        step = replace(step, timeout=10.0)
    if step.kind == "scroll" and not step.times:
        step = replace(step, times=1)
    return step


def _coerce(value: Any, expected: str, where: str) -> Any:
    if expected == "steps":
        if not isinstance(value, list):
            raise RecipeError(f"{where}: a list of steps")
        children: list[Step] = []
        for position, child in enumerate(value):
            try:
                children.append(parse_step(child))
            except RecipeError as error:
                raise RecipeError(f"{where} {position + 1}: {error}") from error
        return tuple(children)
    if expected == "selector":
        if not isinstance(value, dict):
            raise RecipeError(f"{where}: a selector object")
        return _parse_selector(value, where)
    if expected == "bool?":
        if value is None:
            return None
        expected = "bool"
    if expected == "bool":
        if not isinstance(value, bool):
            raise RecipeError(f"{where}: true or false")
        return value
    if expected == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise RecipeError(f"{where}: a whole number")
        return value
    if expected == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RecipeError(f"{where}: a number")
        return float(value)
    if not isinstance(value, str):
        raise RecipeError(f"{where}: a string")
    return value


_SELECTOR_KEYS = {
    "text": "text",
    "id": "resource_id",
    "desc": "desc",
    "textContains": "text_contains",
    "class": "class_name",
    "index": "index",
}


def _parse_selector(raw: dict[str, Any], where: str) -> Selector:
    values: dict[str, Any] = {}
    for key in sorted(raw):
        if key not in _SELECTOR_KEYS:
            raise RecipeError(f'{where}: unknown selector key "{key}"')
        attribute = _SELECTOR_KEYS[key]
        expected = "int" if key == "index" else "str"
        values[attribute] = _coerce(raw[key], expected, f"{where}: {key}")
    return Selector(**values)


def parse_selector(raw: dict[str, Any]) -> Selector:
    """A selector written on a command line, as `probe --tap` takes it."""
    return _parse_selector(raw, "selector")


def validate(recipe: Recipe) -> None:
    """Reject a recipe that cannot run or cannot be compared.

    Steps are judged after expansion, on what will actually run. That is sound
    because neither placeholder can introduce path syntax.
    """
    if not valid_package(recipe.package):
        raise RecipeError(f'invalid package "{recipe.package}"')
    if recipe.artifact and (
        not _ARTIFACT_RE.match(recipe.artifact) or recipe.artifact in (".", "..")
    ):
        raise RecipeError(
            f'invalid artifact "{recipe.artifact}": use only letters, digits, dot, dash, underscore'
        )
    for name, fields in (("volatile", recipe.volatile), ("redact", recipe.redact)):
        for value in fields:
            if not value or any(char in value for char in " \t/"):
                raise RecipeError(f'invalid {name} field "{value}"')
    if recipe.is_private and not recipe.artifact.endswith(".json"):
        # Splitting needs JSON. A binary artifact would land in the recipe tree
        # whole, with its private half still in it.
        raise RecipeError("private fields need a JSON artifact")
    if recipe.private_file and recipe.private:
        raise RecipeError('"private" is either a list or a file name, not both')
    validate_private_paths(recipe)
    for value in recipe.sort:
        if not value or any(char in value for char in " \t/"):
            raise RecipeError(f'invalid sort field "{value}"')
    if not recipe.backup and not recipe.setup and not recipe.restore:
        raise RecipeError("backup is empty")
    expanded = recipe.expand()
    has_pull = False
    has_capture = False
    pulls_artifact = False
    for position, step in enumerate(expanded.backup):
        try:
            validate_step(step)
        except RecipeError as error:
            raise RecipeError(f"backup step {position + 1}: {error}") from error
        if step.kind == "pull":
            has_pull = True
            if step.target == recipe.artifact:
                pulls_artifact = True
        elif step.kind == "capture":
            has_capture = True
        elif step.kind == "push":
            raise RecipeError(f"backup step {position + 1}: push belongs in restore, not backup")

    for position, step in enumerate(expanded.setup):
        try:
            validate_step(step)
        except RecipeError as error:
            raise RecipeError(f"setup step {position + 1}: {error}") from error

    pushes_artifact = False
    for position, step in enumerate(expanded.restore):
        try:
            validate_step(step)
        except RecipeError as error:
            raise RecipeError(f"restore step {position + 1}: {error}") from error
        if step.kind == "pull":
            raise RecipeError(f"restore step {position + 1}: pull belongs in backup, not restore")
        if step.kind == "push" and step.source == recipe.artifact:
            pushes_artifact = True

    if not recipe.restore:
        # A recipe with no restore list is a capture-only record. It must
        # capture something, and it must not export a file nothing reads back.
        if recipe.artifact:
            raise RecipeError("artifact is set but there is no restore flow")
        if not recipe.backup:
            # A setup-only recipe writes app state that has no export to compare,
            # so it has neither an artifact nor a capture to account for.
            return
        if has_pull:
            raise RecipeError("restore is empty but backup exports an artifact")
        if not has_capture:
            raise RecipeError("backup neither exports nor captures anything")
        return
    if not recipe.artifact:
        raise RecipeError("restore is set but artifact is empty")
    if not pushes_artifact:
        raise RecipeError(f'restore never pushes the artifact "{recipe.artifact}"')
    # A backup-less recipe pushes a file that is already stored: an app whose
    # backup can be imported but never written back. There is no export to pull,
    # so the pull check does not apply.
    if recipe.backup and (not has_pull or not pulls_artifact):
        raise RecipeError(f'backup never pulls the artifact "{recipe.artifact}"')


def validate_private_paths(recipe: Recipe) -> None:
    """Check the selector list itself, whether it came inline or from a file.

    This is separate from `validate` because a recipe loads its private file
    after parsing, and at that moment the list and its file name both exist.
    """
    if len(set(recipe.private)) != len(recipe.private):
        raise RecipeError("a private selector is listed twice")
    for value in recipe.private:
        if not value or any(char in value for char in " \t/"):
            raise RecipeError(f'invalid private field "{value}"')
        try:
            parse_selection(value)
        except SplitError as error:
            raise RecipeError(str(error)) from error
        if value in recipe.redact:
            raise RecipeError(
                f'"{value}" is both redacted and private: a value is thrown away or kept, not both'
            )


def validate_step(step: Step) -> None:
    if step.optional and step.kind != "tap":
        raise RecipeError("optional is only valid on tap")
    if step.kind == "launch":
        if not step.component:
            raise RecipeError("launch needs component")
        if "/" not in step.component:
            raise RecipeError(f'component "{step.component}" is not package/activity')
    elif step.kind == "tap":
        if step.selector is None:
            raise RecipeError("tap needs selector")
        if step.duration < 0:
            raise RecipeError("tap duration is negative")
    elif step.kind == "type":
        if not step.text:
            raise RecipeError("type needs text")
    elif step.kind in ("clear", "wait", "assert", "ensure"):
        if step.selector is None:
            raise RecipeError(f"{step.kind} needs selector")
        if step.kind in ("assert", "ensure") and step.checked is None:
            raise RecipeError(f"{step.kind} needs checked")
        if step.kind == "wait" and step.checked is not None and step.absent:
            raise RecipeError("wait is either absent or checked, not both")
        if step.kind == "wait" and step.timeout <= 0:
            raise RecipeError("wait timeout must be positive")
    elif step.kind == "key":
        if not _KEY_RE.match(step.code):
            raise RecipeError(f'invalid key code "{step.code}"')
    elif step.kind == "shell":
        if not step.command:
            raise RecipeError("shell needs command")
        if not step.command.strip():
            raise RecipeError("shell command is empty")
        if "\n" in step.command:
            raise RecipeError("shell command spans lines")
    elif step.kind == "scroll":
        if step.direction not in ("up", "down"):
            raise RecipeError(f'scroll direction "{step.direction}" is not up or down')
        if step.times < 1:
            raise RecipeError("scroll times must be at least 1")
    elif step.kind == "sleep":
        if step.seconds <= 0:
            raise RecipeError("sleep seconds must be positive")
    elif step.kind in ("pull", "push"):
        if not step.source or not step.target:
            raise RecipeError(f"{step.kind} needs from and to")
        if step.newest and step.kind != "pull":
            raise RecipeError("newest is only valid on pull")
        # The local side names a file in one of the recipe's two places: the
        # artifact in the export root, or a payload beside app.py. Nothing
        # outside them is reachable from a step.
        local = step.source if step.kind == "push" else step.target
        if local.startswith("/"):
            raise RecipeError(
                f'{step.kind} local path "{local}" must be relative to the export directory'
            )
        parts = local.split("/")
        if ".." in parts:
            raise RecipeError(f'{step.kind} local path "{local}" leaves the export directory')
        if parts[-1] == APP_FILE:
            raise RecipeError(f'{step.kind} local path "{local}" would overwrite the app file')
        if step.newest and (not _DEVICE_NEWEST_RE.match(step.source) or ".." in step.source):
            raise RecipeError(f'newest needs a plain glob under /sdcard, got "{step.source}"')
    elif step.kind == "capture":
        if not _ARTIFACT_RE.match(step.name) or step.name in (".", ".."):
            raise RecipeError(f'invalid capture name "{step.name}"')
    elif step.kind == "if_absent":
        if step.selector is None:
            raise RecipeError("if_absent needs selector")
        if not step.steps:
            raise RecipeError("if_absent needs at least one step")
        for position, child in enumerate(step.steps):
            if child.kind in ("pull", "push", "capture", "if_absent"):
                raise RecipeError(f"if_absent may not contain {child.kind}")
            try:
                validate_step(child)
            except RecipeError as error:
                raise RecipeError(f"if_absent step {position + 1}: {error}") from error
    else:
        raise RecipeError(f'unknown step "{step.kind}"')
    if step.selector is not None:
        if step.selector.empty:
            raise RecipeError("selector names no field")
        if step.selector.index < 0:
            raise RecipeError("selector index is negative")
