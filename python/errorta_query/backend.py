"""F096 B4 / F116 — the single canonical AIAR backend resolver.

Errorta resolves "which AIAR?" on two sides:

* the **catalog / project-grounding** side (``_catalog_side``) reads
  ``errorta_project_grounding.remote_adapter.remote_aiar_config()``
  (``remote-aiar.json`` / ``ERRORTA_AIAR_REMOTE_URL`` — e.g. example-host), else
  follows the F-INFRA-12 residency config;
* the **retrieval** side (``_retrieval_target_side``) is where retrieval queries
  *actually* go: every ``query()`` in the data plane funnels through
  ``errorta_query.aiar_retrieve.remote_aiar_retrieve`` ->
  :func:`aiar_retrieval_target`, whose precedence is remote-AIAR first, then
  residency-remote, then local.

F116: the ``coordinated`` signal is computed from the SAME precedence the data
plane uses (``_retrieval_target_side`` / :func:`aiar_retrieval_target`), not from
residency alone. So a corpus listed from a remote AIAR and retrieved from that
same remote AIAR reports ``coordinated=True``, and the signal cannot drift from
the data plane (both read one helper). ``coordinated`` means "listing and
retrieval point at the same backend" — NOT that the backend is reachable or that
the corpus is published there.

F116 adds a canonical ``aiar-connection.json`` selector. This module reads that
selector first, then falls back to the legacy remote-AIAR and residency helpers.
It stays read-side only (no network), so ``/healthz`` can call
:func:`resolve_aiar_backend` per request without turning a health check into a
connectivity probe.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AiarBackend:
    """Resolved view of the catalog-side and retrieval-side AIAR backends.

    ``coordinated`` is True when both sides resolve to the same place (both local,
    or the same remote URL). It is the honest replacement for the prior
    ``kind != remote_aiar`` heuristic in ``/healthz``. Tokens are never stored on
    this descriptor — use :func:`aiar_retrieval_target` when a token is needed.
    """

    catalog_kind: str            # "remote_aiar" | "ssh-remote" | "cloud" | "local"
    catalog_base_url: str | None
    retrieval_kind: str          # "remote_aiar" | "ssh-remote" | "cloud" | "local"
    retrieval_base_url: str | None
    coordinated: bool


def _catalog_side() -> tuple[str, str | None]:
    """(kind, base_url) for the corpus catalog / project-grounding backend.

    Mirrors ``errorta_app.corpus_catalog.resolve_corpus_backend`` precedence: a
    configured remote-AIAR wins, else residency-remote (ssh-remote/cloud), else
    local. We return the residency *URL* (not just the mode) for the residency
    case so coordination compares like-for-like against the retrieval side — in
    residency-remote mode the catalog and retrieval are the SAME remote sidecar,
    so they must resolve coordinated.
    """
    try:
        from errorta_project_grounding.remote_adapter import remote_aiar_config
        cfg = remote_aiar_config()
    except Exception:  # pragma: no cover - defensive
        cfg = None
    if cfg is not None:
        return "remote_aiar", cfg.base_url
    # No explicit remote-AIAR: the catalog follows residency, same as retrieval.
    ret_kind, ret_url = _residency_side()
    if ret_kind != "local":
        return ret_kind, ret_url
    return "local", None


def _residency_side() -> tuple[str, str | None]:
    """(kind, base_url) for the F-INFRA-12 residency backend alone.

    The fallback both ``_catalog_side`` and ``_retrieval_target_side`` use when no
    explicit remote AIAR is configured.
    """
    try:
        from errorta_residency import config as residency_config
        state = residency_config.load()
    except Exception:  # pragma: no cover - defensive
        state = None
    if state is None:
        return "local", None
    mode = getattr(state, "mode", "local")
    if mode == "ssh-remote":
        port = getattr(state, "local_tunnel_port", None)
        return "ssh-remote", (f"http://127.0.0.1:{port}" if port else None)
    if mode == "cloud":
        return "cloud", getattr(state, "cloud_url", None)
    return "local", None


def _retrieval_target_side() -> tuple[str, str | None]:
    """(kind, base_url) for where retrieval queries ACTUALLY go.

    The single source of truth for retrieval-target precedence, shared by
    :func:`aiar_retrieval_target` (the data plane) and :func:`resolve_aiar_backend`
    (the ``/healthz`` signal) so the two cannot drift (F116). Precedence mirrors
    the data plane: a configured remote AIAR wins — it is the corpus host the user
    pointed at and the backend ``remote_aiar_retrieve`` hits — else residency
    remote (ssh-remote/cloud), else local.
    """
    try:
        from errorta_project_grounding.remote_adapter import remote_aiar_config
        cfg = remote_aiar_config()
    except Exception:  # pragma: no cover - defensive
        cfg = None
    if cfg is not None:
        return "remote_aiar", cfg.base_url
    return _residency_side()


def _canonical_target_side() -> tuple[str, str | None, str | None] | None:
    """(kind, base_url, token) from the F116 canonical config, if selected."""
    try:
        from errorta_aiar_connection.config import load_canonical

        cfg = load_canonical()
    except Exception:  # pragma: no cover - defensive
        cfg = None
    if cfg is None:
        return None
    if cfg.kind == "aiar-service" and cfg.base_url:
        return "remote_aiar", cfg.base_url, cfg.token
    if cfg.kind == "errorta-sidecar-remote" and cfg.base_url:
        residency_kind, residency_url = _residency_side()
        if residency_kind in {"ssh-remote", "cloud"} and residency_url == cfg.base_url:
            return residency_kind, cfg.base_url, cfg.token
        return "ssh-remote", cfg.base_url, cfg.token
    return None


def resolve_aiar_backend() -> AiarBackend:
    """Resolve the catalog-side and retrieval-side AIAR backends and whether they
    agree. Pure + cheap (no network) so ``/healthz`` can call it per request.

    The retrieval side is resolved via :func:`_retrieval_target_side` — the same
    precedence the data plane's :func:`aiar_retrieval_target` uses — so
    ``coordinated`` reflects where retrieval really routes, not residency alone
    (F116)."""
    canonical = _canonical_target_side()
    if canonical is not None:
        kind, base_url, _token = canonical
        return AiarBackend(
            catalog_kind=kind,
            catalog_base_url=base_url,
            retrieval_kind=kind,
            retrieval_base_url=base_url,
            coordinated=bool(base_url),
        )
    cat_kind, cat_url = _catalog_side()
    ret_kind, ret_url = _retrieval_target_side()
    coordinated = (cat_url or None) == (ret_url or None)
    return AiarBackend(
        catalog_kind=cat_kind,
        catalog_base_url=cat_url,
        retrieval_kind=ret_kind,
        retrieval_base_url=ret_url,
        coordinated=coordinated,
    )


def aiar_retrieval_target() -> tuple[str, str | None] | None:
    """``(base_url, token)`` for the resolved RETRIEVAL backend, or ``None`` for
    local. The seam F096 B1's ``RemoteHttpPipeline`` consumes so it never reads
    residency/remote config directly. The token is returned for request auth and
    MUST NOT be logged or placed in diagnostics.

    Target selection uses :func:`_retrieval_target_side` (remote AIAR first, then
    residency remote, then local); this function only attaches the auth token for
    the chosen backend.
    """
    canonical = _canonical_target_side()
    if canonical is not None:
        _kind, base_url, token = canonical
        return (base_url, token) if base_url else None

    kind, base_url = _retrieval_target_side()
    if kind == "local" or not base_url:
        return None
    if kind == "remote_aiar":
        try:
            from errorta_project_grounding.remote_adapter import remote_aiar_config
            cfg = remote_aiar_config()
        except Exception:  # pragma: no cover - defensive
            cfg = None
        return base_url, (cfg.token if cfg is not None else None)
    token: str | None = None
    if kind == "cloud":
        try:
            from errorta_residency import config as residency_config
            token = getattr(residency_config.load(), "cloud_token", None)
        except Exception:  # pragma: no cover - defensive
            token = None
    return base_url, token
