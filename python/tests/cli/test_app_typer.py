"""Typer argv surface regressions."""
from __future__ import annotations

from typer.testing import CliRunner

from errorta_cli import app as app_module
from errorta_cli.sidecar import SidecarHandle

app = app_module.app


def test_registered_commands_do_not_expose_closure_defaults_as_options() -> None:
    result = CliRunner().invoke(app, ["status", "--help"])

    assert result.exit_code == 0
    assert "Show sidecar health" in result.output
    assert "---name" not in result.output


def test_post_subcommand_poll_interval_is_extracted() -> None:
    overrides, rest = app_module._extract_post_globals(
        ["--watch", "--poll-interval", "0.25", "--role", "dev"]
    )

    assert overrides["poll_interval"] == 0.25
    assert rest == ["--watch", "--role", "dev"]


def test_invalid_post_subcommand_poll_interval_fails_cleanly() -> None:
    result = CliRunner().invoke(app, ["tasks", "--poll-interval", "soon"])

    assert result.exit_code == 1
    assert "--poll-interval must be a number" in result.output


def test_trailing_value_global_without_value_errors() -> None:
    # A value option as the last token (`errorta status --home`) used to be
    # silently dropped — now it's a clean error, not a no-op against the default.
    import pytest

    from errorta_cli.errors import CliError

    for token in ("--home", "--verbosity", "-V", "--poll-interval"):
        with pytest.raises(CliError, match="needs a value"):
            app_module._extract_post_globals([token])


# --- R1: a global-named token that is a value-option VALUE is not eaten -------

def test_value_option_value_matching_a_global_is_preserved() -> None:
    from errorta_cli import registry

    # `errorta log --grep --json`: `--json` is the grep pattern, not the global.
    overrides, rest = app_module._extract_post_globals(
        ["--grep", "--json"], registry.get("log"))
    assert "json" not in overrides
    assert rest == ["--grep", "--json"]
    # …and resolve_args then binds it as the value (round-trip proof).
    assert registry.resolve_args(registry.get("log"), rest)["grep"] == "--json"


def test_global_after_a_value_options_real_value_is_still_harvested() -> None:
    from errorta_cli import registry

    overrides, rest = app_module._extract_post_globals(
        ["--grep", "dev", "--json"], registry.get("log"))
    assert overrides["json"] is True
    assert rest == ["--grep", "dev"]


def test_global_is_harvested_when_command_has_no_such_value_option() -> None:
    from errorta_cli import registry

    # `status` has no value-options, so a post-subcommand `--json` is the global.
    overrides, rest = app_module._extract_post_globals(
        ["--json"], registry.get("status"))
    assert overrides["json"] is True
    assert rest == []


def test_global_looking_required_positional_is_preserved() -> None:
    from errorta_cli import registry

    overrides, rest = app_module._extract_post_globals(
        ["--json"], registry.get("open")
    )
    assert overrides == {}
    assert rest == ["--json"]


def test_global_after_required_positional_option_is_harvested() -> None:
    from errorta_cli import registry

    overrides, rest = app_module._extract_post_globals(
        ["--id", "project", "--json"], registry.get("open")
    )
    assert overrides == {"json": True}
    assert rest == ["--id", "project"]


def test_post_globals_without_command_preserves_legacy_behavior() -> None:
    # command=None ⇒ schema-blind (direct callers / back-compat).
    overrides, rest = app_module._extract_post_globals(["--home", "/x", "--role", "dev"])
    assert overrides["home"] == "/x"
    assert rest == ["--role", "dev"]


def test_watch_uses_post_subcommand_poll_interval(monkeypatch, tmp_path) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(app_module.config, "resolve_home", lambda _override=None: tmp_path)
    monkeypatch.setattr(
        app_module.sidecar,
        "resolve",
        lambda *a, **k: SidecarHandle(
            base_url="http://127.0.0.1:1",
            port=1,
            pid=1,
            commit=None,
            started_by="cli",
            adopted=True,
        ),
    )

    class _Client:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_run_watch(name, client, ctx, raw_args):
        seen["name"] = name
        seen["poll_interval"] = ctx.poll_interval
        seen["raw_args"] = raw_args

    monkeypatch.setattr(app_module, "SidecarClient", _Client)
    from errorta_cli import watch

    original_maybe_run_watch = watch.maybe_run_watch

    def fake_maybe_run_watch(name, ctx, raw_args):
        seen["helper_called"] = True
        return original_maybe_run_watch(name, ctx, raw_args)

    monkeypatch.setattr(watch, "maybe_run_watch", fake_maybe_run_watch)
    monkeypatch.setattr("errorta_cli.watch.run_watch", fake_run_watch)

    result = CliRunner().invoke(
        app, ["tasks", "--watch", "--poll-interval", "0.25"]
    )

    assert result.exit_code == 0
    assert seen == {
        "name": "tasks",
        "poll_interval": 0.25,
        "raw_args": ["--watch"],
        "helper_called": True,
    }


def test_run_watch_streams_and_preserves_exit_code(monkeypatch, tmp_path) -> None:
    """`run --watch` (argv path): never enters run_watch, drops --watch, dispatches
    `run` normally with a note, and still propagates a failure exit code."""
    monkeypatch.setattr(app_module.config, "resolve_home", lambda _override=None: tmp_path)
    monkeypatch.setattr(app_module.config, "build_commit", lambda: None)
    monkeypatch.setattr(
        app_module.sidecar, "resolve",
        lambda *a, **k: SidecarHandle(base_url="http://127.0.0.1:1", port=1, pid=1,
                                      commit=None, started_by="cli", adopted=True),
    )

    class _Client:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(app_module, "SidecarClient", _Client)

    watched = {"called": False}
    monkeypatch.setattr("errorta_cli.watch.run_watch",
                        lambda *a, **k: watched.__setitem__("called", True))

    seen: dict[str, object] = {}

    def fake_dispatch(name, client, ctx, raw_args, *, json_mode=False):
        seen["name"] = name
        seen["raw_args"] = list(raw_args)
        return ({"_exit_code": 7}, "TERMINAL")  # a failure-class run

    monkeypatch.setattr(app_module.registry, "dispatch", fake_dispatch)

    result = CliRunner().invoke(app, ["run", "--watch", "--yes"])

    assert watched["called"] is False              # run never entered the watch loop
    assert seen["name"] == "run"
    assert "--watch" not in seen["raw_args"]        # the flag was stripped
    assert "already streams live" in result.output  # the note
    assert result.exit_code == 7                    # failure exit code preserved
