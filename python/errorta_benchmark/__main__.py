"""F-DEMO-01 benchmark CLI entry point.

Usage:

    python -m errorta_benchmark [--seed PATH] [--report-dir PATH]

Default behaviour:
    * Seed YAML resolves to the in-tree expanded ``seeds/welcome_v1.yaml``.
    * The client is selected via :func:`errorta_benchmark.runner._client_for_mode`,
      which honours the ``ERRORTA_REAL_BENCHMARK`` environment variable.
    * Output JSON lands under ``errorta_benchmark/reports/`` with a stable
      file name; the timestamp is taken from ``ERRORTA_BENCHMARK_NOW`` when
      set so the harness stays reproducible in CI.

This entry point is deliberately tiny — it composes the public helpers in
:mod:`errorta_benchmark.runner`, :mod:`.aggregator` and :mod:`.prompts`
and writes JSON. No new product behaviour lives here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .aggregator import BenchmarkAggregator
from .prompts import load_prompts_yaml
from .report import render_markdown
from .runner import BenchmarkRunner, RecordedVerdict, _base_url_for_env, orchestrate_run


def _judge_model_from_verdicts(verdicts: list[RecordedVerdict]) -> str | None:
    for verdict in verdicts:
        raw = verdict.raw or {}
        for key in ("judge_model", "default_judge_model", "model"):
            value = raw.get(key)
            if value:
                return str(value)
    return None


def _probe_run_metadata(
    base_url: str | None = None,
    *,
    verdicts: list[RecordedVerdict] | None = None,
) -> dict[str, str]:
    """F-DEMO-01 Slice (b) provenance probe.

    Best-effort GET against the sidecar /healthz, /judge/model, and an
    Ollama version probe. Failures are silent — the renderer simply omits
    the field. This runs only in REAL mode so it never adds latency to
    fake-mode runs.
    """
    out: dict[str, str] = {}
    resolved_base_url = (base_url or _base_url_for_env()).rstrip("/")
    try:  # pragma: no cover - network probe; covered by unit fixtures
        import httpx  # local import keeps fake-mode runs dependency-light

        health = httpx.get(
            f"{resolved_base_url}/healthz", timeout=2.0
        ).json()
        aiar_pin = health.get("aiar_pin") or {}
        src = aiar_pin.get("source")
        if src:
            out["aiar_pin_source"] = str(src)
        # Judge model surfaces under different keys across versions; try
        # the documented ones in order.
        for key in ("judge_model", "default_judge_model", "model"):
            jm = health.get(key)
            if jm:
                out["judge_model"] = str(jm)
                break
        if "judge_model" not in out:
            model_body = httpx.get(
                f"{resolved_base_url}/judge/model", timeout=2.0
            ).json()
            jm = model_body.get("judge_model")
            if jm:
                out["judge_model"] = str(jm)
    except Exception:  # pragma: no cover - probe is advisory
        pass
    if "judge_model" not in out and verdicts:
        jm = _judge_model_from_verdicts(verdicts)
        if jm:
            out["judge_model"] = jm
    try:  # pragma: no cover - Ollama probe; advisory only
        import httpx

        ov = httpx.get("http://127.0.0.1:11434/api/version", timeout=2.0).json()
        v = ov.get("version")
        if v:
            out["ollama_version"] = str(v)
    except Exception:  # pragma: no cover
        pass
    return out


def _seed_sha256(seed_path: Path) -> str:
    """Provenance hash of the seed YAML — used for the Run metadata header."""
    return hashlib.sha256(seed_path.read_bytes()).hexdigest()


_PKG_DIR = Path(__file__).resolve().parent
_DEFAULT_SEED = _PKG_DIR / "seeds" / "welcome_v1.yaml"
_DEFAULT_REPORT_DIR = _PKG_DIR / "reports"


def _now_iso() -> str:
    override = os.environ.get("ERRORTA_BENCHMARK_NOW", "").strip()
    if override:
        return override
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="errorta_benchmark")
    parser.add_argument("--seed", type=Path, default=_DEFAULT_SEED)
    parser.add_argument("--report-dir", type=Path, default=_DEFAULT_REPORT_DIR)
    parser.add_argument(
        "--re-run-paraphrase",
        action="store_true",
        help="Also issue paraphrase re-runs for each prompt (F024 delta).",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=None,
        help="When set, write the rendered Markdown report to this path.",
    )
    parser.add_argument(
        "--simulate-corrections",
        action="store_true",
        help=(
            "FAKE mode only — run a before/after pair so before_after_delta "
            "is numeric. Ignored in REAL mode."
        ),
    )
    parser.add_argument(
        "--judge-url",
        default=_base_url_for_env(),
        help=(
            "Sidecar base URL for real-mode runs. Defaults to ERRORTA_JUDGE_URL, "
            "then ERRORTA_SIDECAR_PORT, then http://127.0.0.1:8770."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fake",
        dest="fake",
        action="store_true",
        help="Force fake/mock mode (deterministic, no live judge).",
    )
    mode.add_argument(
        "--real",
        dest="real",
        action="store_true",
        help="Require ERRORTA_REAL_BENCHMARK=1 and a real judge.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])

    if not args.seed.exists():
        # Fall back to the original 10-entry file shipped with the harness.
        legacy = _PKG_DIR / "prompts" / "welcome_v1.yaml"
        if legacy.exists():
            seed_path = legacy
        else:
            print(f"seed file not found: {args.seed}", file=sys.stderr)
            return 2
    else:
        seed_path = args.seed

    prompts = load_prompts_yaml(seed_path)

    # Mode reconciliation. --fake forces mock mode by clearing the env var.
    # --real refuses to proceed unless ERRORTA_REAL_BENCHMARK=1 is set by
    # the caller (typically scripts/run-benchmark.sh after a /healthz probe).
    if args.fake:
        os.environ.pop("ERRORTA_REAL_BENCHMARK", None)
    if args.real and os.environ.get("ERRORTA_REAL_BENCHMARK", "").lower() != "1":
        print(
            "--real requires ERRORTA_REAL_BENCHMARK=1 in the environment",
            file=sys.stderr,
        )
        return 2

    real_run = os.environ.get("ERRORTA_REAL_BENCHMARK", "").lower() == "1"

    before_verdicts: list = []
    after_verdicts: list = []
    if args.simulate_corrections and not real_run:
        runner = BenchmarkRunner()
        before_verdicts, after_verdicts = runner.orchestrate_run_with_before_after(
            prompts, simulate_corrections=True
        )
        # The 'after' verdicts are the canonical headline numbers since
        # they represent the post-correction state the wedge story sells.
        verdicts = after_verdicts
        aggregation = BenchmarkAggregator().aggregate(
            verdicts, before=before_verdicts, after=after_verdicts
        )
    else:
        verdicts = orchestrate_run(
            prompts,
            re_run_paraphrase=args.re_run_paraphrase,
            base_url=str(args.judge_url),
        )
        aggregation = BenchmarkAggregator().aggregate(verdicts)

    report_dir: Path = args.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    generated_at = _now_iso()
    run_metadata: dict[str, object] = {
        "seed": seed_path.name,
        "seed_prompt_count": len(prompts),
        "notes": (
            "real_run=true (live judge)" if real_run
            else "real_run=false (deterministic mock client)"
        ),
        # F-DEMO-01 Slice (b) provenance — seed_sha256 is cheap and
        # always lands. The /healthz + Ollama probe only fires in
        # real-mode to avoid touching the network from fake runs.
        "seed_sha256": _seed_sha256(seed_path),
    }
    if real_run:
        run_metadata.update(
            _probe_run_metadata(str(args.judge_url), verdicts=verdicts)
        )

    report = {
        "seed": str(seed_path),
        "generated_at": generated_at,
        "real_run": real_run,
        "run_metadata": run_metadata,
        "verdicts": [asdict(v) for v in verdicts],
        "aggregation": asdict(aggregation),
    }

    out_path = report_dir / "latest.json"
    out_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"benchmark report written: {out_path}")

    if args.output_markdown is not None:
        md_path: Path = args.output_markdown
        md_path.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, object] = {
            "title": "Errorta benchmark report — welcome_v1",
            "generated_at": generated_at,
            **run_metadata,
        }
        md = render_markdown(aggregation, meta, is_fake_run=not real_run)
        md_path.write_text(md, encoding="utf-8")
        print(f"benchmark markdown written: {md_path}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
