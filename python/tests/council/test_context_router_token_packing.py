"""Deterministic priority + stable omitted reasons.

Priority order (F031-05 default):
  task_instructions → user_prompt → grounding_hint → snippets → transcript → summaries → metadata
"""
from __future__ import annotations

from errorta_council.context.packing import PackedContext, TokenPacker


def _block(class_, content, tokens):
    return {"class_": class_, "content": content, "tokens": tokens,
            "content_sha256": "a" * 64}


def test_priority_order_preserved():
    packer = TokenPacker(max_input_tokens=20)
    out: PackedContext = packer.pack([
        _block("transcript", "T", 4),
        _block("user_prompt", "U", 2),
        _block("task_instructions", "I", 4),
        _block("retrieved_snippet", "S", 8),
    ])
    classes = [m["class_"] for m in out.kept]
    assert classes.index("task_instructions") < classes.index("user_prompt")
    assert classes.index("user_prompt") < classes.index("retrieved_snippet")
    assert classes.index("retrieved_snippet") < classes.index("transcript")


def test_overflow_omits_lowest_priority_first_with_reason():
    packer = TokenPacker(max_input_tokens=10)
    out = packer.pack([
        _block("task_instructions", "I", 4),
        _block("user_prompt", "U", 2),
        _block("retrieved_snippet", "S", 8),
        _block("transcript", "T", 4),
    ])
    kept_classes = [m["class_"] for m in out.kept]
    assert "task_instructions" in kept_classes
    assert "user_prompt" in kept_classes
    omitted_classes = [o["class_"] for o in out.omitted]
    assert any(o == "transcript" for o in omitted_classes)
    assert all(o["reason"] == "token_cap" for o in out.omitted)


def test_stable_byte_output_for_identical_input():
    packer = TokenPacker(max_input_tokens=20)
    blocks = [
        _block("task_instructions", "I", 4),
        _block("user_prompt", "U", 2),
        _block("retrieved_snippet", "S", 8),
    ]
    a = packer.pack(list(blocks))
    b = packer.pack(list(blocks))
    assert a.kept == b.kept
    assert a.omitted == b.omitted
