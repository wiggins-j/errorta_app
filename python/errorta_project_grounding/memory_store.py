"""Structured project memory store for F088.

This store is the authority/provenance gate before any later retrieval slice can
inject context into Coding Mode prompts.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from errorta_council.coding.ledger import LedgerStore

from . import paths as _paths


class ProjectMemoryError(Exception):
    """Base class for project-memory failures."""


class InvalidMemoryItem(ProjectMemoryError):
    """Raised when a memory item fails authority/provenance validation."""


VALID_AUTHORITIES = ("durable_truth", "wip", "claim", "external")
_AUTHORITY_RANK = {"durable_truth": 0, "wip": 1, "external": 2, "claim": 3}

# F088-04: durable truth is EVIDENCE-backed only. Only these source types may
# carry authority="durable_truth", and each must satisfy a per-type provenance
# rule (see _validate_durable) — raw put() can no longer smuggle a durable row
# with generic provenance.
_DURABLE_SOURCE_TYPES = frozenset({
    "pm_decision", "code_chunk", "doc_chunk", "test_evidence",
    "merge_episode", "reviewed_promotion", "pm_working_memory",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _load_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


@dataclass(frozen=True)
class MemorySourceRef:
    path: str | None = None
    commit: str | None = None
    task_id: str | None = None
    pr_id: str | None = None
    test_run_id: str | None = None
    corpus_id: str | None = None
    head: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def has_provenance(self) -> bool:
        return any(v for v in self.to_dict().values())


@dataclass(frozen=True)
class MemoryFreshness:
    indexed_at: str
    source_head: str | None = None
    index_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryVisibility:
    default_pm: bool = True
    default_dev: bool = True
    default_reviewer: bool = True
    default_tester: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def visible_to(self, role: str | None) -> bool:
        if not role:
            return True
        key = f"default_{role}"
        return bool(getattr(self, key, False))


@dataclass(frozen=True)
class MemoryItem:
    project_id: str
    authority: str
    source_type: str
    source_ref: MemorySourceRef
    content: str
    memory_id: str = ""
    summary: str | None = None
    source_ids: tuple[str, ...] = ()
    created_at: str = ""
    valid_from: str = ""
    valid_until: str | None = None
    superseded_by: str | None = None
    freshness: MemoryFreshness | None = None
    visibility: MemoryVisibility = field(default_factory=MemoryVisibility)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "project_id": self.project_id,
            "authority": self.authority,
            "source_type": self.source_type,
            "source_ref": self.source_ref.to_dict(),
            "content": self.content,
            "summary": self.summary,
            "source_ids": list(self.source_ids),
            "created_at": self.created_at,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "superseded_by": self.superseded_by,
            "freshness": (self.freshness or MemoryFreshness(indexed_at=self.created_at)).to_dict(),
            "visibility": self.visibility.to_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MemoryQuery:
    authorities: tuple[str, ...] | None = None
    source_type: str | None = None
    path: str | None = None
    symbol: str | None = None
    source_head: str | None = None
    corpus_id: str | None = None
    role: str | None = None
    include_claims: bool = False
    include_external: bool = False
    include_history: bool = False
    cross_project: bool = False
    limit: int = 50


def _parts(path: str | None) -> set[str]:
    if not path:
        return set()
    return {p for p in Path(path).parts if p}


def _reject_sensitive_path(path: str | None) -> None:
    # Single shared safe-index policy (paths.py) — same rule the bootstrap uses.
    if _paths.is_sensitive_path(path):
        raise InvalidMemoryItem("source path is denied or sensitive")


def _validate_durable(item: MemoryItem) -> None:
    """F088-04: durable truth is admitted by EVIDENCE, not by tone. A
    ``durable_truth`` row must use an allowlisted source type AND satisfy that
    type's provenance rule, so raw ``put()`` cannot promote arbitrary prose."""
    st = item.source_type
    if st not in _DURABLE_SOURCE_TYPES:
        raise InvalidMemoryItem(
            f"durable_truth requires an evidence-backed source_type "
            f"(one of {sorted(_DURABLE_SOURCE_TYPES)}), got {st!r}")
    ref = item.source_ref
    ok = True
    if st == "pm_decision":
        ok = bool(ref.task_id or item.metadata.get("decision_id"))
    elif st in ("code_chunk", "doc_chunk"):
        ok = bool(ref.path and (ref.commit or ref.head))
    elif st == "test_evidence":
        ok = bool(ref.test_run_id)
    elif st == "merge_episode":
        ok = bool(item.source_ids and (ref.pr_id or ref.head or ref.commit))
    elif st == "reviewed_promotion":
        ok = bool(ref.pr_id or ref.task_id)
    elif st == "pm_working_memory":
        ok = (
            ref.task_id == "pm"
            and item.metadata.get("schema_version") == "pm_working_memory.v1"
            and item.visibility.default_pm is True
            and item.visibility.default_dev is False
            and item.visibility.default_reviewer is False
            and item.visibility.default_tester is False
        )
    if not ok:
        raise InvalidMemoryItem(
            f"durable_truth source_type {st!r} is missing required provenance")


def _validate_item(item: MemoryItem) -> None:
    if not item.project_id:
        raise InvalidMemoryItem("project_id is required")
    if item.authority not in VALID_AUTHORITIES:
        raise InvalidMemoryItem(f"unknown authority: {item.authority}")
    if not item.source_type:
        raise InvalidMemoryItem("source_type is required")
    if not item.source_ref.has_provenance():
        raise InvalidMemoryItem("source_ref must include provenance")
    if not item.content.strip():
        raise InvalidMemoryItem("content is required")
    if len(item.content.encode("utf-8")) > _paths.MAX_MEMORY_CONTENT_BYTES:
        raise InvalidMemoryItem(
            f"content exceeds {_paths.MAX_MEMORY_CONTENT_BYTES} bytes")
    # Never index a secret, regardless of authority or filename.
    if _paths.content_has_secret(item.content) or _paths.content_has_secret(item.summary):
        raise InvalidMemoryItem("content contains a secret and cannot be indexed")
    _reject_sensitive_path(item.source_ref.path)
    derived = (item.source_type in ("derived_summary", "merge_episode")
               or bool(item.metadata.get("derived_summary")))
    if derived and item.summary and not item.source_ids:
        raise InvalidMemoryItem("derived summaries require source_ids")
    if item.authority == "external" and not item.metadata.get("external_scope"):
        raise InvalidMemoryItem("external memory requires explicit external_scope")
    if item.authority == "durable_truth":
        _validate_durable(item)


def _item_from_row(row: sqlite3.Row) -> MemoryItem:
    source_ref = MemorySourceRef(**_load_json(row["source_ref_json"], {}))
    freshness = MemoryFreshness(**_load_json(row["freshness_json"], {"indexed_at": row["created_at"]}))
    visibility = MemoryVisibility(**_load_json(row["visibility_json"], {}))
    return MemoryItem(
        memory_id=row["memory_id"],
        project_id=row["project_id"],
        authority=row["authority"],
        source_type=row["source_type"],
        source_ref=source_ref,
        content=row["content"],
        summary=row["summary"],
        source_ids=tuple(_load_json(row["source_ids_json"], [])),
        created_at=row["created_at"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        superseded_by=row["superseded_by"],
        freshness=freshness,
        visibility=visibility,
        metadata=_load_json(row["metadata_json"], {}),
    )


class ProjectMemoryStore:
    def __init__(self, project_id: str, *, root: Path | None = None) -> None:
        self.project_id = project_id
        self.ledger = LedgerStore(project_id, root=root)
        self.dir = self.ledger.dir / "grounding"
        self.path = self.dir / "memory.sqlite3"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                create table if not exists memory_schema(
                    version integer primary key
                )
                """
            )
            conn.execute(
                """
                create table if not exists memory_items(
                    memory_id text primary key,
                    project_id text not null,
                    authority text not null,
                    source_type text not null,
                    source_ref_json text not null,
                    content text not null,
                    summary text,
                    source_ids_json text not null,
                    created_at text not null,
                    valid_from text not null,
                    valid_until text,
                    superseded_by text,
                    freshness_json text not null,
                    visibility_json text not null,
                    metadata_json text not null
                )
                """
            )
            conn.execute("create index if not exists idx_memory_project on memory_items(project_id)")
            conn.execute(
                "create index if not exists idx_memory_project_authority "
                "on memory_items(project_id, authority)"
            )
            conn.execute(
                "create index if not exists idx_memory_source_type "
                "on memory_items(project_id, source_type)"
            )
            conn.execute(
                "insert or ignore into memory_schema(version) values (1)"
            )

    def put(self, item: MemoryItem) -> MemoryItem:
        if item.project_id != self.project_id:
            raise InvalidMemoryItem("memory item project_id does not match store")
        _validate_item(item)
        ts = _now()
        stored = item
        if not stored.memory_id:
            stored = replace(stored, memory_id=f"mem_{uuid.uuid4().hex}")
        if not stored.created_at:
            stored = replace(stored, created_at=ts)
        if not stored.valid_from:
            stored = replace(stored, valid_from=stored.created_at)
        if stored.freshness is None:
            stored = replace(stored, freshness=MemoryFreshness(indexed_at=stored.created_at))

        with self._connect() as conn:
            conn.execute(
                """
                insert or replace into memory_items(
                    memory_id, project_id, authority, source_type, source_ref_json,
                    content, summary, source_ids_json, created_at, valid_from,
                    valid_until, superseded_by, freshness_json, visibility_json,
                    metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.memory_id,
                    stored.project_id,
                    stored.authority,
                    stored.source_type,
                    _json(stored.source_ref.to_dict()),
                    stored.content,
                    stored.summary,
                    _json(list(stored.source_ids)),
                    stored.created_at,
                    stored.valid_from,
                    stored.valid_until,
                    stored.superseded_by,
                    _json(stored.freshness.to_dict()),
                    _json(stored.visibility.to_dict()),
                    _json(stored.metadata),
                ),
            )
        return stored

    # --- explicit admission API (F088-04/05) -------------------------------
    # Named entry points so callers state intent and authority is set by the
    # method, not by the caller. put() still enforces the evidence rules, so
    # these are ergonomics + a clear contract, not the security boundary.
    def _admit(self, *, authority: str, source_type: str, source_ref: MemorySourceRef,
               content: str, memory_id: str = "", summary: str | None = None,
               source_ids: tuple[str, ...] = (), head: str | None = None,
               metadata: dict[str, Any] | None = None,
               visibility: MemoryVisibility | None = None) -> MemoryItem:
        item = MemoryItem(
            project_id=self.project_id, authority=authority, source_type=source_type,
            source_ref=source_ref, content=content, memory_id=memory_id,
            summary=summary, source_ids=source_ids,
            freshness=(MemoryFreshness(indexed_at=_now(),
                                       source_head=(str(head) if head else None))
                       if head is not None else None),
            visibility=visibility or MemoryVisibility(),
            metadata=metadata or {},
        )
        return self.put(item)

    def admit_durable(self, *, source_type: str, source_ref: MemorySourceRef,
                      content: str, **kw: Any) -> MemoryItem:
        """Admit evidence-backed durable truth. Rejects non-allowlisted source
        types / missing provenance via put()'s validation."""
        return self._admit(authority="durable_truth", source_type=source_type,
                           source_ref=source_ref, content=content, **kw)

    def admit_wip(self, *, source_type: str, source_ref: MemorySourceRef,
                  content: str, **kw: Any) -> MemoryItem:
        return self._admit(authority="wip", source_type=source_type,
                           source_ref=source_ref, content=content, **kw)

    def admit_claim(self, *, source_type: str, source_ref: MemorySourceRef,
                    content: str, **kw: Any) -> MemoryItem:
        return self._admit(authority="claim", source_type=source_type,
                           source_ref=source_ref, content=content, **kw)

    def promote_wip_to_durable(self, memory_id: str, *, source_type: str,
                               source_ref: MemorySourceRef, content: str,
                               source_ids: tuple[str, ...] = (), **kw: Any) -> MemoryItem:
        """Promote a WIP item to durable truth once it is backed by merged
        evidence. The new durable record supersedes the WIP one (provenance
        preserved); promotion still passes the durable evidence gate."""
        durable = self.admit_durable(source_type=source_type, source_ref=source_ref,
                                     content=content, source_ids=source_ids, **kw)
        if self.get(memory_id) is not None:
            self.supersede(memory_id, superseded_by=durable.memory_id)
        return durable

    def get(self, memory_id: str) -> MemoryItem | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from memory_items where memory_id = ? and project_id = ?",
                (memory_id, self.project_id),
            ).fetchone()
        return _item_from_row(row) if row else None

    def supersede(
        self,
        memory_id: str,
        *,
        superseded_by: str | None = None,
        valid_until: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update memory_items
                set valid_until = ?, superseded_by = ?
                where memory_id = ? and project_id = ?
                """,
                (valid_until or _now(), superseded_by, memory_id, self.project_id),
            )

    def query(self, query: MemoryQuery | None = None) -> list[MemoryItem]:
        q = query or MemoryQuery()
        clauses: list[str] = []
        params: list[Any] = []
        if not q.cross_project:
            clauses.append("project_id = ?")
            params.append(self.project_id)
        authorities = q.authorities
        if authorities:
            clauses.append("authority in (%s)" % ",".join("?" for _ in authorities))
            params.extend(authorities)
        else:
            excluded = []
            if not q.include_claims:
                excluded.append("claim")
            if not q.include_external:
                excluded.append("external")
            if excluded:
                clauses.append("authority not in (%s)" % ",".join("?" for _ in excluded))
                params.extend(excluded)
        if q.source_type:
            clauses.append("source_type = ?")
            params.append(q.source_type)
        if not q.include_history:
            clauses.append("valid_until is null")
        sql = "select * from memory_items"
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by created_at desc"
        limit = max(1, min(int(q.limit), 500))

        out: list[MemoryItem] = []
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            try:
                item = _item_from_row(row)
            except Exception:
                continue
            if not _matches_structured_filters(item, q):
                continue
            out.append(item)
        out.sort(key=lambda item: item.created_at, reverse=True)
        out.sort(key=lambda item: _AUTHORITY_RANK.get(item.authority, 99))
        return out[:limit]


def _matches_structured_filters(item: MemoryItem, query: MemoryQuery) -> bool:
    ref = item.source_ref
    if query.path and ref.path != query.path:
        return False
    if query.symbol and item.metadata.get("symbol") != query.symbol:
        return False
    if query.source_head and (ref.head or item.freshness.source_head if item.freshness else None) != query.source_head:
        return False
    if query.corpus_id and ref.corpus_id != query.corpus_id:
        return False
    if query.role and not item.visibility.visible_to(query.role):
        return False
    if item.authority == "claim" and not query.include_claims and not query.authorities:
        return False
    if item.authority == "external" and not query.include_external and not query.authorities:
        return False
    return True
