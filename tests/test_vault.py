"""The vault: envelopes inside the private tree, plaintext everywhere else."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
from support import ENVELOPE, VALID, recipe_from

from uiu import artifacts, commands, vault
from uiu.vault import Vault, VaultError, encrypted

KEY = "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"

# A throwaway age keypair generated with `age-keygen` for this test suite only.
# It is never used for real data, and pasting the private half here is what lets
# the real-sops test run without a keyring.
SOPS_RECIPIENT = "age1hkygek0uzhy58kvrkh50mwpve5lnrdr4eye9y7f24fuy03943gysutg6m4"
SOPS_IDENTITY = "AGE-SECRET-KEY-1GDF3XJ9AF85V7XY926DQ6XNY3NLAUTHKGFZ25FNUTX4EV2CQTWYS4UXZ5Q"


def refuse(*_args: object) -> NoReturn:
    raise AssertionError("the identity was resolved before a covered file needed it")


def recorder(result: bytes):
    """A `run` seam that records its calls and returns `result`."""
    calls: list[tuple[list[str], bytes]] = []

    def run(argv, body):
        calls.append((list(argv), body))
        return result

    return run, calls


def writer(plaintext: bytes, envelope: bytes = ENVELOPE):
    """A `run` seam that encrypts to `envelope` and opens that envelope."""
    calls: list[tuple[list[str], bytes]] = []

    def run(argv, body):
        calls.append((list(argv), body))
        return envelope if "encrypt" in argv else plaintext

    return run, calls


def test_an_envelope_is_recognised_by_its_shape():
    assert encrypted(ENVELOPE)


@pytest.mark.parametrize(
    "body",
    [
        b'{"keep": 1}',
        b'["a", "b"]',
        b"[]",
        b"PK\x03\x04\x14\x00\x00\x00",
        b"\x00\xff\x00",
        b'{"data": "not ciphertext", "sops": {"mac": "ENC[x]"}}',
        b'{"data": "ENC[x]", "sops": {"mac": "not ciphertext"}}',
        b'{"data": "ENC[x]"}',
    ],
)
def test_anything_else_is_not_an_envelope(body):
    assert not encrypted(body)


def test_covers_the_private_root_and_nothing_beside_it(tmp_path):
    root = tmp_path / "secrets"
    subject = Vault(root, identity=refuse, run=refuse)

    assert subject.covers(root / "demo" / "roundsync.zip")
    assert not subject.covers(root.with_name("secrets-copy") / "demo.json")
    assert not subject.covers(tmp_path / "policy" / "data" / "demo" / "app.py")


def test_read_passes_an_envelope_outside_the_root_through(tmp_path):
    subject = Vault(tmp_path / "secrets", identity=refuse, run=refuse)
    path = tmp_path / "policy" / "demo.json"
    path.parent.mkdir()
    path.write_bytes(ENVELOPE)

    assert subject.read(path) == ENVELOPE


def test_read_passes_a_plaintext_file_inside_the_root_through(tmp_path):
    root = tmp_path / "secrets"
    root.mkdir()
    subject = Vault(root, identity=refuse, run=refuse)
    path = root / "demo.json"
    path.write_bytes(b'{"keep": 1}')

    assert subject.read(path) == b'{"keep": 1}'


def test_read_decrypts_a_covered_envelope(tmp_path):
    root = tmp_path / "secrets"
    path = root / "demo" / "demo.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(ENVELOPE)
    run, calls = recorder(b'{"keep": 1}')
    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY, run=run)

    assert subject.read(path) == b'{"keep": 1}'
    assert calls == [
        (
            [
                "decrypt",
                "--input-type",
                "binary",
                "--output-type",
                "binary",
                str(path),
            ],
            b"",
        )
    ]


def test_write_returns_an_envelope_for_a_covered_path(tmp_path):
    root = tmp_path / "secrets"
    path = root / "demo" / "demo.json"
    run, calls = writer(b'{"keep": 1}')
    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY, run=run)

    assert subject.write(path, b'{"keep": 1}') == ENVELOPE
    assert calls == [
        (
            [
                "--config",
                str(root / ".sops.yaml"),
                "encrypt",
                "--input-type",
                "binary",
                "--output-type",
                "binary",
                "--filename-override",
                str(path),
                "/dev/stdin",
            ],
            b'{"keep": 1}',
        ),
        (
            [
                "decrypt",
                "--input-type",
                "binary",
                "--output-type",
                "binary",
                "/dev/stdin",
            ],
            ENVELOPE,
        ),
    ]


def test_write_passes_bytes_through_outside_the_root(tmp_path):
    subject = Vault(tmp_path / "secrets", identity=refuse, run=refuse)

    assert subject.write(tmp_path / "policy" / "demo.json", b'{"keep": 1}') == b'{"keep": 1}'


def test_write_refuses_plaintext_from_a_run_that_did_not_encrypt(tmp_path):
    root = tmp_path / "secrets"
    run, _ = recorder(b'{"keep": 1}')
    subject = Vault(root, identity=lambda: KEY, run=run)

    with pytest.raises(VaultError, match="did not produce an envelope"):
        subject.write(root / "demo.json", b'{"keep": 1}')


def test_write_refuses_an_envelope_that_does_not_open_with_the_key(tmp_path):
    root = tmp_path / "secrets"
    path = root / "demo.json"

    def run(argv, body):
        if "encrypt" in argv:
            return ENVELOPE
        raise VaultError("sops exited 128: Recovery failed because no master key was able")

    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY, run=run)

    with pytest.raises(VaultError, match="does not open with SOPS_AGE_KEY") as caught:
        subject.write(path, b'{"keep": 1}')
    assert str(path) in str(caught.value)


def test_write_refuses_an_envelope_that_opens_to_other_bytes(tmp_path):
    root = tmp_path / "secrets"
    path = root / "demo.json"

    def run(argv, body):
        return ENVELOPE if "encrypt" in argv else b'{"keep": 2}'

    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY, run=run)

    with pytest.raises(VaultError, match="does not open with SOPS_AGE_KEY") as caught:
        subject.write(path, b'{"keep": 1}')
    assert str(path) in str(caught.value)


def test_a_broken_identity_names_the_secretspec_command(tmp_path):
    root = tmp_path / "secrets"
    root.mkdir()
    path = root / "demo.json"
    path.write_bytes(ENVELOPE)
    manifest = tmp_path / "secretspec.toml"

    def identity() -> str:
        raise RuntimeError("the keyring is locked")

    subject = Vault(root, manifest=manifest, identity=identity, run=refuse)

    with pytest.raises(VaultError, match="secretspec set SOPS_AGE_KEY") as caught:
        subject.read(path)
    assert str(manifest) in str(caught.value)
    assert str(path) in str(caught.value)
    with pytest.raises(VaultError, match="secretspec set SOPS_AGE_KEY"):
        subject.write(root / "other.json", b"{}")


def test_a_failing_sops_names_the_file_and_does_not_blame_the_key(tmp_path):
    root = tmp_path / "secrets"
    root.mkdir()
    path = root / "demo.json"
    path.write_bytes(ENVELOPE)

    def run(argv, body):
        raise VaultError("sops exited 128: no identity matched any of the recipients")

    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY, run=run)

    with pytest.raises(VaultError, match="no identity matched") as caught:
        subject.read(path)
    message = str(caught.value)
    assert str(path) in message
    assert "secretspec set SOPS_AGE_KEY" not in message
    with pytest.raises(VaultError, match="no identity matched") as caught:
        subject.write(root / "other.json", b"{}")
    assert str(root / "other.json") in str(caught.value)


def test_a_missing_sops_binary_does_not_blame_the_key(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    root.mkdir()
    path = root / "demo.json"
    path.write_bytes(ENVELOPE)

    def run(command, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "sops")

    monkeypatch.setattr(vault.subprocess, "run", run)
    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY)

    with pytest.raises(VaultError, match="sops is not on PATH") as caught:
        subject.read(path)
    assert "secretspec set SOPS_AGE_KEY" not in str(caught.value)


def test_a_hanging_sops_is_a_vault_error_that_names_the_command(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    root.mkdir()
    path = root / "demo.json"
    path.write_bytes(ENVELOPE)

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(vault.subprocess, "run", run)
    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY)

    with pytest.raises(VaultError, match="timed out after 120s") as caught:
        subject.read(path)
    assert "sops decrypt" in str(caught.value)


def test_the_child_environment_carries_only_the_resolved_identity(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    root.mkdir()
    path = root / "demo.json"
    path.write_bytes(ENVELOPE)
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", "age1foreignrecipient")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", "/tmp/foreign-key.txt")
    seen: list[dict[str, str]] = []

    def run(command, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, b'{"keep": 1}', b"")

    monkeypatch.setattr(vault.subprocess, "run", run)
    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: f" {KEY}\n")

    assert subject.read(path) == b'{"keep": 1}'
    assert [name for name in seen[0] if name.startswith("SOPS_")] == ["SOPS_AGE_KEY"]
    assert seen[0]["SOPS_AGE_KEY"] == KEY


def test_a_failing_sops_keeps_the_end_of_its_error(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    root.mkdir()
    path = root / "demo.json"
    path.write_bytes(ENVELOPE)
    stderr = b"\n".join(
        [
            b"Failed to get the data key required to decrypt the SOPS file.",
            b"",
            b"Group 0: FAILED",
            b"  age1foreignrecipient: FAILED",
            b"    - identity did not match any of the recipients",
            b"Recovery failed because no master key was able to decrypt the file.",
            b"In order for SOPS to recover the file, at least one key has to be successful,",
            b"but none were.",
        ]
    )

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 128, b"", stderr)

    monkeypatch.setattr(vault.subprocess, "run", run)
    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY)

    with pytest.raises(VaultError) as caught:
        subject.read(path)
    message = str(caught.value)
    assert str(path) in message
    assert "identity did not match any of the recipients" in message
    assert "but none were." in message
    assert "Failed to get the data key" not in message


def test_a_failing_sops_caps_its_error_tail(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    root.mkdir()
    path = root / "demo.json"
    path.write_bytes(ENVELOPE)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, b"", b"y" * 1000)

    monkeypatch.setattr(vault.subprocess, "run", run)
    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: KEY)

    with pytest.raises(VaultError) as caught:
        subject.read(path)
    message = str(caught.value)
    assert message.endswith("y" * 400)
    assert "y" * 401 not in message


@pytest.mark.skipif(shutil.which("sops") is None, reason="the sops binary is not on PATH")
def test_a_real_sops_envelope_round_trips_through_write_and_read(tmp_path):
    root = tmp_path / "secrets"
    (root / "demo").mkdir(parents=True)
    (root / ".sops.yaml").write_text(
        f"creation_rules:\n  - path_regex: .*\n    age: {SOPS_RECIPIENT}\n"
    )
    (tmp_path / "secretspec.toml").write_text('[project]\nname = "vault-test"\n')
    subject = Vault(root, manifest=tmp_path / "secretspec.toml", identity=lambda: SOPS_IDENTITY)
    bodies = {
        "demo/roundsync.zip": bytes(range(256)) * 4 + b"PK\x03\x04\x14\x00\x00\x00",
        "demo/private.json": b"[1, 2, 3]",
    }

    for name, body in bodies.items():
        path = root / name
        envelope = subject.write(path, body)
        assert envelope.startswith(b"{")
        assert b'"sops"' in envelope
        path.write_bytes(envelope)

    # Creation rules are an encrypt input only, so a tree whose config is gone
    # still opens with the key in the environment.
    (root / ".sops.yaml").unlink()

    for name, body in bodies.items():
        assert subject.read(root / name) == body


def test_the_manifest_defaults_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETSPEC_FILE", str(tmp_path / "custom.toml"))
    assert Vault(tmp_path / "secrets").manifest == tmp_path / "custom.toml"


def test_the_manifest_defaults_to_the_repository_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRETSPEC_FILE", raising=False)
    assert Vault(tmp_path / "secrets").manifest == Path("secretspec.toml")


def test_the_default_identity_resolves_through_secretspec(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    path = root / "demo.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(ENVELOPE)
    manifest = tmp_path / "secretspec.toml"
    manifest.write_text('[project]\nname = "demo"\n')
    seen: dict[str, str] = {}

    def resolve(*, path: str, reason: str):
        seen["path"] = path
        seen["reason"] = reason
        return SimpleNamespace(secrets={"SOPS_AGE_KEY": SimpleNamespace(get=KEY)})

    monkeypatch.setattr(vault.secretspec, "resolve", resolve)
    run, _ = recorder(b'{"keep": 1}')

    assert Vault(root, manifest=manifest, run=run).read(path) == b'{"keep": 1}'
    assert seen == {"path": str(manifest), "reason": vault.REASON}


def test_a_secret_the_sdk_cannot_resolve_names_the_command(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    path = root / "demo.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(ENVELOPE)
    manifest = tmp_path / "secretspec.toml"

    def resolve(**_kwargs: object):
        raise vault.secretspec.MissingRequiredError(["SOPS_AGE_KEY"])

    monkeypatch.setattr(vault.secretspec, "resolve", resolve)

    with pytest.raises(VaultError, match="secretspec set SOPS_AGE_KEY") as caught:
        Vault(root, manifest=manifest, run=refuse).read(path)
    assert str(manifest) in str(caught.value)


def test_diff_compares_a_stored_envelope_with_a_plaintext_export(tmp_path):
    root = tmp_path / "secrets"
    path = root / "demo.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(ENVELOPE)
    stored = artifacts.dumps({"keep": 1, "gone": True})
    fresh = tmp_path / "fresh.json"
    fresh.write_bytes(artifacts.dumps({"keep": 2}))
    run, _ = recorder(stored)
    recipe = recipe_from(VALID)
    recipe.dir = root
    recipe.data_dir = root
    recipe.blob_dir = root
    recipe.vault = Vault(root, identity=lambda: KEY, run=run)

    report = commands.diff_artifacts(recipe, fresh)

    assert '- "gone": true' in report
    assert '- "keep": 1' in report
    assert '+ "keep": 2' in report
