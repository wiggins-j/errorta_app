"""Invariant 3: errorta_council never imports a provider SDK."""
from __future__ import annotations

import ast
from pathlib import Path


FORBIDDEN_TOP_LEVEL_MODULES = {
    "anthropic",
    "openai",
    "cohere",
    "mistralai",
    "google",            # blocks google.generativeai
    "boto3",
    "langchain",
    "langchain_openai",
    "llama_index",
}

GATEWAY_HTTPX_ALLOW = {"errorta_council/gateway_local.py"}


def _council_root() -> Path:
    here = Path(__file__).resolve()
    # tests/council/test_import_lint.py → repo/python/errorta_council
    return here.parents[2] / "errorta_council"


def _iter_council_files() -> list[Path]:
    root = _council_root()
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
    return out


def test_no_provider_sdk_imports_in_council() -> None:
    leaks: list[str] = []
    for f in _iter_council_files():
        imports = _imports(f)
        leak = imports & FORBIDDEN_TOP_LEVEL_MODULES
        if leak:
            leaks.append(f"{f.relative_to(_council_root().parent)}: {sorted(leak)}")
    assert not leaks, "Provider SDK leaked into errorta_council:\n" + "\n".join(leaks)


def test_only_gateway_local_imports_httpx() -> None:
    leaks: list[str] = []
    for f in _iter_council_files():
        rel = str(f.relative_to(_council_root().parent))
        if rel in GATEWAY_HTTPX_ALLOW:
            continue
        if "httpx" in _imports(f):
            leaks.append(rel)
    assert not leaks, (
        "Only errorta_council/gateway_local.py may import httpx. Leaks:\n"
        + "\n".join(leaks)
    )
