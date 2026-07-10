#!/usr/bin/env python3
"""Opt-in LIVE F-DIST-01 check against a real staging Worker (slice 9).

Unlike the deterministic harness, this hits a real ``api.errorta.app``-style
Worker and consumes a real invite code, so it is explicitly opt-in:

    ERRORTA_ALPHA_LIVE=1 \
    ERRORTA_ALPHA_API=https://staging.errorta.app \
    ERRORTA_ALPHA_PUBKEY=<staging public key, base64> \
    ERRORTA_ALPHA_TEST_CODE=ERRT-XXXX-XXXX \
    python scripts/validate_f_dist_01_live.py

Without ``ERRORTA_ALPHA_LIVE=1`` it prints how to run and exits 0 (skipped), so
it's safe to invoke from a checklist without a deployed Worker.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Runnable as `python scripts/validate_f_dist_01_live.py` from the `python/` dir
# regardless of install mode: put the package root (python/) on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    if os.environ.get("ERRORTA_ALPHA_LIVE") != "1":
        print("SKIP live F-DIST-01 check (no deployed Worker).")
        print("To run: set ERRORTA_ALPHA_LIVE=1, ERRORTA_ALPHA_API, ERRORTA_ALPHA_PUBKEY,")
        print("        ERRORTA_ALPHA_TEST_CODE, then re-run this script.")
        return 0

    api = os.environ.get("ERRORTA_ALPHA_API")
    code = os.environ.get("ERRORTA_ALPHA_TEST_CODE")
    if not api or not os.environ.get("ERRORTA_ALPHA_PUBKEY") or not code:
        print("ERROR: ERRORTA_ALPHA_API, ERRORTA_ALPHA_PUBKEY and ERRORTA_ALPHA_TEST_CODE "
              "are all required for the live check.")
        return 2

    os.environ["ERRORTA_HOME"] = str(Path(tempfile.mkdtemp(prefix="fdist-live-")))
    os.environ["ERRORTA_ALPHA_GATE"] = "1"

    from errorta_alpha import client, state

    print(f"Activating against {api} …")
    res = client.activate(code)
    if not res.ok:
        print(f"FAIL activate: {res.error_code} {res.message or ''}")
        return 1
    print("  activate OK")

    out = client.sync()
    print(f"  sync -> {out.kind}")

    st = state.current_status()
    ok = st.state.value == "active" and not st.locked
    print(f"  status -> {st.state.value} (locked={st.locked})")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
