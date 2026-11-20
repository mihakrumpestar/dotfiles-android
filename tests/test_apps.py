"""App install specs, release lookup, checksum parsing, and verified download.

No network: every request goes through a fake `open_url` that plays a GitHub
release with an APK and the publisher's `.sha256` file next to it.
"""

from __future__ import annotations

import hashlib
import http.client
import json
from dataclasses import replace
from pathlib import Path
from typing import Self

import pytest
from support import REPO, VALID, app_file

from androidctl import apps
from androidctl.apps import (
    AppError,
    AppSpec,
    Asset,
    Release,
    StartSpec,
    asset_for,
    describe,
    download,
    expected_hash,
    latest_release,
    load,
)
from uiu.spec import parse as parse_app

ORIGIN = "https://example.invalid"
APK_NAME = "app-arm64-v8a-release.apk"
DIGEST = "ab" * 32

SPEC = AppSpec(
    name="demo",
    package="com.example.demo",
    release=f"{ORIGIN}/releases/latest",
    assets={
        "arm64-v8a": "app-arm64-v8a-release.apk",
        "armeabi-v7a": "app-armeabi-v7a-release.apk",
        "x86_64": "app-x86_64-release.apk",
        "universal": "app-release.apk",
    },
    checksum="asset",
)

GOOD = {
    "release": "https://example.invalid/releases/latest",
    "checksum": "asset",
    "assets": {"universal": "app.apk"},
}
NO_CHECKSUM = {key: value for key, value in GOOD.items() if key != "checksum"}


class FakeResponse:
    """One HTTP reply: a status and a body that can be read in chunks."""

    def __init__(self, body: bytes = b"", status: int = 200):
        self.status = status
        self.body = body
        self.headers: dict[str, str] = {}
        self._offset = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self._offset
        chunk = self.body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeWeb:
    """A stand-in for `open_url` that serves a fixed route table."""

    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.urls: list[str] = []
        self.timeouts: list[float | None] = []

    def __call__(self, url: str, timeout: float | None = None) -> FakeResponse:
        self.urls.append(url)
        self.timeouts.append(timeout)
        return self.routes.get(url, FakeResponse(status=404))


class BrokenBody(FakeResponse):
    """A reply that sends one chunk and then drops the connection."""

    def __init__(self, body: bytes):
        super().__init__(body=body)
        self.sent = False

    def read(self, size: int = -1) -> bytes:
        if self.sent:
            raise OSError("connection reset")
        self.sent = True
        return super().read(size)


def fixture(apk_body: bytes = b"", checksum_body: bytes = b"") -> FakeWeb:
    """Serve a release, an APK, and its checksum."""
    release = {
        "tag_name": "v9.9.9",
        "assets": [
            {
                "name": APK_NAME,
                "browser_download_url": f"{ORIGIN}/apk",
                "size": len(apk_body),
                "digest": "sha256:" + DIGEST,
            },
            {
                "name": APK_NAME + ".sha256",
                "browser_download_url": f"{ORIGIN}/sha",
                "size": len(checksum_body),
            },
        ],
    }
    return FakeWeb(
        {
            f"{ORIGIN}/releases/latest": FakeResponse(json.dumps(release).encode()),
            f"{ORIGIN}/apk": FakeResponse(apk_body),
            f"{ORIGIN}/sha": FakeResponse(checksum_body),
        }
    )


def resolve(web: FakeWeb) -> tuple[Release, Asset, Asset]:
    """The release, its APK, and the publisher's checksum asset."""
    release = latest_release(SPEC, open_url=web)
    apk = release.find(APK_NAME)
    return release, apk, release.find_checksum(apk)


def spec_body(**overrides: object) -> dict:
    """A valid install spec, with fields replaced."""
    return {**GOOD, **overrides}


def make_app(
    root: Path,
    name: str = "demo",
    *,
    package: str = "com.example.demo",
    install: object = None,
    steps: bool = True,
) -> Path:
    """One app directory: `app.py` carrying the install facts and the walk."""
    body: dict = {"package": package, "install": install}
    if steps:
        body.update(
            {
                "artifact": "demo.json",
                "backup": VALID["backup"],
                "restore": VALID["restore"],
            }
        )
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "app.py").write_text(app_file(body))
    return directory


def test_the_committed_specs_track_the_latest_release() -> None:
    for package in ("dev.imranr.obtainium", "moe.shizuku.privileged.api"):
        assert load(package).release.endswith("/releases/latest")


def test_the_committed_obtainium_install_spec_loads() -> None:
    spec = load("dev.imranr.obtainium")
    assert spec.name == "obtainium"
    assert spec.package == "dev.imranr.obtainium"
    assert "ImranR98/Obtainium" in spec.release
    assert spec.checksum == "asset"
    assert spec.assets["universal"] == "app-release.apk"


def test_the_committed_shizuku_install_spec_loads() -> None:
    spec = load("moe.shizuku.privileged.api")
    assert spec.name == "shizuku"
    assert spec.package == "moe.shizuku.privileged.api"
    assert spec.checksum == "digest"
    assert spec.assets["universal"] == "shizuku-next.apk"
    assert spec.start == StartSpec("libshizuku.so", "shizuku_server")
    assert parse_app(REPO / "policy/data/shizuku/app.py").recipe is None


def test_a_second_app_is_added_without_touching_shared_code(tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        make_app(
            tmp_path,
            name,
            package=f"com.example.{name}",
            install={
                "checksum": "asset",
                "release": "https://example.invalid/releases/latest",
                "assets": {
                    "arm64-v8a": f"{name}-arm64.apk",
                    "universal": f"{name}.apk",
                },
                "hint": f"open {name}",
            },
        )

    alpha = load("com.example.alpha", tmp_path)
    beta = load("com.example.beta", tmp_path)

    assert alpha.name == "alpha"
    assert alpha.package == "com.example.alpha"
    assert alpha.release == "https://example.invalid/releases/latest"
    assert asset_for(alpha, ["arm64-v8a"]) == "alpha-arm64.apk"
    assert alpha.hint == "open alpha"

    assert beta.name == "beta"
    assert beta.package == "com.example.beta"
    assert beta.release == "https://example.invalid/releases/latest"
    assert asset_for(beta, ["arm64-v8a"]) == "beta-arm64.apk"
    assert beta.hint == "open beta"


def test_an_install_only_app_needs_no_recipe(tmp_path: Path) -> None:
    make_app(tmp_path, install=spec_body(), steps=False)
    spec = load("com.example.demo", tmp_path)
    assert spec.name == "demo"
    assert spec.package == "com.example.demo"


def test_an_app_file_in_the_private_tree_is_found(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    make_app(secrets, "hidden", package="com.example.hidden", install=spec_body(), steps=False)

    spec = load("com.example.hidden", tmp_path / "apps", secrets)

    assert spec.name == "hidden"
    assert spec.package == "com.example.hidden"


def test_an_app_without_install_facts_is_not_offered(tmp_path: Path) -> None:
    make_app(tmp_path, package="com.example.demo")
    with pytest.raises(AppError, match="no app file declares an install"):
        load("com.example.demo", tmp_path)


@pytest.mark.parametrize("package", ["nope", "com.example/app", "com.example."])
def test_load_refuses_an_app_file_with_a_bad_package(tmp_path: Path, package: str) -> None:
    make_app(tmp_path, package=package, install=spec_body())
    with pytest.raises(AppError, match="Android package id"):
        load("com.example.demo", tmp_path)


@pytest.mark.parametrize("package", ["../x", "nope"])
def test_load_rejects_a_bad_package(tmp_path: Path, package: str) -> None:
    make_app(tmp_path, install=spec_body())
    with pytest.raises(AppError, match="not a package name"):
        load(package, tmp_path)


def test_load_rejects_a_folder_that_is_not_an_app_name(tmp_path: Path) -> None:
    make_app(tmp_path, name="Bad Name", install=spec_body())
    with pytest.raises(AppError, match="not a normalized app name"):
        load("com.example.demo", tmp_path)


@pytest.mark.parametrize(
    ("install", "match"),
    [
        ({}, '"release" is an https URL'),
        (NO_CHECKSUM, '"checksum" is'),
        (spec_body(checksum="sha1"), '"checksum" is'),
        (spec_body(start="x"), '"start" is an object'),
        (spec_body(start={"library": "libx.so", "process": "x", "extra": "y"}), "unknown start"),
        (spec_body(start={"library": "../libx.so", "process": "x"}), '"library"'),
        (spec_body(start={"library": "libx.so", "process": "x y"}), '"process"'),
        ([], "install is an object"),
        (spec_body(nope=1), 'unknown field "nope"'),
        (spec_body(release="http://x/releases/latest"), "https URL"),
        (
            spec_body(release="https://x/releases/tag/v1.2.3"),
            "must end in /releases/latest",
        ),
        (spec_body(assets={}), "maps an ABI"),
        (spec_body(assets={"arm64-v8a": "app.apk"}), "universal"),
        (spec_body(assets={"universal": 1}), "maps an ABI"),
        (spec_body(hint=1), '"hint"'),
    ],
)
def test_load_rejects_an_invalid_install_spec(tmp_path: Path, install: object, match: str) -> None:
    make_app(tmp_path, install=install)
    with pytest.raises(AppError, match=match):
        load("com.example.demo", tmp_path)


@pytest.mark.parametrize(
    ("abilist", "want"),
    [
        (["arm64-v8a"], "app-arm64-v8a-release.apk"),
        (["arm64-v8a", "armeabi-v7a"], "app-arm64-v8a-release.apk"),
        (["armeabi-v7a"], "app-armeabi-v7a-release.apk"),
        (["x86_64"], "app-x86_64-release.apk"),
        (["mips"], "app-release.apk"),
        ([], "app-release.apk"),
    ],
)
def test_asset_for_picks_the_device_abi(abilist: list[str], want: str) -> None:
    assert asset_for(SPEC, abilist) == want


def test_latest_release_and_find() -> None:
    web = fixture()
    release = latest_release(SPEC, open_url=web)
    assert release.tag == "v9.9.9"
    apk = release.find(APK_NAME)
    assert apk == Asset(name=APK_NAME, url=f"{ORIGIN}/apk", size=0, digest="sha256:" + DIGEST)
    assert release.find_checksum(apk).name == APK_NAME + ".sha256"
    assert web.timeouts == [120]

    with pytest.raises(AppError) as caught:
        release.find("nope.apk")
    assert str(caught.value) == 'release v9.9.9 has no asset "nope.apk"'


def test_find_checksum_refuses_a_release_without_one() -> None:
    release = Release(
        tag="v1",
        assets=(Asset(name="app.apk", url="https://example.invalid/app.apk"),),
    )
    with pytest.raises(AppError) as caught:
        release.find_checksum(release.find("app.apk"))
    assert str(caught.value) == 'release v1 has no asset "app.apk.sha256"'


def test_latest_release_rejects_a_non_200() -> None:
    web = FakeWeb({f"{ORIGIN}/releases/latest": FakeResponse(status=404)})
    with pytest.raises(AppError) as caught:
        latest_release(SPEC, open_url=web)
    assert str(caught.value) == "demo release: 404 Not Found"


@pytest.mark.parametrize(
    "body",
    [
        f"{DIGEST}\n",
        f"{DIGEST}  {APK_NAME}\n",
        f"{DIGEST.upper()}  {APK_NAME}\n",
    ],
)
def test_expected_hash_parses_the_digest(body: str) -> None:
    web = fixture(checksum_body=body.encode())
    release, apk, _ = resolve(web)
    assert expected_hash(SPEC, release, apk, open_url=web) == DIGEST


@pytest.mark.parametrize(
    "body",
    [b"\n", b"not-a-digest\n", b"ab" * 31 + b"\n", b"zz" * 32 + b"\n"],
)
def test_expected_hash_rejects_non_digest(body: bytes) -> None:
    web = fixture(checksum_body=body)
    release, apk, _ = resolve(web)
    with pytest.raises(AppError):
        expected_hash(SPEC, release, apk, open_url=web)


def test_expected_hash_rejects_an_empty_body() -> None:
    web = fixture()
    release, apk, _ = resolve(web)
    with pytest.raises(AppError) as caught:
        expected_hash(SPEC, release, apk, open_url=web)
    assert str(caught.value) == f"{APK_NAME}.sha256 is empty"


@pytest.mark.parametrize(
    "digest",
    ["sha256:" + DIGEST, "SHA256:" + DIGEST.upper()],
)
def test_expected_hash_uses_the_release_digest(digest: str) -> None:
    apk = Asset(name=APK_NAME, url=f"{ORIGIN}/apk", digest=digest)
    spec = replace(SPEC, checksum="digest")
    assert expected_hash(spec, Release("v1"), apk) == DIGEST


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "sha256:",
        "md5:" + DIGEST,
        "sha256:" + "ab" * 31,
        "sha256:" + "zz" * 32,
    ],
)
def test_expected_hash_refuses_a_bad_release_digest(digest: str) -> None:
    apk = Asset(name=APK_NAME, url=f"{ORIGIN}/apk", digest=digest)
    spec = replace(SPEC, checksum="digest")
    with pytest.raises(AppError, match="has no sha256 digest"):
        expected_hash(spec, Release("v1"), apk)


def test_download_verifies_the_checksum(tmp_path: Path) -> None:
    body = b"an apk, honestly"
    web = fixture(
        apk_body=body,
        checksum_body=(hashlib.sha256(body).hexdigest() + "\n").encode(),
    )
    release, apk, _ = resolve(web)
    want = expected_hash(SPEC, release, apk, open_url=web)

    directory = tmp_path / "demo"
    path = download(apk, want, directory, open_url=web)
    assert path == directory / APK_NAME
    assert path.read_bytes() == body


def test_download_rejects_a_mismatch_and_removes_the_file(tmp_path: Path) -> None:
    # Installing whatever a release served is an unsigned remote code execution
    # path on the user's phone, so a mismatch must leave nothing behind.
    body = b"the real apk"
    web = fixture(apk_body=body, checksum_body=("0" * 64 + "\n").encode())
    release, apk, _ = resolve(web)
    want = expected_hash(SPEC, release, apk, open_url=web)

    directory = tmp_path / "demo"
    with pytest.raises(AppError) as caught:
        download(apk, want, directory, open_url=web)

    message = str(caught.value)
    assert "checksum mismatch" in message
    assert f"publisher says {want}" in message
    assert f"downloaded     {hashlib.sha256(body).hexdigest()}" in message
    assert "refusing to install" in message
    assert not (directory / APK_NAME).exists()


def test_download_rejects_an_http_error(tmp_path: Path) -> None:
    web = fixture()
    missing = Asset(name="x.apk", url=f"{ORIGIN}/missing")
    directory = tmp_path / "demo"

    with pytest.raises(AppError) as caught:
        download(missing, "0" * 64, directory, open_url=web)
    assert str(caught.value) == "x.apk: 404 Not Found"
    assert not (directory / "x.apk").exists()


def test_download_removes_a_file_when_the_body_fails(tmp_path: Path) -> None:
    web = fixture()
    web.routes[f"{ORIGIN}/apk"] = BrokenBody(b"partial")
    _, apk, _ = resolve(web)

    directory = tmp_path / "demo"
    with pytest.raises(AppError, match="connection reset"):
        download(apk, "0" * 64, directory, open_url=web)
    assert not (directory / APK_NAME).exists()


def test_describe_shows_the_checksum_before_installing() -> None:
    release = Release(tag="v1.2.3")
    asset = Asset(name="app.apk", url="https://example.invalid/app.apk")
    text = describe(release, asset, "a" * 64)

    for want in ("v1.2.3", "app.apk", "example.invalid", "sha256", "a" * 64):
        assert want in text
    assert text == ("v1.2.3 app.apk\n  https://example.invalid/app.apk\n  sha256 " + "a" * 64)


def test_an_http_error_is_reported_like_a_go_status_line():
    from email.message import Message
    from urllib.error import HTTPError

    def refused(url, *, timeout=None):
        raise HTTPError(url, 403, "Forbidden", Message(), None)

    with pytest.raises(apps.AppError, match="demo release: 403 Forbidden"):
        apps.latest_release(SPEC, open_url=refused)


def test_a_checksum_http_error_keeps_the_asset_name():
    from email.message import Message
    from urllib.error import HTTPError

    def refused(url, *, timeout=None):
        raise HTTPError(url, 404, "Not Found", Message(), None)

    apk = apps.Asset("app.apk", "https://x/app.apk")
    checksum = apps.Asset("app.apk.sha256", "https://x/app.apk.sha256")
    with pytest.raises(apps.AppError, match="app.apk.sha256: 404 Not Found"):
        apps.expected_hash(SPEC, apps.Release("v1", (checksum,)), apk, open_url=refused)


def test_a_failed_download_leaves_a_previously_verified_file_in_place(tmp_path):
    web = fixture()
    missing = Asset(name="x.apk", url=f"{ORIGIN}/missing")
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "x.apk").write_bytes(b"previous")

    with pytest.raises(AppError):
        download(missing, "0" * 64, directory, open_url=web)

    assert (directory / "x.apk").read_bytes() == b"previous"


class TruncatedResponse:
    """A response that promises more body than it sends."""

    status = 200

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, *args: object) -> bytes:
        raise http.client.IncompleteRead(b"x", 999)


def truncated(url, *, timeout=None):
    return TruncatedResponse()


def test_a_truncated_release_body_is_a_clean_error():
    with pytest.raises(apps.AppError, match="fetching demo release"):
        apps.latest_release(SPEC, open_url=truncated)


def test_a_truncated_checksum_body_is_a_clean_error():
    apk = apps.Asset("app.apk", "https://x/app.apk")
    checksum = apps.Asset("app.apk.sha256", "https://x/app.apk.sha256")
    with pytest.raises(apps.AppError, match="fetching app.apk.sha256"):
        apps.expected_hash(SPEC, apps.Release("v1", (checksum,)), apk, open_url=truncated)
