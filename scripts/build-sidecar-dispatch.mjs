// Cross-platform dispatch: run the PowerShell wrapper on Windows, the bash
// script elsewhere. Keeps beforeBuildCommand a single portable command.
import { spawnSync } from "node:child_process";
import process from "node:process";

const isWin = process.platform === "win32";
const cmd = isWin ? "powershell" : "bash";
const args = isWin
  ? ["-ExecutionPolicy", "Bypass", "-File", "scripts/build-sidecar.ps1"]
  : ["scripts/build-sidecar.sh"];

const r = spawnSync(cmd, args, { stdio: "inherit" });
process.exit(r.status ?? 1);
