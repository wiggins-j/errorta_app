#!/usr/bin/env python3
"""Live gate: do the AIAR v0.2.4 + v0.2.5 (F098) contracts work for Errorta?

Exercises EVERY v0.2.4 contract Errorta depends on against the live remote AIAR
(example-host, via remote-aiar.json) — through Errorta's own consumption code where
it exists, raw HTTP for the rest. Creates a throwaway instance, runs the full
ingest -> publish -> retrieve lifecycle on it, validates answer/telemetry/
grounding, and DELETES the throwaway instance at the end. Real corpora are never
touched.

Run:  PYTHONPATH=python python python/scripts/validate_aiar_v024_live.py
Exit 0 = all required contracts pass.
"""
from __future__ import annotations

import sys
import time

import httpx

from errorta_project_grounding.remote_adapter import (
    active_remote_adapter,
    remote_aiar_config,
)
from errorta_query.aiar_retrieve import remote_aiar_retrieve

TEST_INSTANCE = "errorta-validate-v024"
RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def skip(name: str, reason: str) -> None:
    SKIPPED.append((name, reason))
    print(f"  [SKIP] {name} — {reason}")


def main() -> int:
    cfg = remote_aiar_config()
    if cfg is None:
        print("FAIL: no remote AIAR configured (remote-aiar.json).")
        return 2
    base = cfg.base_url
    headers = {"Content-Type": "application/json"}
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    client = httpx.Client(timeout=60.0, headers=headers)
    adapter = active_remote_adapter()
    print(f"AIAR target: {base}\n")

    # --- A4 capability manifest ---------------------------------------------
    print("A4 capability manifest (GET /capabilities)")
    caps = client.get(f"{base}/capabilities").json()

    def _ver_tuple(v: str) -> tuple:
        try:
            return tuple(int(x) for x in str(v).split(".")[:3])
        except Exception:
            return (0,)

    check("aiar_version is >= 0.2.4", _ver_tuple(caps.get("aiar_version")) >= (0, 2, 4),
          f"version={caps.get('aiar_version')}")
    check("capabilities schema_version", caps.get("schema_version") == "aiar.capabilities.v1")
    feats = caps.get("features", {})
    check("pure_retrieve feature on", feats.get("pure_retrieve") is True)
    # F098: generation capability reflects active_model readiness.
    check("generation feature on (F098)", feats.get("generation") is True,
          f"generation={feats.get('generation')}")
    check("backend_id present", bool(caps.get("backend_id")), f"backend_id={caps.get('backend_id')}")

    # --- /healthz markers (incl. F098 active-model readiness) ----------------
    hz = client.get(f"{base}/healthz").json()
    check("healthz retrieve_schema_version", hz.get("retrieve_schema_version") == "aiar.retrieve.v1")
    check("healthz active_model_ready (F098)", hz.get("active_model_ready") is True,
          f"active_model={hz.get('active_model')} ready={hz.get('active_model_ready')}")

    # --- A3 ingest lifecycle on a throwaway instance ------------------------
    print("\nA3 ingest -> publish -> health (throwaway instance, via Errorta adapter)")
    try:
        adapter.ensure_instance(TEST_INSTANCE, display_name="Errorta v0.2.4 validation")
        ref = adapter.ingest_record(
            corpus_id=TEST_INSTANCE,
            content="Errorta validates AIAR v0.2.4. The marker phrase is BLUEHERON-42.",
            metadata={"title": "validation-doc", "source": "errorta-validate"})
        check("ingest_record returned a ref", ref is not None)
        # give the async job a moment, then read health
        time.sleep(1.5)
        health = adapter.instance_health(TEST_INSTANCE)
        check("instance health has chunk_count", "chunk_count" in health,
              f"chunk_count={health.get('chunk_count')}")
        pub = adapter.publish(TEST_INSTANCE)
        check("publish succeeded", bool(pub),
              f"published={ (adapter.instance_health(TEST_INSTANCE) or {}).get('published') }")

        # --- A1 retrieve on the freshly-ingested instance (via Errorta B1) ---
        print("\nA1 pure retrieve (Errorta default-pipeline path)")
        hits = remote_aiar_retrieve(prompt="BLUEHERON marker phrase",
                                    corpus_ids=[TEST_INSTANCE], top_k=3)
        check("retrieve returned hits", len(hits) > 0, f"{len(hits)} hit(s)")
        if hits:
            h = hits[0]
            check("hit carries source provenance", bool(h.source))
            check("hit carries score", h.score is not None, f"score={h.score}")
            check("hit content non-empty", bool(h.content))

        # --- A4 answer + sources + /calls trace -----------------------------
        # F098: example-host is repointed to a pulled model (active_model_ready=true),
        # so we use the SERVER DEFAULT with NO per-request model override.
        print("\nA4 answer + include_sources + /calls (POST /services/prompt, server default)")
        body = {"service_name": "errorta-validate", "prompt": "What is the marker phrase?",
                "instance": TEST_INSTANCE, "rag": True, "judge": False, "think": False,
                "sources": True, "top_k": 3}
        try:
            resp = client.post(f"{base}/services/prompt", json=body, timeout=240.0)
            ans = resp.json()
            check("answer (server default) returns 200", resp.status_code == 200,
                  f"HTTP {resp.status_code}")
            check("answer schema_version aiar.answer.v1",
                  ans.get("schema_version") == "aiar.answer.v1",
                  f"schema_version={ans.get('schema_version')}")
            check("answer attaches sources (include_sources)",
                  isinstance(ans.get("sources"), list) and len(ans.get("sources")) > 0,
                  f"{len(ans.get('sources') or [])} source(s)")
            call_id = ans.get("call_id")
            if call_id:
                trace = client.get(f"{base}/calls/{call_id}")
                check("GET /calls/{id} trace reachable", trace.status_code == 200,
                      f"HTTP {trace.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            check("answer path (server default)", False, f"request error: {str(exc)[:80]}")

        # --- F098 negative: unpulled model -> structured 409 model_not_pulled ---
        # Non-mutating: a per-request override with a bogus model must be rejected
        # with the actionable error, NOT a raw ollama 503.
        print("\nF098 structured error (bogus model override -> 409 model_not_pulled)")
        try:
            bad = client.post(f"{base}/services/prompt", timeout=30.0, json={
                **body, "model": "definitely-not-pulled:0b"})
            bd = bad.json().get("detail") if bad.headers.get("content-type", "").startswith("application/json") else None
            code = bd.get("code") if isinstance(bd, dict) else None
            check("unpulled model -> 4xx model_not_pulled",
                  bad.status_code == 409 and code == "model_not_pulled",
                  f"HTTP {bad.status_code} code={code}")
        except httpx.HTTPError as exc:
            check("unpulled model structured error", False, str(exc)[:80])
    finally:
        # --- cleanup: delete the throwaway instance --------------------------
        print("\ncleanup")
        try:
            r = client.delete(f"{base}/instances/{TEST_INSTANCE}")
            check("throwaway instance deleted", r.status_code in (200, 202, 204),
                  f"HTTP {r.status_code}")
        except httpx.HTTPError as exc:
            check("throwaway instance deleted", False, str(exc)[:80])
        client.close()

    # --- summary -------------------------------------------------------------
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== {passed}/{len(RESULTS)} checks passed, {len(SKIPPED)} skipped ===")
    if SKIPPED:
        print("SKIPPED (environment, not AIAR):")
        for n, r in SKIPPED:
            print(f"  - {n}: {r}")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("\nAll AIAR v0.2.4/v0.2.5 contracts (incl. F098 active-model surfaces) work for Errorta.")
    if SKIPPED:
        print("(The answer/judge path was skipped only because example-host's Ollama "
              "has no model pulled — pull one to verify it too.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
