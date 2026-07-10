"""Excluded source classes are dropped BEFORE string redaction.

F031-07 §Source exclusion: raw source files, full credentials, raw
diagnostic notes, raw outbound payloads, transcript events beyond cursor
are all excluded — the pipeline never reads excluded stores.
"""
from __future__ import annotations

import hashlib

from errorta_council.context.transforms.redaction import REDACTION_VERSION, RedactionPipeline
from errorta_council.context.transforms.schema import SourceEnvelope


def _env(content, *, class_, sensitivity="known_local"):
    return SourceEnvelope(
        class_=class_, corpus_id=None, chunk_id=None, citation_id=None,
        content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        tokens=2, sensitivity=sensitivity)


def test_raw_source_file_class_excluded():
    pipe = RedactionPipeline(version=REDACTION_VERSION)
    envs = [
        _env("retrieved snippet text", class_="retrieved_snippet"),
        _env("THE WHOLE FILE CONTENTS THAT WERE NEVER SUPPOSED TO LEAVE", class_="raw_source_file"),
    ]
    kept, dropped = pipe.exclude_disallowed_classes(envs, destination_scope="remote")
    assert len(kept) == 1
    assert kept[0].class_ == "retrieved_snippet"
    assert any(d["reason"] == "source_class_excluded" and d["class_"] == "raw_source_file"
               for d in dropped)


def test_raw_credentials_class_excluded():
    pipe = RedactionPipeline(version=REDACTION_VERSION)
    envs = [_env("AWS_SECRET_ACCESS_KEY=AKIA1234567890", class_="raw_credentials")]
    kept, dropped = pipe.exclude_disallowed_classes(envs, destination_scope="local")
    assert kept == []
    assert dropped[0]["reason"] == "source_class_excluded"
