"""F096 B4 / F116 — the canonical AIAR backend resolver + honest coordination."""
from __future__ import annotations

from errorta_query import backend as be


class _Cfg:
    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url
        self.token = token


def _patch_remote_aiar(monkeypatch, cfg):
    monkeypatch.setattr(
        "errorta_project_grounding.remote_adapter.remote_aiar_config", lambda: cfg)


def _patch_residency(monkeypatch, state):
    # Patch the real residency config loader the resolver lazily imports.
    monkeypatch.setattr("errorta_residency.config.load", lambda: state)


def test_local_both_sides_is_coordinated(monkeypatch) -> None:
    _patch_remote_aiar(monkeypatch, None)
    _patch_residency(monkeypatch, None)
    b = be.resolve_aiar_backend()
    assert b.catalog_kind == "local" and b.retrieval_kind == "local"
    assert b.coordinated is True
    assert be.aiar_retrieval_target() is None


def test_remote_aiar_with_local_residency_is_coordinated(monkeypatch) -> None:
    # F116: corpora listed from a remote AIAR are ALSO retrieved from it — every
    # query() funnels through aiar_retrieval_target(), which prefers the remote
    # AIAR regardless of residency. So listing + retrieval resolve to the same
    # backend → coordinated (was the F095 gap, now closed in the signal too).
    _patch_remote_aiar(monkeypatch, _Cfg("http://example-host:8766", token="tok"))
    _patch_residency(monkeypatch, None)
    b = be.resolve_aiar_backend()
    assert b.catalog_kind == "remote_aiar"
    assert b.catalog_base_url == "http://example-host:8766"
    assert b.retrieval_kind == "remote_aiar"
    assert b.retrieval_base_url == "http://example-host:8766"
    assert b.coordinated is True


def test_remote_aiar_wins_over_ssh_residency(monkeypatch) -> None:
    import types
    _patch_remote_aiar(monkeypatch, _Cfg("http://example-host:8766", token="tok"))
    state = types.SimpleNamespace(mode="ssh-remote", local_tunnel_port=9001,
                                  cloud_url=None, cloud_token=None)
    _patch_residency(monkeypatch, state)
    b = be.resolve_aiar_backend()
    assert b.catalog_kind == "remote_aiar"
    assert b.catalog_base_url == "http://example-host:8766"
    assert b.retrieval_kind == "remote_aiar"
    assert b.retrieval_base_url == "http://example-host:8766"
    assert b.coordinated is True
    assert be.aiar_retrieval_target() == ("http://example-host:8766", "tok")


def test_divergent_catalog_and_retrieval_is_uncoordinated(monkeypatch) -> None:
    # Guard: resolve_aiar_backend reports False whenever the catalog and retrieval
    # sides genuinely resolve to different URLs. No real config produces this today
    # (both sides share one precedence helper), but a future change that diverges
    # them must surface as not-coordinated rather than silently read True.
    monkeypatch.setattr(be, "_catalog_side", lambda: ("remote_aiar", "http://a:8766"))
    monkeypatch.setattr(
        be, "_retrieval_target_side", lambda: ("remote_aiar", "http://b:8766"))
    b = be.resolve_aiar_backend()
    assert b.catalog_base_url == "http://a:8766"
    assert b.retrieval_base_url == "http://b:8766"
    assert b.coordinated is False


def test_retrieval_target_prefers_remote_aiar_and_carries_token(monkeypatch) -> None:
    _patch_remote_aiar(monkeypatch, _Cfg("http://example-host:8766", token="secret"))
    _patch_residency(monkeypatch, None)
    target = be.aiar_retrieval_target()
    assert target == ("http://example-host:8766", "secret")


def test_retrieval_target_falls_back_to_ssh_remote(monkeypatch) -> None:
    import types
    _patch_remote_aiar(monkeypatch, None)
    state = types.SimpleNamespace(mode="ssh-remote", local_tunnel_port=9001,
                                  cloud_url=None, cloud_token=None)
    _patch_residency(monkeypatch, state)
    b = be.resolve_aiar_backend()
    assert b.retrieval_kind == "ssh-remote"
    assert b.retrieval_base_url == "http://127.0.0.1:9001"
    assert be.aiar_retrieval_target() == ("http://127.0.0.1:9001", None)


def test_residency_remote_only_is_coordinated(monkeypatch) -> None:
    import types
    # No remote-AIAR; residency is ssh-remote. The corpus catalog follows
    # residency (fails closed to the same remote sidecar) and so does retrieval,
    # so both sides resolve to the same remote → coordinated. This preserves the
    # prior /healthz answer (residency-remote was coordinated under the old
    # ``kind != remote_aiar`` heuristic).
    _patch_remote_aiar(monkeypatch, None)
    state = types.SimpleNamespace(mode="ssh-remote", local_tunnel_port=9001,
                                  cloud_url=None, cloud_token=None)
    _patch_residency(monkeypatch, state)
    b = be.resolve_aiar_backend()
    assert b.catalog_kind == "ssh-remote" and b.retrieval_kind == "ssh-remote"
    assert b.coordinated is True


def test_remote_aiar_wins_over_cloud_residency(monkeypatch) -> None:
    import types
    _patch_remote_aiar(monkeypatch, _Cfg("http://same:8766"))
    state = types.SimpleNamespace(mode="cloud", local_tunnel_port=None,
                                  cloud_url="http://same:8766", cloud_token="t")
    _patch_residency(monkeypatch, state)
    b = be.resolve_aiar_backend()
    assert b.catalog_kind == "remote_aiar"
    assert b.retrieval_kind == "remote_aiar"
    assert b.retrieval_base_url == "http://same:8766"
    assert b.coordinated is True
