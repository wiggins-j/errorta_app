# Reproducible Windows sidecar build entry.
# Locates a bash (Git Bash) and runs the existing build-sidecar.sh, which
# already has a native-Windows (MSYS/MINGW) branch that stages
# errorta-sidecar-x86_64-pc-windows-msvc.exe into src-tauri/binaries/.
$ErrorActionPreference = "Stop"

$bash = $null
foreach ($c in @(
    "$Env:ProgramFiles\Git\bin\bash.exe",
    "${Env:ProgramFiles(x86)}\Git\bin\bash.exe",
    "$Env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
)) {
    if ($c -and (Test-Path $c)) { $bash = $c; break }
}
if (-not $bash) { $bash = (Get-Command bash -ErrorAction SilentlyContinue).Source }
if (-not $bash) {
    Write-Error "No bash found. Install Git for Windows (provides Git Bash) or add bash to PATH."
    exit 1
}

$repoRoot = Split-Path -Parent $PSScriptRoot
& $bash "$repoRoot/scripts/build-sidecar.sh"
exit $LASTEXITCODE
