"""S7 grounding — binding / corpora / capabilities / retrieve / bootstrap / memory.

Grounded against the real ``coding.py`` grounding routes. Mutations gate on
``--yes`` + guard; the bootstrap job-polls to a terminal status; RESID writes
surface as ResidencyRefused (exit 4).
"""
from __future__ import annotations

import json

import httpx
import pytest

from errorta_cli import registry
from errorta_cli.client import SidecarClient
from errorta_cli.errors import CliError, ResidencyRefused

from .conftest import RouteClient

PID = "proj-1"
G = f"/coding/projects/{PID}/grounding"


def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Reads.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("args", "method", "route"),
    [
        ([], "GET", f"{G}/corpus-binding"),                 # bare = binding
        (["binding"], "GET", f"{G}/corpus-binding"),
        (["corpora"], "GET", "/coding/grounding/corpora"),  # NOT project-scoped
        (["capabilities"], "GET", f"{G}/capabilities"),
        (["working-memory"], "GET", f"/coding/projects/{PID}/pm-working-memory"),
    ],
)
def test_grounding_reads_hit_route(make_ctx, args, method, route) -> None:
    client = RouteClient()
    registry.dispatch("grounding", client, make_ctx(project_id=PID), args)
    assert (method, route) in client.calls


def test_retrieve_passes_query_params(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["q"] = request.url.params.get("q")
        seen["k"] = request.url.params.get("k")
        return httpx.Response(200, json={"hits": [], "status": "ok"})

    with _mock_client(handler) as client:
        registry.dispatch("grounding", client, make_ctx(project_id=PID),
                          ["retrieve", "--q", "how does auth work", "--k", "3"])
    assert seen["path"] == f"{G}/retrieve"
    assert seen["q"] == "how does auth work"
    assert seen["k"] == "3"


def test_retrieve_without_query_is_usage(make_ctx) -> None:
    client = RouteClient()
    _p, text = registry.dispatch("grounding", client, make_ctx(project_id=PID),
                                 ["retrieve"])
    assert client.calls == []
    assert "retrieve" in text.lower()


# --------------------------------------------------------------------------- #
# binding set — mutation.
# --------------------------------------------------------------------------- #

def test_binding_set_puts_body(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"binding": {"mode": "existing_corpus"}})

    with _mock_client(handler) as client:
        registry.dispatch("grounding", client, make_ctx(project_id=PID),
                          ["binding", "set", "--mode", "existing_corpus",
                           "--corpus", "c1", "--yes"])
    assert seen["method"] == "PUT"
    assert seen["body"] == {"mode": "existing_corpus", "corpus_id": "c1"}


def test_binding_set_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("grounding", client, make_ctx(project_id=PID),
                          ["binding", "set", "--mode", "none"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# bootstrap — job poll to terminal.
# --------------------------------------------------------------------------- #

def test_bootstrap_polls_to_done(make_ctx) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "POST" and path == f"{G}/bootstrap":
            body = json.loads(request.content)
            assert body == {"corpus_id": "myc"}
            return httpx.Response(200, json={"job": {"job_id": "boot_1", "status": "running"}})
        # GET the job — running once, then done.
        n = sum(1 for m, p in calls if m == "GET" and p.endswith("/boot_1"))
        status = "running" if n < 2 else "done"
        return httpx.Response(200, json={"job": {"job_id": "boot_1", "status": status,
                                                 "corpus_id": "myc"}})

    with _mock_client(handler) as client:
        payload, text = registry.dispatch("grounding", client, make_ctx(project_id=PID),
                                          ["bootstrap", "--corpus", "myc", "--yes"])
    assert payload["job"]["status"] == "done"
    assert "done" in text
    # Polled the job route at least twice.
    assert sum(1 for m, p in calls if m == "GET" and p.endswith("/boot_1")) >= 2


def test_bootstrap_terminal_immediately_no_poll(make_ctx) -> None:
    # Local path returns a terminal job synchronously — no extra GET poll needed.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job": {"job_id": "b", "status": "done",
                                                 "corpus_id": "c"}})

    with _mock_client(handler) as client:
        payload, _t = registry.dispatch("grounding", client, make_ctx(project_id=PID),
                                        ["bootstrap", "--corpus", "c", "--yes"])
    assert payload["job"]["status"] == "done"


def test_bootstrap_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("grounding", client, make_ctx(project_id=PID),
                          ["bootstrap", "--corpus", "c"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# memory sync/rebuild + build-from-project.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("args", "route", "body"),
    [
        (["memory", "sync", "--yes"], f"{G}/memory/sync", {}),
        (["memory", "rebuild", "--yes"], f"{G}/memory/rebuild", {"mode": "from_ledger"}),
        (["memory", "rebuild", "--mode", "from_repo", "--yes"],
         f"{G}/memory/rebuild", {"mode": "from_repo"}),
        (["build-from-project", "--corpus", "c", "--yes"],
         f"{G}/build-from-project", {"corpus_id": "c"}),
    ],
)
def test_memory_and_build_post_body(make_ctx, args, route, body) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"counts": {}, "result": {}})

    with _mock_client(handler) as client:
        registry.dispatch("grounding", client, make_ctx(project_id=PID), args)
    assert seen["path"] == route
    assert seen["body"] == body


def test_memory_write_residency_refused(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {"code": "residency_unsupported_path",
                                                    "message": "remote"}})

    with _mock_client(handler) as client:
        with pytest.raises(ResidencyRefused) as ei:
            registry.dispatch("grounding", client, make_ctx(project_id=PID),
                              ["memory", "sync", "--yes"])
    assert ei.value.exit_code == 4


# --------------------------------------------------------------------------- #
# Guard + parity.
# --------------------------------------------------------------------------- #

def test_grounding_mutations_guard(make_ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: calls.append(1))
    client = RouteClient(default={"binding": {}, "job": {"status": "done"},
                                  "counts": {}, "result": {}})
    for args in (["binding", "set", "--mode", "none", "--yes"],
                 ["bootstrap", "--corpus", "c", "--yes"],
                 ["memory", "sync", "--yes"],
                 ["build-from-project", "--yes"]):
        registry.dispatch("grounding", client, make_ctx(project_id=PID), args)
    assert len(calls) == 4


def test_grounding_reads_do_not_guard(make_ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: calls.append(1))
    for args in ([], ["corpora"], ["capabilities"], ["retrieve", "--q", "x"],
                 ["working-memory"]):
        registry.dispatch("grounding", RouteClient(default={"hits": []}),
                          make_ctx(project_id=PID), args)
    assert calls == []


def test_grounding_parity_argv_slash(make_ctx) -> None:
    argv, slash = RouteClient(), RouteClient()
    registry.dispatch("grounding", argv, make_ctx(project_id=PID), ["capabilities"])
    n, base = registry.split_slash("/grounding capabilities")
    registry.dispatch(n, slash, make_ctx(project_id=PID), base)
    assert argv.calls == slash.calls == [("GET", f"{G}/capabilities")]
