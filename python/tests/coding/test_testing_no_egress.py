"""The F087-10 test-run engine must import zero egress machinery (inv. 3).

Checked in a CLEAN subprocess so an earlier in-process import can't mask a leak.
The engine legitimately imports errorta_tools.runner (the sanctioned execution
primitive); it must NOT pull in gateway / HTTP / aiar.
"""
import subprocess
import sys


def test_testing_imports_no_egress() -> None:
    code = (
        "import sys; import errorta_council.coding.testing;"
        "banned=['httpx','requests','aiar','errorta_model_gateway'];"
        "leaked=[m for m in banned if m in sys.modules];"
        "print(','.join(leaked));"
        "sys.exit(1 if leaked else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"testing leaked egress imports: {proc.stdout.strip()}"
