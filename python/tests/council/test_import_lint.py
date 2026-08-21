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


# --- Invariant: errorta_council never reaches the live-run supervisor or its
# unbounded remote-command primitive. errorta_liverun drives real ssh/remote
# actions against operator-authored profiles; RemoteToolRunner is the
# supervisor egress primitive documented (in errorta_tools/runner/remote.py)
# as "NOT a member tool" and never registered in the member tool gateway. Any
# path by which council code could reach either would let a model-driven
# coding session issue live remote commands outside the gateway's policy
# checks. ---

FORBIDDEN_MODULE_PREFIXES = ("errorta_liverun", "errorta_tools.runner.remote")
# (module, name) pairs that reach the same primitive via a `from`-import of a
# name rather than the submodule itself — the "RemoteToolRunner bypass".
FORBIDDEN_FROM_NAMES = {
    ("errorta_tools.runner", "RemoteToolRunner"),
    ("errorta_tools.runner", "remote"),
}


def _module_forbidden(mod: str) -> bool:
    return any(mod == p or mod.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES)


def _liverun_and_remote_leaks(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    leaks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_forbidden(alias.name):
                    leaks.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _module_forbidden(mod):
                names = ", ".join(a.name for a in node.names)
                leaks.append(f"from {mod} import {names}")
                continue
            for alias in node.names:
                if (mod, alias.name) in FORBIDDEN_FROM_NAMES:
                    leaks.append(f"from {mod} import {alias.name}")
    return leaks


def test_no_liverun_or_remote_tool_runner_imports_in_council() -> None:
    leaks: list[str] = []
    for f in _iter_council_files():
        for leak in _liverun_and_remote_leaks(f):
            leaks.append(f"{f.relative_to(_council_root().parent)}: {leak}")
    assert not leaks, (
        "errorta_council must not reach errorta_liverun or "
        "errorta_tools.runner.remote / RemoteToolRunner. Leaks:\n"
        + "\n".join(leaks)
    )
