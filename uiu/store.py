"""Discovery and storage: which app is which, and where its bytes live.

One tree of committed policy: `policy/data/<name>/app.py`, one file per app,
holding its package, install facts, device state and screen walk. The private
tree, `secrets/<name>/`, holds a snapshot of one phone, the app files that must
not be public, and the selector lists that name private entries. App files and
selector lists are plaintext policy, not export data: they are read as
plaintext, and only the export files beside them are encrypted. An app that
declares `private` fields is safe by construction, so its artifact lives beside
its app file and only its overlay stays in the private tree. Every other
artifact stays entirely private.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from . import artifacts, split, vault
from .recipe import APP_FILE, Recipe, RecipeError, load_private, valid_package
from .spec import App, parse

DEFAULT_APPS = "policy/data"
DEFAULT_SECRETS = "secrets"
DEFAULT_STATE = ".state"


class StoreError(RuntimeError):
    """A tree that is missing, misplaced, or not text an app file can use."""


@dataclass
class Store:
    apps: Path | str = DEFAULT_APPS
    # The private data root. It is a separate tree because an export is a
    # snapshot of one phone, and it also holds app files that must not be
    # public.
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
        """The private export root, refused when it overlaps the policy tree."""
        return _outside(
            Path(self.secrets or DEFAULT_SECRETS).resolve(),
            Path(self.apps).resolve(),
            "export root",
        )

    def state_root(self) -> Path:
        """The undo-copy root, refused when it overlaps the policy or private tree.

        An undo copy is a full export, so it must not land where a commit could
        pick it up. It is also plaintext at 0600, so it must not sit inside the
        encrypted root or over it: the mode hides it from other users, not from
        anyone holding the tree.
        """
        state = Path(self.state).resolve()
        _outside(state, Path(self.apps).resolve(), "state root")
        secrets = self.data_root()
        if _within(state, secrets):
            raise StoreError(
                f"the state root {state} is inside the private tree {secrets};"
                " an undo copy is plaintext, so it must not sit under the encrypted root"
            )
        if _within(secrets, state):
            raise StoreError(
                f"the state root {state} contains the private tree {secrets};"
                " an undo copy is plaintext, so the encrypted root must not sit inside it"
            )
        return state

    def all(self) -> tuple[list[App], list[RecipeError]]:
        """Every app file in the policy and private trees, plus the files that did not parse.

        A broken file is reported rather than hidden, and the rest are still
        returned, so listing the tree stays useful while one file is edited.
        The vault is attached but its selectors stay unloaded, so this never
        decrypts and needs no key. An app file in the private tree is plaintext
        policy: it is read directly, and the export files beside it are the
        encrypted half.
        """
        root = self.data_root()
        paths = app_files(self.apps, self.secrets)
        if not paths:
            raise StoreError(
                f"no app files under {Path(self.apps) / '*' / APP_FILE} or {root / '*' / APP_FILE}"
            )
        apps: list[App] = []
        errors: list[RecipeError] = []
        for path in paths:
            try:
                app = parse(path)
                # The export tree mirrors the app folder by name, so the folder
                # an app file sits in is the whole mapping between them.
                if app.recipe is not None:
                    app = replace(app, recipe=_with_data(app.recipe, path.parent, root / app.name))
            except RecipeError as error:
                errors.append(error)
                continue
            apps.append(app)
        return apps, errors

    def load(self, package: str) -> App:
        """Find an app by package id, which lives inside the file.

        An app file that does not parse is fatal here, because the duplicate
        check is only sound when every file was read. The selectors are loaded
        here, the one place a recipe is about to run, so `all` can list the
        tree without the key.
        """
        if not valid_package(package or ""):
            raise StoreError(f"not a package name: {package!r}")
        root = self.data_root()
        if not root.is_dir():
            raise StoreError(
                f"export root {root} is not a directory: clone the secrets repository"
                " into ./secrets, or pass --secrets"
            )
        apps, errors = self.all()
        if errors:
            raise StoreError(str(errors[0]))
        matching = [app for app in apps if app.package == package]
        if not matching:
            raise StoreError(f'no app file declares package "{package}" under {self.apps}')
        if len(matching) > 1:
            names = ", ".join(str(app.dir) for app in matching)
            raise StoreError(f'package "{package}" is declared by more than one app file: {names}')
        found = matching[0]
        if found.recipe is None:
            return found
        return replace(found, recipe=load_private(found.recipe))

    def state_of(self, app: App) -> str:
        """One word for `uiu list`: what exists beside the app file right now.

        A covered file that is not a SOPS envelope is marked `plaintext`: the
        store promises ciphertext at rest, and a file left bare by a migration
        otherwise looks exactly like a healthy one. The raw bytes are tested,
        never decrypted, so listing still needs no key.
        """
        recipe = app.recipe
        if recipe is None:
            return "policy only"
        if recipe.artifact:
            stored = recipe.artifact_path()
            state = "stored" if stored.is_file() else "missing"
            # Either half can be bare while its pair is gone, and both are
            # covered, so each is checked on its own.
            if _unsealed(recipe, stored) or (
                recipe.is_private and _unsealed(recipe, recipe.private_path())
            ):
                state += " plaintext"
            if recipe.is_private:
                state += " +private" if recipe.private_path().is_file() else " no private half"
            if has_capture(recipe):
                state += " +captures"
            return state
        return "captured" if has_capture(recipe) else "no captures"

    def undo_path(self, app: App) -> Path:
        """A new undo copy's path, keyed on the package id.

        The folder name can be renamed or reused, but a restore copy has to stay
        attributable, so the path uses the id the app declares.
        """
        recipe = app.recipe
        if recipe is None:
            raise StoreError(f"{app.package} has no screen walk, so no undo copy")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = Path(recipe.artifact).suffix
        base = self.state_root() / "ui" / app.package / f"{stamp}-pre-restore"
        path = base.with_name(base.name + suffix)
        # A refused restore already wrote the copy for this second, and the
        # re-run with --force must not be blocked by the file the refusal made.
        attempt = 1
        while path.exists():
            path = base.with_name(f"{base.name}-{attempt}{suffix}")
            attempt += 1
        return path


def app_files(apps: Path | str = DEFAULT_APPS, secrets: Path | str | None = None) -> list[Path]:
    """Every app file, one per folder name, from the policy and private trees.

    An app file in the private tree is plaintext policy. The default policy
    tree pairs with the default private tree; a custom `apps` root with no
    explicit `secrets` is self-contained, so a fixture does not inherit the
    repository's private tree. A folder that exists in both trees is an error:
    one of them would silently shadow the other.
    """
    store = Store(apps=apps, secrets=secrets)
    root = store.data_root()
    public = sorted(Path(store.apps).glob(f"*/{APP_FILE}"))
    private: list[Path] = []
    if secrets is not None or Path(store.apps).resolve() == Path(DEFAULT_APPS).resolve():
        private = sorted(root.glob(f"*/{APP_FILE}")) if root.is_dir() else []
    paths = sorted(public + private, key=lambda path: path.parent.name)
    seen: dict[str, Path] = {}
    for path in paths:
        name = path.parent.name
        other = seen.get(name)
        if other is not None:
            raise StoreError(
                f"the app folder {name} exists in both trees: {other} and {path};"
                " a private app file replaces the public one, so remove the duplicate"
            )
        seen[name] = path
    return paths


def _with_data(recipe: Recipe, directory: Path, data: Path) -> Recipe:
    """Attach the app's own folder, export root and vault, without reading selectors.

    `directory` is where a private artifact is committed beside the app file,
    and `data` is the export root every other artifact lives in. For an app
    file in the private tree the two are the same folder, so its artifact is
    stored there too. The vault covers that root and decrypts on read, but
    nothing is read here: `load` loads the selectors, so listing never touches
    the keyring.
    """
    recipe.dir = directory
    recipe.data_dir = data
    recipe.vault = vault.Vault(root=data.parent)
    recipe.blob_dir = recipe.dir if recipe.is_private else data
    return recipe


def _within(child: Path, parent: Path) -> bool:
    """Whether `child` is `parent`, or sits inside it."""
    relative = Path(os.path.relpath(child, parent))
    return relative == Path(".") or (bool(relative.parts) and relative.parts[0] != "..")


def _outside(root: Path, apps: Path, what: str) -> Path:
    """Refuse a directory that contains, or sits inside, the policy tree."""
    if _within(root, apps):
        raise StoreError(
            f"the {what} {root} is inside the policy tree {apps};"
            " an export must not sit where a commit can publish it"
        )
    if _within(apps, root):
        raise StoreError(f"the {what} {root} contains the policy tree {apps}")
    return root


def _unsealed(recipe: Recipe, path: Path) -> bool:
    """Whether a stored file the vault covers is not a SOPS envelope."""
    if recipe.vault is None or not recipe.vault.covers(path) or not path.is_file():
        return False
    try:
        return not vault.encrypted(path.read_bytes())
    except OSError:
        return False


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
    body = vault.read(recipe, path or recipe.artifact_path())
    return hashlib.sha256(
        artifacts.canonical(body, recipe.volatile, recipe.sort, omit=recipe.omit)
    ).hexdigest()


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
    body = vault.read(recipe, path)
    if recipe.is_private:
        overlay = recipe.private_path()
        if not overlay.is_file():
            raise StoreError(
                f"the private half {overlay} is missing; a backup writes it beside the artifact,"
                " and without it the state cannot be compared or restored"
            )
        body = split.merge_export(body, vault.read(recipe, overlay), recipe.private)
    return hashlib.sha256(
        artifacts.canonical(body, recipe.volatile, recipe.sort, omit=recipe.omit)
    ).hexdigest()


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
