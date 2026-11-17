"""Ratings: the upstream list, its ETag cache, and the offline fallback."""

from __future__ import annotations

import pytest

from androidctl.ratings import (
    UAD_BRANCH,
    UAD_URL,
    Rating,
    Ratings,
    RatingsError,
    load_ratings,
)

LIST = (
    b'{"com.a":{"removal":"Recommended","list":"Oem","description":"An app.",'
    b'"dependencies":["com.b"],"neededBy":["com.c"]}}'
)


class Fetch:
    """A stand-in for the network, recording the URL and ETag it was sent."""

    def __init__(
        self,
        status: int,
        body: bytes | None = None,
        etag: str = "",
        error: Exception | None = None,
    ):
        self.status = status
        self.body = body
        self.result_etag = etag
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, etag: str) -> tuple[int, bytes | None, str]:
        self.calls.append((url, etag))
        if self.error is not None:
            raise self.error
        return self.status, self.body, self.result_etag


def test_revalidates_with_the_stored_etag(tmp_path):
    (tmp_path / "uad_lists.json").write_bytes(b'{"com.a":{"removal":"Recommended"}}')
    (tmp_path / "uad_lists.etag").write_text('"abc123"\n')
    fetch = Fetch(304)

    ratings = load_ratings(tmp_path, "https://example.test/uad_lists.json", fetch)

    assert fetch.calls == [("https://example.test/uad_lists.json", '"abc123"')]
    assert ratings.source == "revalidated"
    assert ratings.detail == ""
    assert ratings.by_package["com.a"].removal == "Recommended"


def test_fetches_and_caches_the_bytes_and_etag(tmp_path):
    fetch = Fetch(200, LIST, '"fresh"')

    ratings = load_ratings(tmp_path, "https://example.test/list", fetch)

    assert ratings.source == "fetched"
    assert ratings.detail == ""
    assert ratings.by_package["com.a"].dependencies == ("com.b",)
    assert ratings.by_package["com.a"].needed_by == ("com.c",)
    assert (tmp_path / "uad_lists.json").read_bytes() == LIST
    assert (tmp_path / "uad_lists.etag").read_text() == '"fresh"\n'


def test_a_response_without_an_etag_drops_the_stale_one(tmp_path):
    (tmp_path / "uad_lists.etag").write_text('"old"\n')

    ratings = load_ratings(tmp_path, "u", Fetch(200, LIST))

    assert ratings.source == "fetched"
    assert not (tmp_path / "uad_lists.etag").exists()


def test_offline_uses_the_cache(tmp_path):
    (tmp_path / "uad_lists.json").write_bytes(b'{"com.a":{"removal":"Oem"}}')
    fetch = Fetch(0, error=OSError("network unreachable"))

    ratings = load_ratings(tmp_path, "u", fetch)

    assert ratings.source == "cache"
    assert "network unreachable" in ratings.detail
    assert ratings.by_package["com.a"].removal == "Oem"


def test_offline_without_a_cache_fails(tmp_path):
    with pytest.raises(RatingsError):
        load_ratings(tmp_path, "u", Fetch(0, error=OSError("offline")))


def test_not_modified_without_a_cache_fails(tmp_path):
    with pytest.raises(RatingsError, match="304"):
        load_ratings(tmp_path, "u", Fetch(304))


def test_an_error_status_uses_the_cache(tmp_path):
    (tmp_path / "uad_lists.json").write_bytes(b'{"com.a":{"removal":"Oem"}}')

    ratings = load_ratings(tmp_path, "u", Fetch(500))

    assert ratings.source == "cache"
    assert ratings.detail == "500 Internal Server Error"
    assert ratings.by_package["com.a"].removal == "Oem"


def test_an_error_status_without_a_cache_fails(tmp_path):
    with pytest.raises(RatingsError, match="500"):
        load_ratings(tmp_path, "u", Fetch(500))


def test_rating_defaults_are_empty():
    assert Rating() == Rating(
        removal="",
        list="",
        description="",
        dependencies=(),
        needed_by=(),
    )


def test_header_shows_the_source_and_detail():
    assert str(Ratings({}, "fetched")) == "fetched"
    assert str(Ratings({}, "cache", "HTTP 500")) == "cache (HTTP 500)"


def test_url_tracks_a_branch_not_a_commit():
    # A pinned commit silently ages, and ratings are an input to a human
    # decision, so they must be current.
    assert f"/{UAD_BRANCH}/" in UAD_URL
    assert len(UAD_BRANCH) != 40
    assert UAD_URL.endswith("/resources/assets/uad_lists.json")
