//! F006 — Tauri shell polish commands.
//!
//! Thin `#[tauri::command]` facade. Implementation lives in
//! `shell_cmds_impl` so this file stays a clean entry-point surface.
//!
//! NOTE: The sidecar lifecycle commands (`sidecar_port`, `restart_sidecar`,
//! `processes`) moved to `mod sidecar` in the SIDECAR track because they now
//! need real shared state (the running child + allocated port). The helpers
//! in `shell_cmds_impl` are retained for `shell_ping` and `open_logs_folder`
//! and as scratch space for future shell-only commands.

#[path = "shell_cmds_impl.rs"]
#[allow(dead_code)]
mod imp;

#[tauri::command]
pub fn shell_ping() -> &'static str {
    "pong"
}

#[tauri::command]
pub fn open_logs_folder() -> Result<String, String> {
    imp::open_logs_folder()
}

/// F087-20: reveal a delivered project folder in the OS file manager so the user
/// can open/run what the Coding Team built. Best-effort; errors return a string.
#[tauri::command]
pub fn open_path(path: String) -> Result<String, String> {
    imp::open_path(path)
}

/// F040-01 S5a: launch a subscription CLI's OWN login flow in a terminal.
///
/// `provider` is matched against the closed enum `{claude, codex, cursor}`;
/// `binary_path` is basename-allowlist-validated and must exist + be
/// executable. The fixed `login` subcommand is appended. Errorta never sees
/// the token — the launched vendor CLI owns the credential. See
/// `shell_cmds_impl::launch_cli_login` for the full security design.
#[tauri::command]
pub fn launch_cli_login(
    provider: String,
    binary_path: String,
) -> Result<imp::LoginLaunch, String> {
    imp::launch_cli_login(provider, binary_path)
}

/// F040-01 S5a: whether the native launcher should be offered on this
/// platform. True for the macOS-verified path; Windows/Linux stay on the
/// copy-command floor until those hosts get manual QA.
#[tauri::command]
pub fn cli_login_launch_available() -> bool {
    imp::login_launch_available()
}
