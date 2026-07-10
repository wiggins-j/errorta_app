"""F063 B1 — local Ollama/onboarding probes degrade, never 500."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_ollama import detect


@pytest.mark.parametrize("host", [
    "http://nope.invalid.local:1",   # DNS failure
    "not-a-url",                      # malformed
    "http://127.0.0.1:1",            # connection refused
    "",                               # empty
])
def test_probe_never_raises_and_reports_unreachable(host):
    result = detect.probe(host, timeout=0.3)
    assert result.reachable is False
    # error is populated; the call returned a result instead of raising.
    assert result.host == host


def test_probe_survives_unexpected_exception(monkeypatch):
    # A non-httpx exception from the client must still degrade, not propagate.
    import httpx

    class _Boom:
        def __enter__(self):
            raise OSError("kernel said no")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Boom())
    result = detect.probe("http://x:1")
    assert result.reachable is False
    assert "kernel said no" in (result.error or "")


def test_ollama_health_returns_200_when_unreachable(tmp_errorta_home, monkeypatch):
    # Even if settings.load itself blows up, /ollama/health degrades to 200.
    from errorta_ollama import settings as settings_module
    monkeypatch.setattr(settings_module, "load",
                        lambda: (_ for _ in ()).throw(RuntimeError("settings boom")))
    client = TestClient(server_mod.app)
    r = client.get("/ollama/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is False


def test_onboarding_corpora_returns_200_when_listing_raises(tmp_errorta_home, monkeypatch):
    import errorta_corpus.listing as listing
    monkeypatch.setattr(listing, "list_corpora",
                        lambda: (_ for _ in ()).throw(OSError("disk gone")))
    client = TestClient(server_mod.app)
    r = client.get("/onboarding/corpora")
    assert r.status_code == 200, r.text
    assert r.json()["corpora"] == []
