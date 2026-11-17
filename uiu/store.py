"""Discovery and storage: which recipe is which app, and where its bytes live.

Two trees, never one. `policy/data/<name>/steps.py` is committed policy that
anyone may read, and `secrets/<name>/` holds a snapshot of one phone. A recipe
that declares `private` fields is safe by construction, so its artifact lives
beside the recipe and only its overlay stays in the private tree. Every other
artifact stays entirely private.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import artifacts, split
from .recipe import STEPS_FILE, Recipe, RecipeError, load_private, parse, valid_package

DEFAULT_APPS = "policy/data"
DEFAULT_SECRETS = "secrets"
DEFAULT_STATE = ".state"


class StoreError(RuntimeError):
    """A tree that is missing, misplaced, or not text a recipe can use."""


@dataclass
class Store:
    apps: Path | str = DEFAULT_APPS
    # The private data root. It is a separate tree because an export is a
    # snapshot of one phone and the recipe beside it is policy.
    secrets: Path | str | None = None
    state: Path | str = DEFAULT_STATE

    def __post_init__(self) -> None:
        self.apps = Path(self.apps)
        chosen = (
            self.secrets
            if self.secrets not in (None, "")
            else os.environ.get("ANDROID_SECRETS_DIR") or DEFAULT_SECRETS
        )
        self.secrets = Path(chosen)
        self.state = Path(self.state)

    def data_root(self) -> Path:
        """The private export root, refused when it overlaps the recipe tree."""
        return _outside(
            Path(self.secrets or DEFAULT_SECRETS).resolve(),
            Path(self.apps).resolve(),
            "export root",
        )

    def state_root(self) -> Path:
        """The undo-copy root, refused when it sits in the recipe tree.

        An undo copy is a full export, so it must not land where a commit could
        pick it up. The same check as the export root applies.
        """
        return _outside(Path(self.state).resolve(), Path(self.apps).resolve(), "state root")

    def all(self) -> tuple[list[Recipe], list[RecipeError]]:
        """Every recipe in the tree, plus the files that did not parse.

        A broken recipe is reported rather than hidden, and the rest are still
        returned, so listing the tree stays useful while one file is edited.
        """
        root = self.data_root()
        paths = sorted(Path(self.apps).glob(f"*/{STEPS_FILE}"))
        if not paths:
            raise StoreError(f"no recipes under {Path(self.apps) / '*' / STEPS_FILE}")
        recipes: list[Recipe] = []
        errors: list[RecipeError] = []
        for path in paths:
            try:
                recipe = parse(path)
            except RecipeError as error:
                errors.append(error)
                continue
            # The export tree mirrors the recipe tree by app name, so the folder
            # a recipe sits in is the whole mapping between them.
            recipe.data_dir = root / recipe.name
            try:
                recipe = load_private(recipe)
            except RecipeError as error:
                errors.append(error)
                continue
            recipe.blob_dir = recipe.dir if recipe.is_private else recipe.data_dir
            recipes.append(recipe)
        return recipes, errors

    def load(self, package: str) -> Recipe:
        """Find an app's recipe by package id, which lives inside the file.

        A recipe that does not parse is fatal here, because the duplicate check
        is only sound when every file was read.
        """
        if not valid_package(package or ""):
            raise StoreError(f"not a package name: {package!r}")
        root = self.data_root()
        if not root.is_dir():
            raise StoreError(
                f"export root {root} is not a directory: clone the secrets repository"
                " into ./secrets, or pass --secrets"
            )
        recipes, errors = self.all()
        if errors:
            raise StoreError(str(errors[0]))
        matching = [recipe for recipe in recipes if recipe.package == package]
        if not matching:
            raise StoreError(f'no recipe declares package "{package}" under {self.apps}')
        if len(matching) > 1:
            names = ", ".join(str(recipe.dir) for recipe in matching)
            raise StoreError(f'package "{package}" is declared by more than one recipe: {names}')
        return matching[0]

    def state_of(self, recipe: Recipe) -> str:
        """One word for `uiu list`: what exists beside the recipe right now."""
        if recipe.artifact:
            stored = recipe.artifact_path()
            state = "stored" if stored.is_file() else "missing"
            if recipe.is_private:
                state += " +private" if recipe.private_path().is_file() else " no private half"
            if has_capture(recipe):
                state += " +captures"
            return state
        return "captured" if has_capture(recipe) else "no captures"

    def undo_path(self, recipe: Recipe) -> Path:
        """A new undo copy's path, keyed on the package id.

        The folder name can be renamed or reused, but a restore copy has to stay
        attributable, so the path uses the id the recipe declares.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = Path(recipe.artifact).suffix
        base = self.state_root() / "ui" / recipe.package / f"{stamp}-pre-restore"
        path = base.with_name(base.name + suffix)
        # A refused restore already wrote the copy for this second, and the
        # re-run with --force must not be blocked by the file the refusal made.
        attempt = 1
        while path.exists():
            path = base.with_name(f"{base.name}-{attempt}{suffix}")
            attempt += 1
        return path


def _outside(root: Path, apps: Path, what: str) -> Path:
    """Refuse a directory that contains, or sits inside, the recipe tree."""

    def within(child: Path, parent: Path) -> bool:
        relative = Path(os.path.relpath(child, parent))
        return relative == Path(".") or (bool(relative.parts) and relative.parts[0] != "..")

    if within(root, apps):
        raise StoreError(
            f"the {what} {root} is inside the recipe tree {apps};"
            " an export must not sit where a commit can publish it"
        )
    if within(apps, root):
        raise StoreError(
            f"the {what} {root} contains the recipe tree {apps};"
            " an export must not sit where a commit can publish it"
        )
    return root


def has_capture(recipe: Recipe) -> bool:
    directory = recipe.capture_path()
    if not directory.is_dir():
        return False
    return any(entry.is_file() for entry in directory.iterdir())


def artifact_digest(recipe: Recipe, path: Path | None = None) -> str:
    """The artifact's canonical digest.

    Hashing the raw bytes would call every export drift, because apps stamp an
    export time into the file.
    """
    body = (path or recipe.artifact_path()).read_bytes()
    return hashlib.sha256(artifacts.canonical(body, recipe.volatile, recipe.sort)).hexdigest()


def state_digest(recipe: Recipe, artifact: Path | None = None) -> str:
    """The digest of the whole state, public half and private overlay together.

    Hashing only the public artifact cannot see a change confined to a private
    value, so `verify` would call the phone unchanged and `restore` would skip
    the overlay. A missing overlay is an error rather than an empty one, because
    it is the half that cannot be fetched again.
    """
    path = artifact or recipe.artifact_path()
    if not path.is_file():
        raise StoreError(
            f"stored artifact (run `uiu backup --app {recipe.package}` first): {path} does not exist"
        )
    body = path.read_bytes()
    if recipe.is_private:
        overlay = recipe.private_path()
        if not overlay.is_file():
            raise StoreError(
                f"the private half {overlay} is missing; a backup writes it beside the artifact,"
                " and without it the state cannot be compared or restored"
            )
        body = split.merge_export(body, overlay.read_bytes(), recipe.private)
    return hashlib.sha256(artifacts.canonical(body, recipe.volatile, recipe.sort)).hexdigest()


def write_new_file(to: Path, body: bytes) -> None:
    """Write an undo copy, refusing to replace one, readable only by its owner.

    An undo copy is the only record of what the phone held before a forced
    restore, and it is a full export, so it may hold what the recipe keeps
    private.
    """
    to.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL makes the refusal atomic, and the mode is set as the file is
        # created. A chmod afterwards would leave a readable window.
        descriptor = os.open(to, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise StoreError(f"writing {to}: the file already exists") from error
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(body)
    except BaseException:
        # A half-written undo copy would look like a record of the phone.
        to.unlink(missing_ok=True)
        raise
