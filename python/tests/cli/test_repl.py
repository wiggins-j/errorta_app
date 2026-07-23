"""REPL line handling — the pure `handle_line` core (no live terminal).

Covers the new-user ergonomics: a stray `errorta ` prefix typed inside the
session, shell muscle-memory (`cd`/`ls`) that the REPL has no verb for, and the
unknown-command hint pointing at `/quit`.
"""
from __future__ import annotations

from errorta_cli import repl, watch

from .conftest import RouteClient


def test_errorta_prefix_is_stripped_and_run(make_ctx):
    # Typing the whole `errorta help` line inside the REPL should run `help`
    # (not error as `/errorta`), with a one-line nudge to drop the prefix.
    out = repl.handle_line("errorta help", make_ctx(), RouteClient())
    assert "drop the 'errorta' prefix" in out
    assert "Commands:" in out  # the real /help output still rendered


def test_bare_errorta_word_is_guided(make_ctx):
    out = repl.handle_line("errorta", make_ctx(), RouteClient())
    assert "already inside errorta" in out
    assert "/quit" in out


def test_cd_is_redirected_to_project_verbs(make_ctx):
    out = repl.handle_line("cd users", make_ctx(), RouteClient())
    assert "/projects" in out and "/open" in out
    assert "unknown command" not in out


def test_unknown_command_hint_mentions_quit(make_ctx):
    out = repl.handle_line("bogus", make_ctx(), RouteClient())
    assert out == "unknown command: /bogus (try /help, or /quit to leave)"


def _feed_lines(monkeypatch, lines):
    """Drive ``run_repl`` off a canned line list; EOF ends the session."""
    it = iter(lines)

    class _Session:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, _prompt):
            try:
                return next(it)
            except StopIteration as exc:
                raise EOFError from exc

    monkeypatch.setattr("prompt_toolkit.PromptSession", _Session)


def test_repl_routes_registry_commands_through_shared_watch_helper(
    make_ctx, monkeypatch
):
    calls: list[tuple[str, list[str]]] = []

    def fake_maybe_run_watch(name, ctx, raw_args):
        calls.append((name, raw_args))
        return watch.WatchDecision(False, raw_args)

    _feed_lines(monkeypatch, ["/tasks"])
    monkeypatch.setattr(watch, "maybe_run_watch", fake_maybe_run_watch)

    repl.run_repl(make_ctx(project_id="p"), RouteClient(default={"tasks": []}))

    assert calls == [("tasks", [])]


def test_run_watch_and_status_share_one_registry_helper(
    make_ctx, monkeypatch, capsys
):
    # The self-streaming `/run --watch` case and a plain `/status` must dispatch
    # through the SAME helper — differing only by the printed note — and `/run`'s
    # `--watch` must already be stripped by the time it reaches that helper.
    seen: list[tuple[str, list[str]]] = []

    def fake_handle_registry(name, raw_args, ctx, client):
        seen.append((name, list(raw_args)))
        return f"ran {name}"

    _feed_lines(monkeypatch, ["/run --watch", "/status"])
    monkeypatch.setattr(repl, "handle_registry", fake_handle_registry)

    repl.run_repl(make_ctx(project_id="p"), RouteClient())

    out = capsys.readouterr().out
    assert "already streams live" in out  # the self-streaming note is printed
    assert seen == [("run", []), ("status", [])]  # --watch stripped; same helper
    assert "ran run" in out and "ran status" in out


def test_read_command_watch_routes_to_run_watch(make_ctx, monkeypatch):
    # A read command with `--watch` still takes the handled/poll-loop path
    # (run_watch), never the one-shot dispatch helper.
    ran: list[tuple[str, list[str]]] = []
    helper: list[str] = []

    def fake_run_watch(name, client, ctx, raw_args, **_kwargs):
        ran.append((name, list(raw_args)))

    _feed_lines(monkeypatch, ["/status --watch"])
    monkeypatch.setattr(watch, "run_watch", fake_run_watch)
    monkeypatch.setattr(
        repl, "handle_registry",
        lambda *a, **k: helper.append(a[0]) or "",
    )

    repl.run_repl(make_ctx(project_id="p"), RouteClient())

    assert ran == [("status", ["--watch"])]
    assert helper == []  # the handled path never touches the dispatch helper


def test_builtins_still_render_once_in_repl(make_ctx, monkeypatch, capsys):
    _feed_lines(monkeypatch, ["/help", "/verbosity 3"])

    repl.run_repl(make_ctx(project_id="p"), RouteClient())

    out = capsys.readouterr().out
    assert "Commands:" in out           # /help
    assert "verbosity: 3" in out        # /verbosity


def test_quit_exits_repl(make_ctx, monkeypatch, capsys):
    seen: list[str] = []
    _feed_lines(monkeypatch, ["/quit", "/status"])
    monkeypatch.setattr(
        repl, "handle_registry", lambda *a, **k: seen.append(a[0]) or ""
    )

    repl.run_repl(make_ctx(project_id="p"), RouteClient())

    out = capsys.readouterr().out
    assert "bye" in out
    assert seen == []  # /quit returns before the line after it runs
