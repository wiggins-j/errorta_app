"""S7 publish — OUTWARD-FACING gating + reads (F147 §8.6, §14).

Grounded against the real ``coding.py`` publish routes. The marquee properties:
``publish pr`` / ``publish new-repo`` NEVER fire without an explicit ``--yes``
(non-interactive) and surface exactly what will happen; ``new-repo`` defaults to
private; ``auth-status`` never prints a token.
"""
from __future__ import annotations

import json

import httpx
import pytest

from errorta_cli import registry
from errorta_cli.client import ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient
from errorta_cli.errors import CliError

from .conftest import RouteClient

PID = "proj-1"
BASE = f"/coding/projects/{PID}/publish"


def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Reads: targets / events / auth-status.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("args", "route"),
    [
        ([], f"{BASE}/targets"),               # bare = targets
        (["targets"], f"{BASE}/targets"),
        (["events"], f"{BASE}/events"),
        (["auth-status"], f"{BASE}/auth-status"),
    ],
)
def test_publish_reads_hit_expected_route(make_ctx, args, route) -> None:
    client = RouteClient()
    registry.dispatch("publish", client, make_ctx(project_id=PID), args)
    assert ("GET", route) in client.calls


def test_auth_status_never_prints_token(make_ctx) -> None:
    # The route contract returns booleans + login, never a token. Even if a token
    # sneaked into the payload, the view selects fields and would not print it.
    resp = {"gh_present": True, "login": "octocat", "token_in_keychain": True,
            "token": "ghp_SECRETTOKEN_should_never_render"}
    client = RouteClient(responses={f"{BASE}/auth-status": resp})
    _payload, text = registry.dispatch("publish", client, make_ctx(project_id=PID),
                                       ["auth-status"])
    assert "octocat" in text
    assert "ghp_SECRETTOKEN" not in text
    assert "SECRETTOKEN" not in text


# --------------------------------------------------------------------------- #
# manual-export — local artifact writer (guard, no outward gate).
# --------------------------------------------------------------------------- #

def test_manual_export_posts_kind(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["origin"] = request.headers.get(ORIGIN_HEADER)
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"kind": "zip", "path": "/tmp/p.zip",
                                         "run_hint": "unzip /tmp/p.zip"})

    with _mock_client(handler) as client:
        _payload, text = registry.dispatch("publish", client, make_ctx(project_id=PID),
                                           ["manual-export", "--kind", "zip"])
    assert seen["path"] == f"{BASE}/manual-export"
    assert seen["origin"] == ORIGIN_VALUE
    assert seen["body"] == {"kind": "zip"}
    assert "/tmp/p.zip" in text


def test_manual_export_rejects_unknown_kind(make_ctx) -> None:
    client = RouteClient()
    _payload, text = registry.dispatch("publish", client, make_ctx(project_id=PID),
                                       ["manual-export", "--kind", "bogus"])
    assert client.calls == []  # never fired
    assert "manual-export" in text.lower()


# --------------------------------------------------------------------------- #
# publish pr — OUTWARD-FACING: requires --yes, surfaces the detail.
# --------------------------------------------------------------------------- #

def test_pr_requires_yes_non_interactive_and_shows_detail(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("publish", client, make_ctx(project_id=PID),
                          ["pr", "--branch", "feature/x", "--title", "My PR"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []  # never opened a PR
    msg = ei.value.message
    assert "pull request" in msg.lower()
    assert "feature/x" in msg          # exact branch shown
    assert "My PR" in msg              # exact title shown
    assert "--yes" in msg


def test_pr_with_yes_posts_body(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"pr_url": "https://github.com/o/r/pull/1"})

    with _mock_client(handler) as client:
        _p, text = registry.dispatch("publish", client, make_ctx(project_id=PID),
                                     ["pr", "--branch", "b", "--title", "t",
                                      "--body", "hello", "--override", "--yes"])
    assert seen["path"] == f"{BASE}/existing-repo-pr"
    assert seen["body"] == {"override": True, "branch": "b", "title": "t",
                            "body_override": "hello"}
    assert "pull/1" in text


def test_pr_json_mode_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("publish", client, make_ctx(project_id=PID),
                          ["pr"], json_mode=True)
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# publish new-repo — private by default; requires --yes.
# --------------------------------------------------------------------------- #

def test_new_repo_defaults_private(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"repo_url": "https://github.com/o/newrepo"})

    with _mock_client(handler) as client:
        registry.dispatch("publish", client, make_ctx(project_id=PID),
                          ["new-repo", "newrepo", "--yes"])
    assert seen["body"] == {"repo_name": "newrepo", "private": True,
                            "local_only": False, "override": False}


def test_new_repo_public_flag_sets_private_false(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"repo_url": "u"})

    with _mock_client(handler) as client:
        registry.dispatch("publish", client, make_ctx(project_id=PID),
                          ["new-repo", "r", "--public", "--yes"])
    assert seen["body"]["private"] is False


def test_new_repo_requires_yes_and_shows_visibility(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("publish", client, make_ctx(project_id=PID),
                          ["new-repo", "myrepo"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []
    # Default is private and the exact repo name is shown.
    assert "PRIVATE" in ei.value.message
    assert "myrepo" in ei.value.message


def test_new_repo_public_names_public_in_confirmation(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("publish", client, make_ctx(project_id=PID),
                          ["new-repo", "r", "--public"])
    assert "PUBLIC" in ei.value.message


# --------------------------------------------------------------------------- #
# Guard invocation: outward mutations guard sole-owner; reads don't.
# --------------------------------------------------------------------------- #

def test_outward_mutations_guard_sole_owner(make_ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: calls.append(1))
    client = RouteClient(default={"pr_url": "u", "repo_url": "u"})
    registry.dispatch("publish", client, make_ctx(project_id=PID),
                      ["pr", "--yes"])
    registry.dispatch("publish", client, make_ctx(project_id=PID),
                      ["new-repo", "r", "--yes"])
    assert len(calls) == 2


def test_publish_reads_do_not_guard(make_ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: calls.append(1))
    for args in ([], ["events"], ["auth-status"]):
        registry.dispatch("publish", RouteClient(), make_ctx(project_id=PID), args)
    assert calls == []


# --------------------------------------------------------------------------- #
# Registry parity for the publish command (argv ≡ slash).
# --------------------------------------------------------------------------- #

def test_publish_parity_argv_slash(make_ctx) -> None:
    argv, slash = RouteClient(), RouteClient()
    registry.dispatch("publish", argv, make_ctx(project_id=PID), ["events"])
    n, base = registry.split_slash("/publish events")
    registry.dispatch(n, slash, make_ctx(project_id=PID), base)
    assert argv.calls == slash.calls == [("GET", f"{BASE}/events")]
