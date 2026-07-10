#!/usr/bin/env python3
"""Deterministic F-DIST-01 validation harness (slice 9).

Exercises the whole alpha-delivery flow against an IN-PROCESS fake check-in
Worker — no network, no real ``api.errorta.app``. Run:

    python scripts/validate_f_dist_01.py

Exits 0 if every check passes, non-zero otherwise. The companion
``validate_f_dist_01_live.py`` hits a real staging Worker and is opt-in.

Checks: activate -> ACTIVE; sync drains floor+extras; offline-past-grace ->
EXPIRED; revoke -> REVOKED; soft EOL -> banner / required EOL -> lock; extras-off
sends only floor; crash breadcrumb is content-free; forged token -> UNACTIVATED;
feedback send; and the marquee "no content on the wire" assertion over every
heartbeat/metrics body.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

# Runnable as `python scripts/validate_f_dist_01.py` from the `python/` dir
# regardless of install mode: put the package root (python/) on the path so
# errorta_alpha/errorta_app import cleanly even without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="fdist-harness-"))
    os.environ["ERRORTA_HOME"] = str(home)
    os.environ["ERRORTA_ALPHA_GATE"] = "1"
    os.environ["ERRORTA_ALPHA_API"] = "https://fake.invalid"

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from errorta_alpha import client, config, feedback, state, telemetry
    from errorta_alpha import license as lic
    from errorta_alpha import token as token_mod

    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    pub_b64 = base64.b64encode(pub_raw).decode()
    os.environ["ERRORTA_ALPHA_PUBKEY"] = pub_b64

    CODE = "ERRT-7F3K-9Q2M"
    outbound: list[tuple[str, dict]] = []

    class Fake:
        hb = "active"
        required = False

        def mint(self, device_id: str) -> str:
            now = int(time.time())
            payload = {
                "v": 1, "device_id": device_id, "code": CODE, "issued_at": now,
                "grace_until": now + 14 * 86400, "program": "alpha", "build_channel": "alpha",
            }
            return token_mod.encode(payload, priv)

    fw = Fake()

    def fake_post_json(path, body):
        outbound.append((path, body))
        if path == "/v1/activate":
            return 200, {"status": "active", "token": fw.mint(body["device_id"]), "grace_days": 14}
        if path == "/v1/heartbeat":
            if fw.hb == "revoked":
                return 200, {"status": "revoked", "reason": "left program"}
            if fw.hb == "build_eol":
                return 200, {
                    "status": "build_eol", "required": fw.required,
                    "update_url": "https://errorta.app/download",
                    "token": fw.mint(body["device_id"]), "grace_days": 14,
                }
            return 200, {"status": "active", "token": fw.mint(body["device_id"]), "grace_days": 14}
        if path == "/v1/metrics":
            return 202, {}
        return 404, {}

    def fake_post_multipart(path, fields, files):
        outbound.append((path, {"fields": fields, "has_bundle": files is not None}))
        return 201, {"ticket_id": "tkt_harness"}

    client._post_json = fake_post_json
    client._post_multipart = fake_post_multipart

    results: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            fn()
            results.append((name, True, ""))
            print(f"  PASS  {name}")
        except AssertionError as exc:
            results.append((name, False, str(exc)))
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, repr(exc)))
            print(f"  ERROR {name}: {exc!r}")

    def c_activate():
        assert client.activate(CODE).ok, "activate should succeed"
        assert state.current_status().state.value == "active"

    def c_sync():
        telemetry.record_launch()
        telemetry.record_feature_used("judge_run")
        # activate stamped last_heartbeat=now, so advance past the 1h anti-spam
        # dedupe to model a real periodic check-in that actually sends.
        assert client.sync(now=int(time.time()) + 7200).kind == "active"
        assert telemetry.snapshot_queue() == [], "extras should drain"
        assert telemetry.snapshot_floor() == {}, "floor should clear"

    def c_expired():
        r = lic.load()
        st = state.current_status(now=r.grace_until + 10 * 86400)
        assert st.state.value == "expired" and st.locked, st

    def c_revoked():
        fw.hb = "revoked"
        client.heartbeat(force=True)
        st = state.current_status()
        assert st.state.value == "revoked" and st.locked
        fw.hb = "active"
        client.heartbeat(force=True)  # recover

    def c_eol():
        fw.hb, fw.required = "build_eol", False
        client.heartbeat(force=True)
        st = state.current_status()
        assert st.build_eol and not st.locked, "soft EOL: banner, not locked"
        fw.required = True
        client.heartbeat(force=True)
        assert state.current_status().locked, "required EOL: locked"
        fw.hb, fw.required = "active", False
        client.heartbeat(force=True)  # recover

    def c_extras_off():
        telemetry.set_extras_enabled(False)
        assert telemetry.record_feature_used("judge_run") is False
        before = len(outbound)
        client.sync(now=int(time.time()) + 7200)
        sent = outbound[before:]
        assert not any(p == "/v1/metrics" for p, _ in sent), "no metrics when extras off"
        telemetry.set_extras_enabled(True)

    def c_crash():
        os.environ["ERRORTA_ALPHA_PUBKEY"] = "AAAA"
        try:
            config.license_public_key_raw()
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            bc = feedback.build_crash_breadcrumb(exc)
        finally:
            os.environ["ERRORTA_ALPHA_PUBKEY"] = pub_b64
        assert "/" not in bc and ".py" not in bc, bc
        assert bc.startswith("ValueError@errorta"), bc

    def c_forged():
        r = lic.load()
        r.token = r.token[:-4] + ("AAAA" if not r.token.endswith("AAAA") else "BBBB")
        lic.store(r)
        assert state.current_status().state.value == "unactivated"
        assert client.activate(CODE).ok  # restore a valid license

    def c_feedback():
        res = client.send_feedback(kind="bug", message="harness note", bundle_path=None)
        assert res.ok and res.ticket_id == "tkt_harness"

    def c_no_content():
        # Every heartbeat/metrics body must carry no path-shaped text.
        for path, body in outbound:
            if path in ("/v1/heartbeat", "/v1/metrics"):
                blob = json.dumps(body)
                assert not re.search(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", blob), (path, blob)

    for name, fn in [
        ("activate -> ACTIVE", c_activate),
        ("sync drains floor + extras", c_sync),
        ("offline past grace -> EXPIRED locked", c_expired),
        ("revoke -> REVOKED locked", c_revoked),
        ("soft EOL banner / required EOL lock", c_eol),
        ("extras off -> only floor sent", c_extras_off),
        ("crash breadcrumb is content-free", c_crash),
        ("forged token -> UNACTIVATED", c_forged),
        ("feedback send returns ticket", c_feedback),
        ("no content on the wire (heartbeat/metrics)", c_no_content),
    ]:
        check(name, fn)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\nF-DIST-01 harness: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
