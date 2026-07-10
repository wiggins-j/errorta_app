"""F102 RC8 — the new council-side publish modules must not import egress directly.

These modules reach git / ``gh`` egress ONLY through the
``errorta_tools.runner.publish`` + ``errorta_tools.runner.secret_scan`` seams
(Council invariant 3). An AST scan of each module's OWN source asserts it never
imports ``subprocess`` / ``httpx`` / ``requests`` / ``urllib`` and never calls
``subprocess.*`` / ``os.system`` / ``os.popen`` directly. (Transitive imports
THROUGH the sanctioned seam are fine — the seam IS the egress boundary.)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import errorta_council.coding.orientation_scan as _scan
import errorta_council.coding.publish_body as _body
import errorta_council.coding.publish_gate as _gate
import errorta_council.coding.publish_github as _orch

_BANNED_IMPORTS = {"subprocess", "httpx", "requests", "urllib", "socket"}
_BANNED_CALL_ROOTS = {"subprocess"}
_BANNED_OS_CALLS = {"system", "popen", "spawn", "execv", "execvp"}

# F135 review #8: orientation_scan is a new council module; lock that it imports
# no egress at module load (it pulls repo_reader lazily inside its one function).
_MODULES = [_orch, _body, _gate, _scan]


@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__)
def test_module_does_not_import_or_call_egress(module) -> None:  # noqa: ANN001
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in _BANNED_IMPORTS, (
                    f"{module.__name__} imports banned egress module: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in _BANNED_IMPORTS, (
                f"{module.__name__} imports from banned egress module: {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            # subprocess.run(...) / subprocess.Popen(...)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in _BANNED_CALL_ROOTS:
                    raise AssertionError(
                        f"{module.__name__} calls {func.value.id}.{func.attr} directly")
                if func.value.id == "os" and func.attr in _BANNED_OS_CALLS:
                    raise AssertionError(
                        f"{module.__name__} calls os.{func.attr} directly")
