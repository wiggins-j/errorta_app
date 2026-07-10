"""AIAR project-grounding capability probe.

The probe is intentionally defensive: absence or API drift should produce a
typed report, not break Coding Mode startup.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from importlib import import_module, metadata
from inspect import signature
from types import ModuleType
from typing import Callable


Importer = Callable[[str], ModuleType]


@dataclass(frozen=True)
class AiarGroundingCapabilities:
    available: bool
    version: str | None
    source: str
    supports_corpus_ids: bool
    supports_file_ingest: bool
    supports_record_ingest: bool
    supports_metadata_filters: bool
    supports_provenance_metadata: bool
    supports_incremental_refresh: bool
    supports_supersession: bool
    supports_export_import: bool
    local_only_embedding: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        out["notes"] = list(self.notes)
        return out


def _version() -> str | None:
    for package in ("aiar-rag", "aiar"):
        try:
            return metadata.version(package)
        except metadata.PackageNotFoundError:
            continue
    return None


def _has_any(module: ModuleType | None, *names: str) -> bool:
    return bool(module and any(hasattr(module, name) for name in names))


def _has_filterable_query(module: ModuleType | None) -> bool:
    if module is None:
        return False
    for name in ("query", "search", "retrieve"):
        fn = getattr(module, name, None)
        if fn is None:
            continue
        try:
            params = set(signature(fn).parameters)
        except (TypeError, ValueError):
            continue
        if params & {"filters", "filter", "metadata_filter", "where"}:
            return True
    return False


def _try_import(importer: Importer, name: str) -> ModuleType | None:
    try:
        return importer(name)
    except Exception:
        return None


def probe_aiar_grounding_capabilities(
    *,
    importer: Importer = import_module,
) -> AiarGroundingCapabilities:
    """Return a best-effort report of AIAR's generic grounding primitives."""
    root = _try_import(importer, "aiar")
    if root is None:
        return AiarGroundingCapabilities(
            available=False,
            version=None,
            source="absent",
            supports_corpus_ids=False,
            supports_file_ingest=False,
            supports_record_ingest=False,
            supports_metadata_filters=False,
            supports_provenance_metadata=False,
            supports_incremental_refresh=False,
            supports_supersession=False,
            supports_export_import=False,
            local_only_embedding=False,
            notes=("aiar import failed",),
        )

    ingest = _try_import(importer, "aiar.rag.ingest")
    store = _try_import(importer, "aiar.rag.store")
    harness = _try_import(importer, "aiar.harness.pipeline")
    export_mod = _try_import(importer, "aiar.rag.export")

    supports_corpus_ids = _has_any(store, "create_instance", "set_active", "publish_instance")
    supports_file_ingest = _has_any(ingest, "ingest_chunks", "Chunk") or _has_any(store, "add")
    supports_incremental_refresh = _has_any(ingest, "evict_chunks", "refresh")
    supports_metadata_filters = _has_filterable_query(store)
    supports_export_import = _has_any(export_mod, "export", "import_", "import_bundle")

    notes: list[str] = []
    if harness is not None and hasattr(harness, "answer_prompt"):
        notes.append("retrieval available through answer_prompt adapter")
    if supports_file_ingest and not supports_metadata_filters:
        notes.append("file ingest available; metadata filter support not detected")
    if not supports_incremental_refresh:
        notes.append("incremental refresh/supersession require Errorta fallback")

    # Honest embedding-locality: the probe cannot prove runtime model routing,
    # but it CAN observe residency. Under remote residency the data plane (and
    # thus embedding) runs on the remote sidecar, so embedding is NOT local-only.
    # Only assert local-only when AIAR is present AND residency is local.
    local_only_embedding, residency_note = _embedding_is_local()
    if residency_note:
        notes.append(residency_note)

    return AiarGroundingCapabilities(
        available=True,
        version=getattr(root, "__version__", None) or _version(),
        source="installed",
        supports_corpus_ids=supports_corpus_ids,
        supports_file_ingest=supports_file_ingest,
        supports_record_ingest=_has_any(ingest, "ingest_record", "ingest_records"),
        supports_metadata_filters=supports_metadata_filters,
        supports_provenance_metadata=supports_file_ingest,
        supports_incremental_refresh=supports_incremental_refresh,
        supports_supersession=_has_any(ingest, "supersede", "tombstone"),
        supports_export_import=supports_export_import,
        local_only_embedding=local_only_embedding,
        notes=tuple(notes),
    )


def _embedding_is_local() -> tuple[bool, str]:
    """(local_only_embedding, note). False under remote residency — the probe
    won't claim local embedding when the data plane is demonstrably remote."""
    try:
        from errorta_residency import config as residency_config
        mode = getattr(residency_config.load(), "mode", "local")
    except Exception:
        return True, ""  # no residency module -> default local profile
    if mode and mode != "local":
        return False, f"embedding runs on the {mode} residency sidecar, not locally"
    return True, ""
