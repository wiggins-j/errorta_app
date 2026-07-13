"""REPL line handling — the pure `handle_line` core (no live terminal).

Covers the new-user ergonomics: a stray `errorta ` prefix typed inside the
session, shell muscle-memory (`cd`/`ls`) that the REPL has no verb for, and the
unknown-command hint pointing at `/quit`.
"""
from __future__ import annotations

from errorta_cli import repl

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
