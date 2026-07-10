#!/usr/bin/env bash
# F-DEMO-01 benchmark driver.
#
# Modes:
#   --fake   (default) Run the harness against the deterministic mock
#            client. No /healthz probe, no Ollama, no live judge — safe in
#            CI and on dev boxes without the sidecar running.
#   --real   Probe /healthz on the judge endpoint (default:
#            http://127.0.0.1:8770). Refuse to proceed unless reachable.
#            Then export ERRORTA_REAL_BENCHMARK=1 and invoke the runner
#            against /judge/verdict for real verdicts.
#
# Both modes pass --output-markdown docs/BENCHMARK.md through so the
# top-level report is regenerated. The fake banner is emitted by the
# Python renderer when real_run is false.
#
# Flags:
#   --fake              Force fake/mock mode (default).
#   --real              Require a healthy judge and run live verdict calls.
#   --seed <path>       Path to the seed YAML.
#   --judge-url <url>   Judge endpoint base URL (default: http://127.0.0.1:8770).
#   --re-run-paraphrase Also issue paraphrase re-runs for each prompt.
#   --output-markdown <path>  Override the Markdown output path.
#   --report-dir <path> Override the JSON report output directory.
#   --python <path>     Python interpreter to use (default: repo venv, then python3).
#   -h | --help         Print usage.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SEED=""
MODE="fake"
JUDGE_URL="${ERRORTA_JUDGE_URL:-http://127.0.0.1:8770}"
RE_RUN_PARAPHRASE=1
OUTPUT_MARKDOWN="$REPO_ROOT/docs/BENCHMARK.md"
REPORT_DIR=""
SIMULATE_CORRECTIONS=0
PYTHON_BIN="${PYTHON:-}"

usage() {
    sed -n '2,25p' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fake)              MODE="fake"; shift ;;
        --real)              MODE="real"; shift ;;
        --seed)              SEED="$2"; shift 2 ;;
        --judge-url)         JUDGE_URL="$2"; shift 2 ;;
        --re-run-paraphrase) RE_RUN_PARAPHRASE=1; shift ;;
        --no-paraphrase)     RE_RUN_PARAPHRASE=0; shift ;;
        --output-markdown)   OUTPUT_MARKDOWN="$2"; shift 2 ;;
        --report-dir)        REPORT_DIR="$2"; shift 2 ;;
        --python)            PYTHON_BIN="$2"; shift 2 ;;
        --simulate-corrections) SIMULATE_CORRECTIONS=1; shift ;;
        -h|--help)           usage; exit 0 ;;
        *) echo "unknown flag: $1" >&2; usage; exit 2 ;;
    esac
done

# Deterministic timestamp for placeholder/fake generation. Operators can
# override by exporting ERRORTA_BENCHMARK_NOW before invoking the script;
# otherwise the BENCH-FIRST baseline timestamp is used so docs/BENCHMARK.md
# stays byte-stable across runs.
export ERRORTA_BENCHMARK_NOW="${ERRORTA_BENCHMARK_NOW:-2026-06-08T00:00:00Z}"

resolve_python() {
    if [[ -n "$PYTHON_BIN" ]]; then
        if [[ -x "$PYTHON_BIN" ]] || command -v "$PYTHON_BIN" >/dev/null 2>&1; then
            printf '%s\n' "$PYTHON_BIN"
            return 0
        fi
        echo "python interpreter not found or not executable: $PYTHON_BIN" >&2
        return 1
    fi
    if [[ -x "$REPO_ROOT/python/.venv/bin/python" ]]; then
        printf '%s\n' "$REPO_ROOT/python/.venv/bin/python"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        printf '%s\n' "python3"
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        printf '%s\n' "python"
        return 0
    fi
    echo "no usable Python interpreter found" >&2
    return 1
}

probe_judge() {
    local url="${JUDGE_URL%/}/healthz"
    if command -v curl >/dev/null 2>&1; then
        curl -sf -o /dev/null --max-time 2 "$url"
        return $?
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=2).read()
except Exception:
    sys.exit(1)
PY
        return $?
    fi
    return 1
}

PY_ARGS=()
if [[ -n "$SEED" ]]; then
    PY_ARGS+=(--seed "$SEED")
fi
if [[ "$RE_RUN_PARAPHRASE" -eq 1 ]]; then
    PY_ARGS+=(--re-run-paraphrase)
fi
PY_ARGS+=(--output-markdown "$OUTPUT_MARKDOWN")
if [[ -n "$REPORT_DIR" ]]; then
    PY_ARGS+=(--report-dir "$REPORT_DIR")
fi
if [[ "$SIMULATE_CORRECTIONS" -eq 1 ]]; then
    PY_ARGS+=(--simulate-corrections)
fi
PY_ARGS+=(--judge-url "${JUDGE_URL%/}")

PYTHON_BIN="$(resolve_python)"
export PYTHONPATH="$REPO_ROOT/python${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$MODE" == "real" ]]; then
    if ! probe_judge; then
        echo "judge /healthz unreachable at ${JUDGE_URL%/} — refusing --real run" >&2
        exit 1
    fi
    export ERRORTA_REAL_BENCHMARK=1
    export ERRORTA_JUDGE_URL="${JUDGE_URL%/}"
    PY_ARGS+=(--real)
    exec "$PYTHON_BIN" -m errorta_benchmark "${PY_ARGS[@]}"
fi

# Fake mode — clear any inherited live-mode flag and proceed.
unset ERRORTA_REAL_BENCHMARK
PY_ARGS+=(--fake)
exec "$PYTHON_BIN" -m errorta_benchmark "${PY_ARGS[@]}"
