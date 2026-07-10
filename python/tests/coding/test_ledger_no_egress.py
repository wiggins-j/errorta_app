"""The ledger module must import zero egress machinery (Council invariant 3).

Checked in a CLEAN subprocess so an earlier in-process test that imported httpx
can't mask a real leak.
"""
import subprocess
import sys


def test_ledger_imports_no_egress() -> None:
    code = (
        "import sys; import errorta_council.coding.ledger;"
        "banned=['httpx','requests','errorta_model_gateway','subprocess'];"
        "leaked=[m for m in banned if m in sys.modules];"
        "print(','.join(leaked));"
        "sys.exit(1 if leaked else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"ledger leaked egress imports: {proc.stdout.strip()}"
