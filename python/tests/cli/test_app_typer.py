"""Typer argv surface regressions."""
from __future__ import annotations

from typer.testing import CliRunner

from errorta_cli.app import app


def test_registered_commands_do_not_expose_closure_defaults_as_options() -> None:
    result = CliRunner().invoke(app, ["status", "--help"])

    assert result.exit_code == 0
    assert "Show sidecar health" in result.output
    assert "---name" not in result.output
