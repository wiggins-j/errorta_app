"""F014-EXPAND — Brief library validation.

Iterates every shipped example brief under ``docs/examples/briefs/*.md`` and
asserts that:

1. ``parse_brief_markdown`` returns a valid ``BriefConfig``.
2. Any ``license_override`` declared in a source config is in
   ``DEFAULT_LICENSE_ALLOWLIST`` (so a connector-emitted doc that picks up the
   override will clear the compliance gate by default).
3. Each ``source.name`` resolves to a class in ``CONNECTOR_REGISTRY`` and the
   class can be instantiated with the brief's ``source.config`` (with a stub
   ``httpx`` client injected so no network I/O happens).

The test is hermetic: it never touches the network. It exists so that adding
a new brief to ``docs/examples/briefs/`` fails CI immediately if the brief
references an unknown connector, supplies a license the gate would refuse, or
breaks the BriefConfig schema.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from errorta_briefs import BriefConfig, parse_brief_markdown
from errorta_briefs.compliance import DEFAULT_LICENSE_ALLOWLIST
from errorta_briefs_connectors import CONNECTOR_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIEFS_DIR = REPO_ROOT / "docs" / "examples" / "briefs"

EXPECTED_TEMPLATE_FLOOR = 5  # bump when a new shipped template lands.


def _briefs() -> list[Path]:
    paths = sorted(BRIEFS_DIR.glob("*.md"))
    assert paths, f"no example briefs found under {BRIEFS_DIR}"
    return paths


def _stub_client() -> httpx.Client:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=""))
    return httpx.Client(transport=transport)


@pytest.mark.parametrize("brief_path", _briefs(), ids=lambda p: p.name)
def test_brief_parses_and_connectors_instantiate(brief_path: Path) -> None:
    text = brief_path.read_text(encoding="utf-8")
    config, body = parse_brief_markdown(text)

    assert isinstance(config, BriefConfig)
    assert config.project, f"{brief_path.name}: project is required"
    assert config.corpus, f"{brief_path.name}: corpus is required"
    assert config.sensitivity == "Public"
    assert config.refresh in {"manual", "daily", "weekly"}
    assert config.sources, f"{brief_path.name}: at least one source required"
    assert body.strip(), f"{brief_path.name}: body should describe intent"

    for source in config.sources:
        # License override (if any) must be in the default allowlist so the
        # compliance gate accepts docs the connector emits with that license.
        license_override = source.config.get("license_override")
        if license_override is not None:
            assert license_override in DEFAULT_LICENSE_ALLOWLIST, (
                f"{brief_path.name}: source {source.name!r} has license_override "
                f"{license_override!r} which is not in DEFAULT_LICENSE_ALLOWLIST"
            )

        # Connector must be registered and instantiable with the brief's
        # config. We inject a stub httpx client so __init__ does not try to
        # build a real one. Both shipped connectors (arxiv, generic_html)
        # honour an ``http_client`` key on their config dict.
        cls = CONNECTOR_REGISTRY.get(source.name)
        assert cls is not None, (
            f"{brief_path.name}: source {source.name!r} not in CONNECTOR_REGISTRY "
            f"(known: {sorted(CONNECTOR_REGISTRY)})"
        )

        instance_config = dict(source.config)
        instance_config.setdefault("http_client", _stub_client())
        connector = cls(instance_config)
        assert connector is not None


def test_brief_library_count_floor() -> None:
    """Regression sentinel for F014-min.

    A previous slice can rename or move templates safely (the
    parametrized test still passes against whatever is left). This
    sentinel locks the *count* so an accidental file deletion is
    caught at the suite level, not by an external ``GET
    /briefs/templates`` probe.
    """
    discovered = sorted(BRIEFS_DIR.glob("*.md"))
    assert len(discovered) >= EXPECTED_TEMPLATE_FLOOR, (
        f"expected >= {EXPECTED_TEMPLATE_FLOOR} brief templates "
        f"under {BRIEFS_DIR}, found {len(discovered)}: "
        f"{[p.name for p in discovered]}"
    )
