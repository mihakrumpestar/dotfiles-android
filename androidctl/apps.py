"""Install an app from its release, verified against a recorded checksum.

An app is one directory under `policy/data`, or under the private tree when the
app itself must not be public. Its `app.py` carries the package id, and
optionally the release URL, the checksum source, one asset per ABI and a
starter, under `install=`. The same file holds the app's device state and its
screen walk, so an install and a restore cannot disagree about the app.

Every install resolves the current release, so the URL must point at the latest
release endpoint: a pinned tag would go stale.

The checksum is the point. Android checks that an APK is signed, not who signed
it, so this module verifies the publisher's `.sha256` or, when none is
published, the digest GitHub records for the uploaded asset, and refuses to
return a file whose bytes do not match.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen

from uiu.recipe import RecipeError, valid_package
from uiu.spec import App, parse
from uiu.store import DEFAULT_APPS, StoreError, app_files

UNIVERSAL = "universal"

# A stalled connection must fail instead of hanging a bootstrap.
TIMEOUT = 120

# Read and hash in chunks so an APK never has to fit in memory.
CHUNK = 64 * 1024

_KEYS = ("assets", "checksum", "hint", "release", "start")
_START_KEYS = ("library", "process")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_BARE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class AppError(RuntimeError):
    """A release, checksum, or download that cannot be trusted."""


@dataclass(frozen=True)
class StartSpec:
    """The native starter a release ships, and the process it spawns."""

    library: str
    process: str


@dataclass(frozen=True)
class AppSpec:
    """One app's install facts, keyed by the package it provides."""

    name: str
    package: str
    release: str
    assets: dict[str, str]
    checksum: str
    hint: str = ""
    start: StartSpec | None = None


@dataclass(frozen=True)
class Asset:
    """One file attached to a release."""

    name: str
    url: str
    size: int = 0
    digest: str = ""


@dataclass(frozen=True)
class Release:
    """The subset of GitHub's release payload this needs."""

    tag: str
    assets: tuple[Asset, ...] = ()

    def find(self, name: str) -> Asset:
        """The asset called `name`. A missing name is an error, never a fallback."""
        for asset in self.assets:
            if asset.name == name:
                return asset
        raise AppError(f'release {self.tag} has no asset "{name}"')

    def find_checksum(self, apk: Asset) -> Asset:
        """The publisher's `.sha256` asset for an APK.

        A release without one is an error: skipping verification is not an
        option.
        """
        return self.find(apk.name + ".sha256")


def load(
    package: str, apps: Path | str = DEFAULT_APPS, secrets: Path | str | None = None
) -> AppSpec:
    """Resolve an app by package id, reading its app file."""
    if not valid_package(package):
        raise AppError(f'not a package name: "{package}"')

    try:
        paths = app_files(apps, secrets)
    except StoreError as error:
        raise AppError(str(error)) from error
    specs: list[AppSpec] = []
    for path in paths:
        app = _app(path)
        if app.install is None:
            # An app with no release facts is installed some other way, such as
            # the Play Store, so it cannot answer `task install`.
            continue
        specs.append(_spec(path, app))

    matching = [spec for spec in specs if spec.package == package]
    if not matching:
        raise AppError(
            f'no app file declares an install for "{package}" in the policy or private tree'
        )
    if len(matching) > 1:
        names = ", ".join(str(spec.name) for spec in matching)
        raise AppError(f'package "{package}" is declared by more than one app file: {names}')
    return matching[0]


def _app(path: Path) -> App:
    """Read one app file, turning its parse error into an install error."""
    try:
        return parse(path)
    except RecipeError as error:
        raise AppError(str(error)) from error


def _spec(path: Path, app: App) -> AppSpec:
    """Validate one app's install facts into an install spec."""
    name = app.name
    if not _NAME_RE.match(name):
        raise AppError(
            f'{path}: folder "{name}" is not a normalized app name;'
            " use lowercase letters, digits, dot, dash or underscore"
        )
    payload: dict[str, Any] = app.install or {}
    for key in payload:
        if key not in _KEYS:
            raise AppError(f'{path}: unknown install field "{key}"')

    release = payload.get("release")
    if not isinstance(release, str) or not release.startswith("https://"):
        raise AppError(f'{path}: "release" is an https URL')
    if not urlparse(release).path.rstrip("/").endswith("/releases/latest"):
        raise AppError(
            f'{path}: "release" must end in /releases/latest, so an install cannot go stale'
        )

    checksum = payload.get("checksum")
    if checksum not in ("asset", "digest"):
        raise AppError(f'{path}: "checksum" is "asset" or "digest"')

    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, dict):
        raise AppError(f'{path}: "assets" maps an ABI to a release asset name')
    assets: dict[str, str] = {}
    for abi, asset_name in raw_assets.items():
        if not isinstance(asset_name, str) or not asset_name:
            raise AppError(f'{path}: "assets" maps an ABI to a release asset name')
        assets[str(abi)] = asset_name
    if not assets:
        raise AppError(f'{path}: "assets" maps an ABI to a release asset name')
    if UNIVERSAL not in assets:
        raise AppError(f'{path}: "assets" needs a "{UNIVERSAL}" fallback')

    hint = payload.get("hint", "")
    if not isinstance(hint, str):
        raise AppError(f'{path}: "hint" is a string')

    start = _start(path, payload.get("start"))
    return AppSpec(
        name=name,
        package=app.package,
        release=release,
        assets=assets,
        checksum=checksum,
        hint=hint,
        start=start,
    )


def _start(path: Path, raw: Any) -> StartSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AppError(f'{path}: "start" is an object')
    for key in raw:
        if key not in _START_KEYS:
            raise AppError(f'{path}: unknown start field "{key}"')
    library = raw.get("library")
    process = raw.get("process")
    if not isinstance(library, str) or not _BARE_RE.match(library):
        raise AppError(f'{path}: "library" is a native library file name')
    if not isinstance(process, str) or not _BARE_RE.match(process):
        raise AppError(f'{path}: "process" is a process name')
    return StartSpec(library, process)


def asset_for(spec: AppSpec, abilist: Sequence[str]) -> str:
    """Pick the release asset for an abilist, the device's supported CPU types.

    The list is best first, so the first supported ABI wins. Anything unknown,
    including an empty list, falls back to the universal build.
    """
    for abi in abilist:
        if abi in spec.assets:
            return spec.assets[abi]
    return spec.assets[UNIVERSAL]


def latest_release(spec: AppSpec, *, open_url: Callable[..., Any] = urlopen) -> Release:
    """Fetch the current release metadata. A non-200 response is an error."""
    try:
        with open_url(spec.release, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise AppError(f"{spec.name} release: {_status(response.status)}")
            body = response.read()
    except HTTPError as error:
        raise AppError(f"{spec.name} release: {_status(error.code)}") from error
    except http.client.HTTPException as error:
        raise AppError(f"fetching {spec.name} release: {error}") from error
    except OSError as error:
        raise AppError(f"fetching {spec.name} release: {error}") from error
    try:
        payload: Any = json.loads(body)
        release = Release(
            tag=str(payload["tag_name"]),
            assets=tuple(
                Asset(
                    name=str(item["name"]),
                    url=str(item["browser_download_url"]),
                    size=int(item.get("size", 0)),
                    digest=str(item.get("digest") or ""),
                )
                for item in payload["assets"]
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AppError(f"parsing release metadata: {error}") from error
    return release


def expected_hash(
    spec: AppSpec,
    release: Release,
    apk: Asset,
    *,
    open_url: Callable[..., Any] = urlopen,
) -> str:
    """The digest `apk` must match, from the release or the publisher's file.

    `spec.checksum` decides the source: the digest GitHub records for the
    uploaded asset, or the publisher's `.sha256` asset.
    """
    if spec.checksum == "digest":
        return _digest(apk)
    return _published_hash(release.find_checksum(apk), open_url=open_url)


def _digest(apk: Asset) -> str:
    prefix, _, value = apk.digest.partition(":")
    digest = value.lower()
    if (
        prefix.lower() != "sha256"
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise AppError(f"{apk.name} has no sha256 digest in the release metadata")
    return digest


def _published_hash(asset: Asset, *, open_url: Callable[..., Any] = urlopen) -> str:
    """Download and parse a `.sha256` asset into a lowercase hex digest.

    The file holds the digest alone, or the digest followed by a filename.
    Anything else, including an empty body, is an error.
    """
    try:
        with open_url(asset.url, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise AppError(f"{asset.name}: {_status(response.status)}")
            body = response.read()
    except HTTPError as error:
        raise AppError(f"{asset.name}: {_status(error.code)}") from error
    except http.client.HTTPException as error:
        raise AppError(f"fetching {asset.name}: {error}") from error
    except OSError as error:
        raise AppError(f"fetching {asset.name}: {error}") from error

    fields = body.split()
    if not fields:
        raise AppError(f"{asset.name} is empty")
    digest = fields[0].decode("ascii", "replace").lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise AppError(f"{asset.name} does not hold a sha256 digest: {digest!r}")
    return digest


def download(
    asset: Asset,
    want: str,
    directory: Path | str,
    *,
    open_url: Callable[..., Any] = urlopen,
) -> Path:
    """Fetch an asset into `directory` and verify it against `want`.

    The path is returned only after the bytes match the publisher's digest. The
    file is removed on a short write, an HTTP error, or a mismatch, so a
    corrupted APK cannot be installed later.
    """
    target = Path(directory)
    try:
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
    except OSError as error:
        raise AppError(f"creating download directory: {error}") from error
    path = target / asset.name

    try:
        with open_url(asset.url, timeout=TIMEOUT) as response:
            if response.status != 200:
                # Nothing has been written yet, so a previously verified copy
                # stays in place, the way the old tool left it.
                raise AppError(f"{asset.name}: {_status(response.status)}")
            got = _copy_verified(response, path, asset.name)
    except HTTPError as error:
        raise AppError(f"{asset.name}: {_status(error.code)}") from error
    except http.client.HTTPException as error:
        raise AppError(f"fetching {asset.name}: {error}") from error
    except OSError as error:
        raise AppError(f"fetching {asset.name}: {error}") from error

    if got != want:
        _remove(path)
        raise AppError(
            f"{asset.name} checksum mismatch\n"
            f"  publisher says {want}\n"
            f"  downloaded     {got}\n"
            "refusing to install; retry, and if it persists, investigate the release"
        )
    return path


def _copy_verified(response: Any, path: Path, name: str) -> str:
    """Stream a response to `path`, returning the SHA-256 of what was written."""
    digest = hashlib.sha256()
    try:
        with path.open("wb") as file:
            while chunk := response.read(CHUNK):
                digest.update(chunk)
                if file.write(chunk) != len(chunk):
                    raise AppError(f"downloading {name}: short write to {path}")
    except OSError as error:
        _remove(path)
        raise AppError(f"downloading {name}: {error}") from error
    except http.client.HTTPException as error:
        _remove(path)
        raise AppError(f"downloading {name}: {error}") from error
    except AppError:
        _remove(path)
        raise
    return digest.hexdigest()


def _remove(path: Path) -> None:
    """Best effort: the mismatch is the error worth reporting."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _status(code: int) -> str:
    """Render a status code with its reason, for example `404 Not Found`."""
    reason = http.client.responses.get(code, "")
    return f"{code} {reason}".strip()


def describe(release: Release, asset: Asset, digest: str) -> str:
    """Render the plan for a human before anything is downloaded."""
    return f"{release.tag} {asset.name}\n  {asset.url}\n  sha256 {digest}"
