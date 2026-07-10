"""Tests for ``errorta_query.pipeline.default_pipeline`` residency dispatch.

F-INFRA-12 Phase B Slice 8: ``default_pipeline()`` consults the residency
config on every call and returns a ``RemoteHttpPipeline`` when mode is
``ssh-remote`` (with a configured tunnel port). All other states — including
cloud (deferred until token auth ships), half-applied or
deliberately broken residency files — fall through to the existing
local AIAR-or-Stub selection (no exceptions surface to the caller).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_query.pipeline import default_pipeline
from errorta_query.remote_pipeline import RemoteHttpPipeline

# ---------------------------------------------------------------------------
# Local mode → unchanged AIAR/Stub selection
# ---------------------------------------------------------------------------


def test_local_mode_falls_through_to_local_pipeline(
    tmp_errorta_home: Path,
) -> None:
    """Default residency (mode=local, no file) keeps the F001-SEAM path.

    The returned object is *not* a RemoteHttpPipeline. We don't care
    here whether AIAR is importable in this env (CI may or may not
    have it) — only that the remote dispatch did not fire.
    """
    p = default_pipeline()
    assert not isinstance(p, RemoteHttpPipeline)


def test_aiar_service_runtime_returns_service_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_errorta_home: Path,
) -> None:
    import errorta_aiar_connection
    from errorta_aiar_connection.models import AiarCapabilities, AiarRuntime
    from errorta_query.aiar_service_pipeline import AiarServicePipeline

    runtime = AiarRuntime(
        kind="aiar-service",
        display_name="example-host",
        connected=True,
        base_url="http://example-host.local:8766",
        token="secret",
        capabilities=AiarCapabilities(answer=True, judge=True),
    )
    monkeypatch.setattr(errorta_aiar_connection, "resolve_aiar_runtime", lambda: runtime)

    p = default_pipeline()

    assert isinstance(p, AiarServicePipeline)
    assert p.base_url == "http://example-host.local:8766"


def test_canonical_disconnected_runtime_does_not_use_stub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_errorta_home: Path,
) -> None:
    import errorta_aiar_connection
    from errorta_aiar_connection.models import AiarRuntime

    runtime = AiarRuntime(
        kind="disconnected",
        display_name="AIAR disconnected",
        connected=False,
        config_source="canonical",
        error_code="aiar_disconnected",
    )
    monkeypatch.setattr(errorta_aiar_connection, "resolve_aiar_runtime", lambda: runtime)

    p = default_pipeline()
    result = p.answer(
        prompt="hello",
        corpus="default",
        judge=True,
        reground=True,
        model=None,
    )

    assert result.aiar is False
    assert result.verdict is not None
    assert result.verdict.failure_tags == ["aiar_disconnected"]
    assert "development answer" not in result.answer


def test_canonical_local_aiar_overrides_stale_residency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_errorta_home: Path,
) -> None:
    """An explicit canonical 'local-aiar' choice is authoritative: even with a
    stale ssh-remote residency config present, default_pipeline must NOT route to
    the remote sidecar. Locks the F116-review fix for the missing local-aiar
    branch."""
    import errorta_aiar_connection
    from errorta_aiar_connection.models import AiarRuntime
    from errorta_residency import config as residency_config

    # Stale residency that WOULD route remote if the local-aiar choice fell through.
    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=12345,
        local_tunnel_port=54321,
    )
    runtime = AiarRuntime(
        kind="local-aiar",
        display_name="Local AIAR",
        connected=True,
        config_source="canonical",
    )
    monkeypatch.setattr(errorta_aiar_connection, "resolve_aiar_runtime", lambda: runtime)

    p = default_pipeline()

    assert not isinstance(p, RemoteHttpPipeline)


# ---------------------------------------------------------------------------
# ssh-remote dispatch
# ---------------------------------------------------------------------------


def test_ssh_remote_with_port_returns_remote_pipeline(
    tmp_errorta_home: Path,
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=12345,
        local_tunnel_port=54321,
    )

    p = default_pipeline()
    assert isinstance(p, RemoteHttpPipeline)
    assert p.base_url == "http://127.0.0.1:54321"


def test_ssh_remote_with_only_remote_sidecar_port_falls_through_to_local(
    tmp_errorta_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=12345,
    )

    with caplog.at_level("WARNING", logger="errorta_query.pipeline"):
        p = default_pipeline()

    assert not isinstance(p, RemoteHttpPipeline)


def test_ssh_remote_without_port_falls_through_to_local(
    tmp_errorta_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Half-applied state must not break the judge surface."""
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=None,
    )

    with caplog.at_level("WARNING", logger="errorta_query.pipeline"):
        p = default_pipeline()
    assert not isinstance(p, RemoteHttpPipeline)


# ---------------------------------------------------------------------------
# cloud dispatch
# ---------------------------------------------------------------------------


def test_cloud_with_url_returns_remote_pipeline(
    tmp_errorta_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="cloud",
        cloud_url="https://errorta.example.com",
    )

    with caplog.at_level("WARNING", logger="errorta_query.pipeline"):
        p = default_pipeline()
    assert isinstance(p, RemoteHttpPipeline)
    assert p.base_url == "https://errorta.example.com"


def test_cloud_without_url_falls_through_to_local(
    tmp_errorta_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Persisted residency rejects mode=cloud without a URL (see config._validate),
    so we have to hand-write a half-applied JSON file to drive this branch.
    """
    from errorta_app.paths import data_residency_path

    data_residency_path().write_text(
        '{"mode": "cloud", "cloud_url": null, "ssh_port": 22}'
    )

    with caplog.at_level("WARNING", logger="errorta_query.pipeline"):
        p = default_pipeline()
    assert not isinstance(p, RemoteHttpPipeline)


# ---------------------------------------------------------------------------
# Broken residency file → fall through, do not raise
# ---------------------------------------------------------------------------


def test_broken_residency_file_falls_through_silently(
    tmp_errorta_home: Path,
) -> None:
    """A garbage JSON file in ~/.errorta must not break default_pipeline().

    ``residency_config.load()`` already coerces malformed JSON to a
    default ``ResidencyState(mode="local")``, so the broken-file path
    routes through the same code as the explicit-local case.
    """
    from errorta_app.paths import data_residency_path

    data_residency_path().write_text("{not valid json")

    p = default_pipeline()
    assert not isinstance(p, RemoteHttpPipeline)


def test_no_residency_module_falls_through(
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If errorta_residency is briefly unimportable, the local path still works.

    Simulate by making the lazy import inside default_pipeline raise.
    """
    import builtins

    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "errorta_residency.config" or name == "errorta_residency":
            raise ImportError("simulated missing module")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)

    p = default_pipeline()
    assert not isinstance(p, RemoteHttpPipeline)


# ---------------------------------------------------------------------------
# No caching: pipeline reflects the latest residency state on every call
# ---------------------------------------------------------------------------


def test_pipeline_re_reads_residency_each_call(
    tmp_errorta_home: Path,
) -> None:
    """Switching mode in Settings becomes effective on the next judge call."""
    from errorta_residency import config as residency_config

    p1 = default_pipeline()
    assert not isinstance(p1, RemoteHttpPipeline)

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=18770,
        local_tunnel_port=28770,
    )
    p2 = default_pipeline()
    assert isinstance(p2, RemoteHttpPipeline)
    assert p2.base_url == "http://127.0.0.1:28770"

    residency_config.update(mode="local", remote_sidecar_port=None, local_tunnel_port=None)
    p3 = default_pipeline()
    assert not isinstance(p3, RemoteHttpPipeline)
