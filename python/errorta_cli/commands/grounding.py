"""``grounding`` — the project's corpus binding + retrieval + memory (§8).

Grounded against the real ``coding.py`` (file:line inline):

* ``grounding`` (bare) / ``binding [show]`` → ``GET .../grounding/corpus-binding`` (621)
* ``grounding binding set --mode M [--corpus C] [--source-root P]``
      → ``PUT .../grounding/corpus-binding`` (``_CorpusBindingBody`` 221/1047)
* ``grounding corpora``       → ``GET  /coding/grounding/corpora``        (435)
* ``grounding capabilities``  → ``GET  .../grounding/capabilities``       (580)
* ``grounding retrieve --q Q [--k N]`` → ``GET .../grounding/retrieve?q=&k=`` (597)
* ``grounding bootstrap --corpus C [--source-root P]``
      → ``POST .../grounding/bootstrap`` (``_BootstrapBody`` 227/1069)
      → poll ``GET .../grounding/bootstrap/{job_id}`` (1099)
* ``grounding memory sync``   → ``POST .../grounding/memory/sync``        (1118)
* ``grounding memory rebuild [--mode from_ledger|from_repo]``
      → ``POST .../grounding/memory/rebuild`` (``_MemorySyncBody`` 1114/1134)
* ``grounding build-from-project [--corpus C]``
      → ``POST .../grounding/build-from-project`` (``_BuildFromProjectBody`` 1160/1164)
* ``grounding working-memory`` → ``GET .../pm-working-memory``            (633)

**Residency.** ``binding set`` / ``bootstrap`` / ``memory *`` /
``build-from-project`` are ``refuse_local_dataplane_if_remote`` writes;
``retrieve`` fails closed under remote residency without a remote adapter — all
surface as :class:`ResidencyRefused` (exit 4) via the client's 409 mapping. The
mutations take the sole-owner guard + ``--yes``/confirm gate; reads don't.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import grounding as _rg
from ..render import is_no_project, muted, no_project, render
from ..session import Context
from . import _base, _mutate

_MUTATING = {"bootstrap", "memory", "build-from-project"}
_TERMINAL_JOB = {"done", "failed", "interrupted", "error"}


def _g(ctx: Context) -> str:
    return f"/coding/projects/{ctx.project_id}/grounding"


def _binding_show(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    return {"_kind": "binding", **(client.get_json(f"{_g(ctx)}/corpus-binding") or {})}


def _binding_set(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, "set the corpus binding",
                           note="rebinds the project's grounding corpus"):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {"mode": str(args.get("mode") or "none")}
    if args.get("corpus"):
        body["corpus_id"] = str(args["corpus"])
    if args.get("source-root"):
        body["source_root"] = str(args["source-root"])
    return {"_kind": "binding", **(client.put_json(f"{_g(ctx)}/corpus-binding", json=body) or {})}


def _binding(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if str(args.get("p1") or "").lower() == "set":
        return _binding_set(client, ctx, args)
    return _binding_show(client, ctx)


def _corpora(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    return {"_kind": "corpora", **(client.get_json("/coding/grounding/corpora") or {})}


def _capabilities(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    return {"_kind": "capabilities", **(client.get_json(f"{_g(ctx)}/capabilities") or {})}


def _retrieve(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    q = args.get("q")
    if not q:
        return _base.usage("grounding retrieve --q \"<query>\" [--k N]")
    params: dict[str, Any] = {"q": str(q)}
    if args.get("k") is not None:
        params["k"] = str(args["k"])
    return {"_kind": "retrieve", **(client.get_json(f"{_g(ctx)}/retrieve", params=params) or {})}


def _poll_bootstrap(client: SidecarClient, ctx: Context, job_id: str, *,
                    sleep: Callable[[float], None] = time.sleep,
                    interval: float = 1.0, max_attempts: int = 600) -> dict[str, Any]:
    for _ in range(max_attempts):
        job = (client.get_json(f"{_g(ctx)}/bootstrap/{job_id}") or {}).get("job") or {}
        if str(job.get("status")) in _TERMINAL_JOB:
            return job
        sleep(interval)
    raise CliError("corpus bootstrap timed out", code="bootstrap_timeout")


def _bootstrap(client: SidecarClient, ctx: Context, args: dict[str, Any],
               *, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    corpus = str(args.get("corpus") or "").strip()
    if not corpus:
        return _base.usage("grounding bootstrap --corpus <id> [--source-root PATH]")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"bootstrap corpus '{corpus}'",
                           note="ingests source into a grounding corpus"):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {"corpus_id": corpus}
    if args.get("source-root"):
        body["source_root"] = str(args["source-root"])
    started = (client.post_json(f"{_g(ctx)}/bootstrap", json=body) or {}).get("job") or {}
    job_id = str(started.get("job_id") or "")
    if str(started.get("status")) in _TERMINAL_JOB or not job_id:
        return {"_kind": "bootstrap", "job": started}
    return {"_kind": "bootstrap", "job": _poll_bootstrap(client, ctx, job_id, sleep=sleep)}


def _memory(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    sub = str(args.get("p1") or "").lower()
    if sub not in ("sync", "rebuild"):
        return _base.usage("grounding memory sync | rebuild [--mode from_ledger|from_repo]")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"{sub} project memory",
                           note="re-projects the ledger into project memory"):
        return {"_kind": "aborted"}
    if sub == "rebuild":
        body = {"mode": str(args.get("mode") or "from_ledger")}
        result = client.post_json(f"{_g(ctx)}/memory/rebuild", json=body) or {}
    else:
        result = client.post_json(f"{_g(ctx)}/memory/sync", json={}) or {}
    return {"_kind": "memory", "sub": sub, **result}


def _build_from_project(
    client: SidecarClient, ctx: Context, args: dict[str, Any]
) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, "build a corpus from this project",
                           note="ingests the merged master tree into a corpus"):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {}
    if args.get("corpus"):
        body["corpus_id"] = str(args["corpus"])
    result = client.post_json(f"{_g(ctx)}/build-from-project", json=body) or {}
    return {"_kind": "build", "result": result}


def _working_memory(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    wm = client.get_json(f"/coding/projects/{ctx.project_id}/pm-working-memory") or {}
    return {"_kind": "wm", **wm}


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    action = str(args.get("action") or "").strip().lower()
    if action in _MUTATING and args.get("watch"):
        raise CliError(
            f"--watch is for read views; `grounding {action}` mutates and can't "
            "be watched.", code="watch_on_mutation")
    if action in ("", "binding"):
        return _binding(client, ctx, args)
    if action == "corpora":
        return _corpora(client, ctx)
    if action in ("capabilities", "caps"):
        return _capabilities(client, ctx)
    if action == "retrieve":
        return _retrieve(client, ctx, args)
    if action == "bootstrap":
        return _bootstrap(client, ctx, args)
    if action == "memory":
        return _memory(client, ctx, args)
    if action == "build-from-project":
        return _build_from_project(client, ctx, args)
    if action in ("working-memory", "wm"):
        return _working_memory(client, ctx)
    return _base.usage(
        "grounding [binding [set --mode M --corpus C --source-root P] | corpora | "
        "capabilities | retrieve --q Q [--k N] | bootstrap --corpus C [--source-root P] | "
        "memory sync|rebuild [--mode M] | build-from-project [--corpus C] | working-memory]")


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("aborted — nothing changed."))
    if kind == "binding":
        return _rg.render_binding(payload)
    if kind == "corpora":
        return _rg.render_corpora(payload)
    if kind == "capabilities":
        return _rg.render_capabilities(payload)
    if kind == "retrieve":
        return _rg.render_retrieve(payload)
    if kind == "bootstrap":
        return _rg.render_bootstrap(payload)
    if kind == "memory":
        return _rg.render_memory(payload)
    if kind == "build":
        return _rg.render_build(payload)
    if kind == "wm":
        return _rg.render_working_memory(payload)
    return render(muted("nothing to show"))


register(Command(
    name="grounding",
    help="Project corpus binding + retrieval + memory (grounding).",
    call=_call,
    render=_render,
    params=(
        Param("action", "binding|corpora|capabilities|retrieve|bootstrap|memory|"
                        "build-from-project|working-memory", default=""),
        Param("p1", "sub-verb (binding 'set', memory 'sync'/'rebuild').", default=None),
        Param("mode", "binding set mode / memory rebuild mode.", is_flag=False),
        Param("corpus", "corpus id (binding set / bootstrap / build-from-project).",
              is_flag=False),
        Param("source-root", "source root path (binding set / bootstrap).", is_flag=False),
        Param("q", "retrieve: the query string.", is_flag=False),
        Param("k", "retrieve: number of hits (default 6).", is_flag=False),
        Param("watch", "re-render on the poll loop (read views only).", is_flag=True),
        Param("yes", "Skip the confirmation prompt (required non-interactively).",
              is_flag=True),
    ),
))
