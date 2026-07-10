"""Phase 3 Council context-routing layer.

Re-exports the stable Phase 3 surface. Modules under this package own:
- visibility.py     — TranscriptVisibilityResolver (pure function)
- policy.py         — EffectiveContextPolicy (most-restrictive combination)
- retrieval.py      — RetrievalSeam adapter over errorta_query
- packing.py        — token-budget packer
- router.py         — ContextRouter top-level orchestrator
- manifest_store.py — ContextManifestStore persistence
- inspection.py     — read-only view models for F031-08
- transforms/       — RedactionPipeline + SummaryPipeline + TransformStore
"""
from __future__ import annotations
