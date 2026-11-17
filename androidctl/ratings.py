"""Universal Android Debloater ratings, cached under the state directory.

The upstream list is a community opinion, not authority: on a stock Samsung the
package marked Recommended is often the dangerous one. Ratings are an input to
a human decision, not a reproducible artifact, so they are read from a branch
and refreshed whenever the network allows.

An ETag is a version tag the server compares, so a warm cache costs one 304
response instead of the whole file. The response bytes are cached unchanged,
because re-encoding them would churn the file for no gain.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A branch, never a commit: a pinned commit silently ages while the device keeps
# updating.
UAD_BRANCH = "main"

UAD_URL = (
    "https://raw.githubusercontent.com/Universal-Debloater-Alliance/"
    "universal-android-debloater-next-generation/" + UAD_BRANCH + "/resources/assets/uad_lists.json"
)

Fetcher = Callable[[str, str], tuple[int, bytes | None, str]]


class RatingsError(RuntimeError):
    """The list could not be fetched, cached, or parsed."""


@dataclass
class Rating:
    """One entry from the upstream list."""

    removal: str = ""
    list: str = ""
    description: str = ""
    dependencies: tuple[str, ...] = ()
    needed_by: tuple[str, ...] = ()


@dataclass
class Ratings:
    """The upstream list plus where it came from."""

    by_package: dict[str, Rating]
    source: str
    detail: str = ""

    def __str__(self) -> str:
        if self.detail:
            return f"{self.source} ({self.detail})"
        return self.source


def load_ratings(
    state_dir: Path | str,
    url: str = UAD_URL,
    fetch: Fetcher | None = None,
) -> Ratings:
    """Fetch the list, revalidating the cache with its ETag.

    Offline with a cache is not an error: describing the device is still useful.
    Offline without one is.
    """
    state = Path(state_dir)
    cache_path = state / "uad_lists.json"
    etag_path = state / "uad_lists.etag"

    cached = _read_bytes(cache_path)
    stored_etag = _read_bytes(etag_path).decode("utf-8", "replace").rstrip("\r\n")
    request_etag = stored_etag if cached and stored_etag else ""

    fetcher = fetch or _fetch
    try:
        status, body, etag = fetcher(url, request_etag)
    except (RatingsError, OSError) as error:
        if cached:
            return _decode(cached, "cache", str(error))
        raise RatingsError(f"fetching upstream ratings: {error}") from error

    if status == 304:
        if not cached:
            raise RatingsError("upstream returned 304 but no cache exists")
        return _decode(cached, "revalidated", "")

    if status == 200:
        fresh = body or b""
        try:
            state.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(fresh)
        except OSError as error:
            raise RatingsError(f"caching ratings: {error}") from error
        try:
            if etag:
                etag_path.write_text(etag + "\n", encoding="utf-8")
            else:
                etag_path.unlink(missing_ok=True)
        except OSError:
            pass  # A missing ETag costs one full fetch next time, nothing more.
        return _decode(fresh, "fetched", "")

    reason = f"{status} {http.client.responses.get(status, '')}".strip()
    if cached:
        return _decode(cached, "cache", reason)
    raise RatingsError(f"upstream ratings: {reason}")


def _fetch(url: str, etag: str) -> tuple[int, bytes | None, str]:
    """The real fetch: one GET, 60 seconds, reused only through the cache."""
    request = urllib.request.Request(url, method="GET")
    if etag:
        request.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read() if response.status == 200 else None
            return response.status, body, response.headers.get("ETag", "")
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return 304, None, error.headers.get("ETag", "")
        return error.code, None, ""


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _decode(body: bytes, source: str, detail: str) -> Ratings:
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise RatingsError(f"parsing upstream ratings: {error}") from error
    if not isinstance(payload, dict):
        raise RatingsError("parsing upstream ratings: expected an object")
    by_package: dict[str, Rating] = {}
    for name, entry in payload.items():
        if not isinstance(entry, dict):
            raise RatingsError(f"parsing upstream ratings: {name} is not an object")
        by_package[str(name)] = _rating(entry)
    return Ratings(by_package=by_package, source=source, detail=detail)


def _rating(entry: dict[str, Any]) -> Rating:
    return Rating(
        removal=str(entry.get("removal") or ""),
        list=str(entry.get("list") or ""),
        description=str(entry.get("description") or ""),
        dependencies=_strings(entry.get("dependencies")),
        needed_by=_strings(entry.get("neededBy")),
    )


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)
