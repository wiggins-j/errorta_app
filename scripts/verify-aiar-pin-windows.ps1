# verify-aiar-pin-windows.ps1
#
# Cross-platform clean-install probe for aiar-rag==0.2.0 on Windows 11.
# Designed to be copied into the F-INFRA-13 / F-INFRA-08 Windows VM and
# run from an Administrator PowerShell.
#
# Part of F-INFRA-01 Slice (f). See docs/V015_PUBLISH_RUNBOOK.md §11.5.
#
# Usage (inside the VM):
#   powershell -ExecutionPolicy Bypass -File verify-aiar-pin-windows.ps1
#
# Exit codes:
#   0 — clean venv resolved aiar-rag==0.2.0 and printed "OK".
#   1 — python not on PATH (install Python 3.11+ first).
#   non-zero — pip/import failure; investigate the printed output.

$ErrorActionPreference = "Stop"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "python not found on PATH. Install Python 3.11+ and re-run."
    exit 1
}

$venvPath = Join-Path $env:TEMP "aiar-pin-check"

if (Test-Path $venvPath) {
    Write-Host "Removing stale venv at $venvPath..."
    Remove-Item -Recurse -Force $venvPath
}

Write-Host "Creating fresh venv at $venvPath..."
python -m venv $venvPath

$venvPython = Join-Path $venvPath "Scripts\python.exe"
$venvPip = Join-Path $venvPath "Scripts\pip.exe"

Write-Host "Installing aiar-rag==0.2.0..."
& $venvPip install --quiet aiar-rag==0.2.0

Write-Host "Importing aiar and asserting version..."
$assertScript = @"
import aiar
assert aiar.__version__ == '0.2.0', aiar.__version__
print('OK')
"@
& $venvPython -c $assertScript

Write-Host "Cleaning up venv..."
Remove-Item -Recurse -Force $venvPath

Write-Host "Windows clean-install probe: PASSED"
exit 0
