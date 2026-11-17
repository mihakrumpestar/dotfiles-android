"""The androidctl CLI: flags, defaults, error mapping and the profile preflight."""

from __future__ import annotations

import io
import sys

import pytest

from androidctl import cli, commands


def test_serial_is_accepted_before_and_after_the_subcommand():
    before = cli.build_parser().parse_args(["--serial", "A", "report"])
    after = cli.build_parser().parse_args(["report", "--serial", "B"])
    assert before.serial == "A"
    assert after.serial == "B"


def test_files_are_none_by_default_and_split_on_commas_when_given():
    assert cli._files(None) is None
    assert cli._files([]) is None
    assert cli._files(["a,b", "c"]) == ["a", "b", "c"]


def test_packages_come_from_a_file_or_stdin(tmp_path, monkeypatch):
    path = tmp_path / "packages.txt"
    path.write_text("# comment\ncom.a\n\ncom.b\n")
    assert cli._packages(str(path)) == ["com.a", "com.b"]

    monkeypatch.setattr(sys, "stdin", io.StringIO("com.c\n# skip\ncom.d\n"))
    assert cli._packages(None) == ["com.c", "com.d"]


def test_a_command_error_prints_one_line_and_exits_one(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise commands.CommandError("no snapshots for SERIAL")

    monkeypatch.setattr(cli.commands, "report", fail)
    assert cli.main(["report", "--serial", "SERIAL"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "androidctl: no snapshots for SERIAL\n"
    assert "Traceback" not in captured.err


def test_a_profile_error_is_a_clean_cli_error(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise cli.profile.ProfileError("line 2: unknown section [nope]")

    monkeypatch.setattr(cli.commands, "load_profiles", fail)
    assert cli.main(["apply", "--serial", "SERIAL"]) == 1
    assert capsys.readouterr().err == "androidctl: line 2: unknown section [nope]\n"


def test_apply_validates_profiles_before_it_opens_the_device(monkeypatch):
    def no_device(*args, **kwargs):
        raise AssertionError("the device must not be opened before the profile parses")

    monkeypatch.setattr(cli, "Adb", no_device)
    monkeypatch.setattr(
        cli.commands,
        "load_profiles",
        lambda paths: (_ for _ in ()).throw(cli.profile.ProfileError("bad profile")),
    )
    assert cli.main(["apply", "--serial", "SERIAL"]) == 1


def test_a_parse_error_is_one_line_and_exit_two(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["nope"])
    assert caught.value.code == 2
    assert capsys.readouterr().err.startswith("androidctl: ")


def test_apply_and_verify_receive_the_comma_split_files(monkeypatch):
    seen = {}

    def apply(device, files, state, **kwargs):
        seen["apply"] = files
        return 0

    def verify(device, files, **kwargs):
        seen["verify"] = files
        return 0

    monkeypatch.setattr(cli, "Adb", lambda serial: object())
    monkeypatch.setattr(cli.commands, "load_profiles", lambda paths: [])
    monkeypatch.setattr(cli.commands, "apply", apply)
    monkeypatch.setattr(cli.commands, "verify", verify)

    assert cli.main(["apply", "--files", "a,b", "--serial", "S"]) == 0
    assert cli.main(["verify", "--files", "c", "--serial", "S"]) == 0
    assert seen == {"apply": ["a", "b"], "verify": ["c"]}


def test_install_loads_the_app_spec_before_it_opens_the_device(monkeypatch):
    def no_device(*args, **kwargs):
        raise AssertionError("the device must not be opened before the app spec loads")

    def fail(*args, **kwargs):
        raise cli.apps.AppError("no recipe declares package")

    monkeypatch.setattr(cli, "Adb", no_device)
    monkeypatch.setattr(cli.apps, "load", fail)
    assert cli.main(["install", "dev.imranr.obtainium"]) == 1


def test_install_dispatches_the_loaded_spec(monkeypatch):
    spec = cli.apps.AppSpec(
        name="demo",
        package="com.example.demo",
        release="https://example.invalid/release",
        assets={"universal": "app.apk"},
        checksum="asset",
    )
    seen: dict = {}

    def load(package):
        seen["package"] = package
        return spec

    def install(device, got, state, **kwargs):
        seen["spec"] = got
        seen["state"] = state
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(cli, "Adb", lambda serial: object())
    monkeypatch.setattr(cli.apps, "load", load)
    monkeypatch.setattr(cli.commands, "install_app", install)

    assert cli.main(["install", "com.example.demo", "--asset", "app.apk", "--dry-run"]) == 0
    assert seen["package"] == "com.example.demo"
    assert seen["spec"] is spec
    assert seen["state"] == ".state"
    assert seen["kwargs"] == {"asset": "app.apk", "dry_run": True}


def test_init_loads_the_bootstrap_list_before_the_device(monkeypatch):
    def no_device(*args, **kwargs):
        raise AssertionError("the device must not be opened before the bootstrap list loads")

    def fail(package):
        raise cli.apps.AppError("no install spec declares package")

    monkeypatch.setattr(cli, "Adb", no_device)
    monkeypatch.setattr(cli.apps, "load", fail)
    assert cli.main(["init"]) == 1


def test_init_dispatches_the_specs(monkeypatch):
    specs = {
        "moe.shizuku.privileged.api": cli.apps.AppSpec(
            name="shizuku",
            package="moe.shizuku.privileged.api",
            release="https://example.invalid/shizuku",
            assets={"universal": "shizuku-next.apk"},
            checksum="digest",
        ),
        "dev.imranr.obtainium": cli.apps.AppSpec(
            name="obtainium",
            package="dev.imranr.obtainium",
            release="https://example.invalid/obtainium",
            assets={"universal": "app-release.apk"},
            checksum="asset",
        ),
    }
    seen: dict = {}

    def init(device, got, state, **kwargs):
        seen["specs"] = got
        seen["state"] = state
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(cli, "_packages", lambda path: list(specs))
    monkeypatch.setattr(cli.apps, "load", lambda package: specs[package])
    monkeypatch.setattr(cli, "Adb", lambda serial: object())
    monkeypatch.setattr(cli.commands, "init_device", init)

    assert cli.main(["init", "--dry-run"]) == 0
    assert seen["specs"] == list(specs.values())
    assert seen["state"] == ".state"
    assert seen["kwargs"] == {"dry_run": True}


@pytest.mark.parametrize("argv", [["disable", "--no-snap", "list.txt"], ["apply", "--ye"]])
def test_abbreviated_safety_flags_are_refused(argv):
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(argv)
    assert caught.value.code == 2


def test_a_bad_package_is_rejected_before_the_device_is_opened(tmp_path, monkeypatch):
    path = tmp_path / "packages.txt"
    path.write_text("com.a\ncom.b;reboot\n")

    def no_device(*args, **kwargs):
        raise AssertionError("the device must not be opened before the list is validated")

    monkeypatch.setattr(cli, "Adb", no_device)
    assert cli.main(["disable", str(path), "--dry-run"]) == 1
