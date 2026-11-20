"""Transparent SOPS encryption for the private tree.

Every export file `uiu` reads or writes under the export root is a SOPS
envelope at rest, so the private tree can be committed and synced as it is.
App files and the selector list are policy: they stay plaintext, and `read`
passes them through. Binary mode keeps every covered file byte for byte, which
a zip needs.

A vault is attached to a recipe by the store. A recipe without one reads and
writes plaintext, which is what lets a fixture that writes a bare directory and
a tree that has not been migrated yet keep working.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import secretspec

if TYPE_CHECKING:
    from .recipe import Recipe

REASON = "uiu reads and writes the encrypted private tree"

_KEY = "SOPS_AGE_KEY"
_CONFIG = ".sops.yaml"
_TIMEOUT = 120
_TAIL_LINES = 5
_TAIL_CHARS = 400
_CANNOT_OPEN = f"the envelope does not open with {_KEY}; check .sops.yaml and the key"


class VaultError(RuntimeError):
    """The private tree could not be encrypted or decrypted."""


def encrypted(body: bytes) -> bool:
    """Whether a body is a SOPS envelope written in binary mode.

    A binary envelope is a JSON object with one `data` string and the `sops`
    metadata beside it. Checking the shape, never the content, is what tells a
    committed plaintext file from an encrypted one during migration.
    """
    if not body.lstrip().startswith(b"{"):
        return False
    try:
        raw = json.loads(body)
    except ValueError:
        return False
    if not isinstance(raw, dict):
        return False
    data = raw.get("data")
    sops = raw.get("sops")
    if not isinstance(data, str) or not data.startswith("ENC["):
        return False
    if not isinstance(sops, dict):
        return False
    mac = sops.get("mac")
    return isinstance(mac, str) and mac.startswith("ENC[")


class Vault:
    """One private root, and the age identity that opens it.

    A path is covered when it resolves under `root`. `verify` runs its export
    into a temporary directory and re-points the recipe there, so those files
    stay plaintext without a second code path.
    """

    def __init__(
        self,
        root: Path,
        *,
        manifest: Path | None = None,
        identity: Callable[[], str] | None = None,
        run: Callable[[Sequence[str], bytes], bytes] | None = None,
    ) -> None:
        self.root: Path = Path(root).resolve()
        self.manifest: Path = Path(manifest) if manifest is not None else _default_manifest()
        # Neither the identity nor sops runs until a covered byte has to be
        # encrypted or decrypted, so listing apps never touches the keyring.
        self.identity: Callable[[], str] = identity if identity is not None else self._from_manifest
        self.run: Callable[[Sequence[str], bytes], bytes] = run if run is not None else self._sops
        self._age: str | None = None

    def covers(self, path: Path) -> bool:
        """Whether `path` resolves inside the private root."""
        try:
            Path(path).resolve().relative_to(self.root)
        except ValueError:
            return False
        return True

    def read(self, path: Path) -> bytes:
        """The plaintext of `path`, decrypting it when it is a covered envelope."""
        body = Path(path).read_bytes()
        if not self.covers(path) or not encrypted(body):
            return body
        try:
            return self._run(self._decrypt_argv(str(path)), b"")
        except VaultError as error:
            raise VaultError(f"{path}: {error}") from error

    def write(self, path: Path, body: bytes) -> bytes:
        """The bytes to store at `path`: an envelope when the path is covered.

        A covered path never receives plaintext, so a sops that reports success
        without producing an envelope is an error rather than a leak. The
        envelope is then opened with the same identity before it is returned:
        an ambient recipient or a stale `.sops.yaml` can address it to a key
        this vault does not hold.
        """
        if not self.covers(path):
            return body
        try:
            envelope = self._run(self._encrypt_argv(path), body)
        except VaultError as error:
            raise VaultError(f"{path}: {error}") from error
        if not encrypted(envelope):
            raise VaultError(f"{path}: sops did not produce an envelope")
        try:
            opened = self._run(self._decrypt_argv("/dev/stdin"), envelope)
        except VaultError as error:
            raise VaultError(f"{path}: {_CANNOT_OPEN}") from error
        if opened != body:
            raise VaultError(f"{path}: {_CANNOT_OPEN}")
        return envelope

    def _decrypt_argv(self, source: str) -> list[str]:
        # Creation rules pick recipients only on encrypt, so a read does not
        # need `.sops.yaml`: a tree whose config was deleted still opens.
        return [
            "decrypt",
            "--input-type",
            "binary",
            "--output-type",
            "binary",
            source,
        ]

    def _encrypt_argv(self, path: Path) -> list[str]:
        # Binary input over stdin, with the destination pinned so the creation
        # rule that picks the recipient is the one for the file's real name.
        return [
            "--config",
            str(self.root / _CONFIG),
            "encrypt",
            "--input-type",
            "binary",
            "--output-type",
            "binary",
            "--filename-override",
            str(path),
            "/dev/stdin",
        ]

    def _run(self, argv: Sequence[str], body: bytes) -> bytes:
        self._key()
        return self.run(argv, body)

    def _key(self) -> str:
        """The age identity, resolved once and only when a file needs it."""
        if self._age is None:
            try:
                value = self.identity().strip()
            except VaultError:
                raise
            except Exception as error:
                raise VaultError(self._missing(str(error))) from error
            if not value:
                raise VaultError(self._missing("the secret is empty"))
            self._age = value
        return self._age

    def _missing(self, detail: str) -> str:
        return (
            f"cannot read {_KEY} from {self.manifest}: {detail};"
            f" set it with `secretspec set {_KEY}`"
        )

    def _from_manifest(self) -> str:
        try:
            resolved = secretspec.resolve(path=str(self.manifest), reason=REASON)
            value = resolved.secrets[_KEY].get
        except Exception as error:
            raise VaultError(self._missing(str(error))) from error
        if not value:
            raise VaultError(self._missing("the secret is empty"))
        return value

    def _sops(self, argv: Sequence[str], body: bytes) -> bytes:
        command = ["sops", *argv]
        try:
            result = subprocess.run(
                command,
                input=body,
                capture_output=True,
                env=_child_environment(self._key()),
                timeout=_TIMEOUT,
                check=False,
            )
        except FileNotFoundError as error:
            raise VaultError("sops is not on PATH; the devenv carries it") from error
        except subprocess.TimeoutExpired as error:
            raise VaultError(f"`{' '.join(command)}` timed out after {_TIMEOUT}s") from error
        if result.returncode != 0:
            raise VaultError(f"sops exited {result.returncode}: {_tail(result.stderr)}")
        return result.stdout


def read(recipe: Recipe, path: Path) -> bytes:
    """The plaintext of `path` for `recipe`, or its bytes when it has no vault."""
    vault = recipe.vault
    return vault.read(path) if vault is not None else Path(path).read_bytes()


def write(recipe: Recipe, path: Path, body: bytes) -> bytes:
    """The bytes to store at `path` for `recipe`, encrypted when it is covered."""
    vault = recipe.vault
    return vault.write(path, body) if vault is not None else body


def _default_manifest() -> Path:
    return Path(os.environ.get("SECRETSPEC_FILE") or "secretspec.toml")


def _child_environment(identity: str) -> dict[str, str]:
    """The parent environment without any SOPS variable, plus the identity.

    An ambient `SOPS_AGE_RECIPIENTS` outranks `.sops.yaml`, so inheriting it
    would address a fresh envelope to a recipient this vault cannot open.
    """
    env = {name: value for name, value in os.environ.items() if not name.startswith("SOPS_")}
    env[_KEY] = identity
    return env


def _tail(body: bytes) -> str:
    """The end of sops' stderr, where the reason it failed actually is."""
    lines = [line.strip() for line in body.decode(errors="replace").splitlines() if line.strip()]
    text = " | ".join(lines[-_TAIL_LINES:])
    return text[-_TAIL_CHARS:] if text else "no output"
