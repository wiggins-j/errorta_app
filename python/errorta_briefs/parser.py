"""Markdown front-matter parser for briefs.

Briefs are markdown files with YAML front-matter delimited by `---` lines:

    ---
    project: Aerospace Mini
    corpus: aerospace-mini
    ...
    ---

    Body markdown here.

`parse_brief_markdown` splits the two parts, validates the front-matter against
`BriefConfig`, and returns `(config, body_markdown)`. All failures raise
`BriefParseError` with structured field-level diagnostics so the frontend can
surface them inline.
"""
from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError

from errorta_briefs.schema import BriefConfig


class BriefParseError(ValueError):
    """Raised when a brief cannot be parsed or fails schema validation.

    Attributes
    ----------
    message:
        Human-readable summary of what went wrong.
    errors:
        List of field-level diagnostics. Each entry is a dict with at least
        ``loc`` (tuple/list path to the field, or ``("__root__",)`` for
        document-level issues) and ``msg``. Validation errors also include
        ``type`` mirroring Pydantic's error type tag.
    """

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors: list[dict[str, Any]] = errors or []

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.errors:
            return self.message
        diag = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ('__root__',)))}: {e.get('msg', '')}"
            for e in self.errors
        )
        return f"{self.message} [{diag}]"


def _split_front_matter(text: str) -> tuple[str, str]:
    """Split YAML front-matter from the body.

    The document MUST start with a line consisting of exactly ``---`` and contain
    a second ``---`` line that closes the block. Anything after the closing line
    is the body.
    """
    # Normalize line endings; preserve content otherwise.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    # First non-empty line must be ---
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        raise BriefParseError(
            "Brief is missing the opening YAML front-matter delimiter ('---').",
            errors=[
                {
                    "loc": ("__root__",),
                    "msg": "expected '---' on the first non-empty line",
                    "type": "front_matter.missing_open",
                }
            ],
        )

    start = idx + 1
    # Find the closing ---
    close = None
    for i in range(start, len(lines)):
        if lines[i].strip() == "---":
            close = i
            break
    if close is None:
        raise BriefParseError(
            "Brief is missing the closing YAML front-matter delimiter ('---').",
            errors=[
                {
                    "loc": ("__root__",),
                    "msg": "expected a second '---' line to close the front-matter block",
                    "type": "front_matter.missing_close",
                }
            ],
        )

    yaml_block = "\n".join(lines[start:close])
    body = "\n".join(lines[close + 1 :]).lstrip("\n")
    return yaml_block, body


def parse_brief_markdown(text: str) -> tuple[BriefConfig, str]:
    """Parse a brief markdown document.

    Returns a tuple of ``(BriefConfig, body_markdown)``. Raises
    :class:`BriefParseError` with structured field-level diagnostics if the
    front-matter is missing, the YAML is malformed, or the data fails schema
    validation.
    """
    yaml_block, body = _split_front_matter(text)

    try:
        raw = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        # Try to surface line/column info if PyYAML attached a mark.
        mark = getattr(exc, "problem_mark", None)
        loc: tuple[Any, ...] = ("__root__",)
        if mark is not None:
            loc = (f"line {mark.line + 1}", f"col {mark.column + 1}")
        raise BriefParseError(
            "Brief front-matter is not valid YAML.",
            errors=[
                {
                    "loc": loc,
                    "msg": str(exc),
                    "type": "front_matter.yaml_error",
                }
            ],
        ) from exc

    if raw is None:
        raise BriefParseError(
            "Brief front-matter is empty.",
            errors=[
                {
                    "loc": ("__root__",),
                    "msg": "no fields declared between the '---' delimiters",
                    "type": "front_matter.empty",
                }
            ],
        )
    if not isinstance(raw, dict):
        raise BriefParseError(
            "Brief front-matter must be a YAML mapping at the top level.",
            errors=[
                {
                    "loc": ("__root__",),
                    "msg": f"expected mapping, got {type(raw).__name__}",
                    "type": "front_matter.not_mapping",
                }
            ],
        )

    try:
        config = BriefConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [
            {
                "loc": tuple(e.get("loc", ())) or ("__root__",),
                "msg": e.get("msg", ""),
                "type": e.get("type", "validation_error"),
            }
            for e in exc.errors()
        ]
        raise BriefParseError(
            "Brief front-matter failed schema validation.", errors=errors
        ) from exc

    return config, body
