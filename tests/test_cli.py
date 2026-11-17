"""The command line: flags reach the store, and failures exit non-zero."""

from __future__ import annotations

import pytest
from support import steps_file

from uiu import cli


def make_tree(tmp_path):
    apps = tmp_path / "apps"
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (apps / "demo").mkdir(parents=True)
    (apps / "demo" / "steps.py").write_text(steps_file())
    return apps, secrets


def test_main_lists_recipes(tmp_path, capsys):
    apps, secrets = make_tree(tmp_path)

    assert cli.main(["list", "--apps", str(apps), "--secrets", str(secrets)]) == 0

    output = capsys.readouterr().out
    assert "com.example.demo" in output
    assert str(secrets) in output


def test_main_reports_a_recipe_error_on_stderr(tmp_path, capsys):
    apps, secrets = make_tree(tmp_path)

    code = cli.main(
        ["backup", "--app", "com.example.other", "--apps", str(apps), "--secrets", str(secrets)]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("uiu: ")


def test_probe_chain_keeps_the_order_of_tap_and_step():
    args = cli.build_parser().parse_args(
        [
            "probe",
            "--tap",
            '{"text": "a"}',
            "--step",
            '{"key": {"code": "BACK"}}',
            "--tap",
            '{"text": "b"}',
        ]
    )
    assert args.chain == [
        ("tap", '{"text": "a"}'),
        ("step", '{"key": {"code": "BACK"}}'),
        ("tap", '{"text": "b"}'),
    ]


def test_main_requires_an_action():
    with pytest.raises(SystemExit) as exit:
        cli.build_parser().parse_args([])
    assert exit.value.code == 2
