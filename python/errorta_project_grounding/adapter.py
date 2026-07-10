"""Errorta-side adapter contract for project grounding."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Protocol

from .capabilities import AiarGroundingCapabilities, probe_aiar_grounding_capabilities


class ProjectGroundingError(Exception):
    """Base class for project-grounding adapter failures."""


class UnsupportedGroundingOperation(ProjectGroundingError):
    """Raised when an operation is not safely supported by the active adapter."""


@dataclass(frozen=True)
class GroundingHit:
    content: str
    corpus_id: str
    chunk_id: str
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GroundingRecordRef:
    corpus_id: str
    record_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProjectGroundingAdapter(Protocol):
    def capabilities(self) -> AiarGroundingCapabilities:
        ...

    def ingest_file(
        self,
        *,
        corpus_id: str,
        path: Path,
        metadata: dict[str, Any],
    ) -> GroundingRecordRef:
        ...

    def ingest_record(
        self,
        *,
        corpus_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> GroundingRecordRef:
        ...

    def retrieve(
        self,
        *,
        corpus_id: str,
        query: str,
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[GroundingHit]:
        ...


class FallbackProjectGroundingAdapter:
    """No-AIAR adapter: reports capability state and fails closed."""

    def __init__(self, capabilities: AiarGroundingCapabilities | None = None) -> None:
        self._capabilities = capabilities or probe_aiar_grounding_capabilities()

    def capabilities(self) -> AiarGroundingCapabilities:
        return self._capabilities

    def ingest_file(
        self,
        *,
        corpus_id: str,
        path: Path,
        metadata: dict[str, Any],
    ) -> GroundingRecordRef:
        raise UnsupportedGroundingOperation("file ingest is unavailable without AIAR")

    def ingest_record(
        self,
        *,
        corpus_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> GroundingRecordRef:
        raise UnsupportedGroundingOperation("record ingest is handled by Errorta memory store")

    def retrieve(
        self,
        *,
        corpus_id: str,
        query: str,
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[GroundingHit]:
        raise UnsupportedGroundingOperation("semantic retrieval is unavailable without AIAR")


class AiarProjectGroundingAdapter:
    """Adapter over Errorta's existing AIAR seams."""

    def __init__(self, capabilities: AiarGroundingCapabilities | None = None) -> None:
        self._capabilities = capabilities or probe_aiar_grounding_capabilities()

    def capabilities(self) -> AiarGroundingCapabilities:
        return self._capabilities

    def ingest_file(
        self,
        *,
        corpus_id: str,
        path: Path,
        metadata: dict[str, Any],
    ) -> GroundingRecordRef:
        if not self._capabilities.supports_file_ingest:
            raise UnsupportedGroundingOperation("AIAR file ingest support was not detected")
        source = Path(path)
        if not source.is_file():
            raise ProjectGroundingError(f"not a file: {source}")
        from errorta_corpus import corpus_dir, validate_corpus_name
        from errorta_corpus.manifest import FileEntry, reserve_or_get_duplicate
        from errorta_corpus.pipeline import copied_path_for, enqueue, new_file_id
        from errorta_extract.registry import supported_extensions

        from .bootstrap import CODE_EXTENSIONS
        validate_corpus_name(corpus_id)
        if source.suffix.lower() not in (set(supported_extensions()) | set(CODE_EXTENSIONS)):
            raise ProjectGroundingError(f"unsupported file extension: {source.suffix}")
        target = copied_path_for(corpus_id, source.name)
        try:
            target.resolve().relative_to((corpus_dir(corpus_id) / "files").resolve())
        except (OSError, ValueError) as exc:
            raise ProjectGroundingError("invalid ingest target path") from exc
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        target.write_bytes(content)
        file_id = new_file_id()
        entry = FileEntry(
            file_id=file_id,
            original_path=str(source),
            copied_path=str(target),
            sha256=digest,
            size_bytes=source.stat().st_size,
            mime_ext=source.suffix.lower(),
            status="queued",
        )
        inserted, _prior = reserve_or_get_duplicate(corpus_id, digest, entry, overwrite=False)
        if inserted is None:
            target.unlink(missing_ok=True)
            return GroundingRecordRef(corpus_id=corpus_id, record_id=source.name, metadata=metadata)
        enqueue(corpus_id, file_id)
        return GroundingRecordRef(corpus_id=corpus_id, record_id=file_id, metadata=metadata)

    def ingest_record(
        self,
        *,
        corpus_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> GroundingRecordRef:
        if not self._capabilities.supports_record_ingest:
            raise UnsupportedGroundingOperation("generic AIAR record ingest is not available")
        raise UnsupportedGroundingOperation("AIAR record ingest adapter is not wired yet")

    def retrieve(
        self,
        *,
        corpus_id: str,
        query: str,
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[GroundingHit]:
        from errorta_query.pipeline import default_pipeline

        pipeline = default_pipeline()
        call_kwargs: dict[str, Any] = {
            "prompt": query, "corpus_ids": [corpus_id], "top_k": top_k,
        }
        if filters:
            # The capability probe inspects AIAR's store, but retrieval actually
            # runs through the errorta_query seam — if THAT call can't forward
            # the filters, a caller-supplied filter would be silently dropped
            # (a memory-pollution path). Forward only if the active seam's
            # query() accepts a filter kwarg; otherwise fail closed, regardless
            # of what the probe reported.
            from inspect import signature
            try:
                params = set(signature(pipeline.query).parameters)
            except (TypeError, ValueError):
                params = set()
            forward_key = next(
                (k for k in ("filters", "filter", "metadata_filter", "where")
                 if k in params), None)
            if forward_key is None:
                raise UnsupportedGroundingOperation(
                    "metadata filters are requested but the active retrieval "
                    "seam does not forward them")
            call_kwargs[forward_key] = filters

        raw = pipeline.query(**call_kwargs)
        hits: list[GroundingHit] = []
        for item in raw:
            item_metadata = getattr(item, "metadata", None)
            if not isinstance(item_metadata, dict):
                item_metadata = {}
            source = getattr(item, "source", None) or item_metadata.get("source")
            hits.append(
                GroundingHit(
                    content=item.content,
                    corpus_id=item.corpus_id,
                    chunk_id=item.chunk_id,
                    score=item.score,
                    metadata={
                        "citation_id": item.citation_id,
                        "tokens": item.tokens,
                        "source": source,
                    },
                )
            )
        return hits


def default_project_grounding_adapter() -> ProjectGroundingAdapter:
    # Remote AIAR (Errorta on the Mac, AIAR instance on another host): when
    # ERRORTA_AIAR_REMOTE_URL is configured the corpus is owned by the remote
    # AIAR, so route ingest/retrieve there. Lazy import avoids an import cycle
    # (remote_adapter imports this module). Unconfigured → unchanged behavior.
    from .remote_adapter import RemoteAiarCorpusAdapter, remote_aiar_config
    cfg = remote_aiar_config()
    if cfg is not None:
        return RemoteAiarCorpusAdapter(cfg)
    caps = probe_aiar_grounding_capabilities()
    if not caps.available:
        return FallbackProjectGroundingAdapter(caps)
    return AiarProjectGroundingAdapter(caps)
