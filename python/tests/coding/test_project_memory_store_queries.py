from pathlib import Path

from errorta_project_grounding.memory_store import (
    MemoryItem,
    MemoryQuery,
    MemorySourceRef,
    MemoryVisibility,
    ProjectMemoryStore,
)


def _put(
    store: ProjectMemoryStore,
    *,
    authority: str,
    content: str,
    path: str = "src/app.py",
    **metadata,
) -> MemoryItem:
    return store.put(
        MemoryItem(
            project_id=store.project_id,
            authority=authority,
            source_type=metadata.pop("source_type", "pm_decision"),
            source_ref=MemorySourceRef(path=path, task_id="t1", corpus_id=metadata.pop("corpus_id", None)),
            content=content,
            metadata=metadata,
        )
    )


def test_default_query_excludes_claims_and_external(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    _put(store, authority="durable_truth", content="merged fact")
    _put(store, authority="wip", content="open branch fact")
    _put(store, authority="claim", content="raw model claim")
    store.put(
        MemoryItem(
            project_id="p",
            authority="external",
            source_type="external_doc",
            source_ref=MemorySourceRef(corpus_id="external"),
            content="external fact",
            metadata={"external_scope": "explicit"},
        )
    )

    got = [m.content for m in store.query()]

    assert got == ["merged fact", "open branch fact"]


def test_include_claims_surfaces_audit_claims(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    _put(store, authority="durable_truth", content="merged fact")
    _put(store, authority="claim", content="raw model claim")

    got = [m.content for m in store.query(MemoryQuery(include_claims=True))]

    assert got == ["merged fact", "raw model claim"]


def test_default_limit_is_applied_after_authority_ranking(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    _put(store, authority="durable_truth", content="older durable")
    for idx in range(20):
        _put(store, authority="wip", content=f"newer wip {idx}")

    got = [m.content for m in store.query(MemoryQuery(limit=1))]

    assert got == ["older durable"]


def test_cross_project_isolation_by_default(tmp_path: Path) -> None:
    p1 = ProjectMemoryStore("p1", root=tmp_path)
    p2 = ProjectMemoryStore("p2", root=tmp_path)
    _put(p1, authority="durable_truth", content="p1 fact")
    _put(p2, authority="durable_truth", content="p2 fact")

    assert [m.content for m in p1.query()] == ["p1 fact"]


def test_query_filters_path_symbol_corpus_and_visibility(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    _put(store, authority="durable_truth", content="parser", path="src/parser.py", symbol="parse")
    _put(store, authority="durable_truth", content="ui", path="src/ui.ts", symbol="render")
    hidden = MemoryItem(
        project_id="p",
        authority="durable_truth",
        source_type="pm_decision",
        source_ref=MemorySourceRef(path="src/secret.py", corpus_id="main", task_id="t1"),
        content="pm only",
        visibility=MemoryVisibility(default_dev=False),
    )
    store.put(hidden)

    assert [m.content for m in store.query(MemoryQuery(path="src/parser.py"))] == ["parser"]
    assert [m.content for m in store.query(MemoryQuery(symbol="render"))] == ["ui"]
    assert "pm only" not in [m.content for m in store.query(MemoryQuery(role="dev"))]


def test_superseded_items_are_hidden_unless_history_requested(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    saved = _put(store, authority="durable_truth", content="old")
    _put(store, authority="durable_truth", content="new")
    store.supersede(saved.memory_id)

    assert [m.content for m in store.query()] == ["new"]
    assert {m.content for m in store.query(MemoryQuery(include_history=True))} == {"old", "new"}
