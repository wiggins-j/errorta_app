//! F006 — implementation backing for `shell_cmds`.
//!
//! Kept in a sibling file so `shell_cmds.rs` stays a thin facade of
//! `#[tauri::command]` entry points the way `lib.rs` expects.

use std::process::Command;
use std::sync::OnceLock;
use std::time::Instant;

use serde::Serialize;

/// Records the moment the Rust shell first booted; used by `sidecar_port`
/// callers to estimate cold-start. Re-evaluating this is cheap; we keep a
/// single static instance so all calls agree.
static SHELL_BOOT: OnceLock<Instant> = OnceLock::new();

fn shell_boot() -> Instant {
    *SHELL_BOOT.get_or_init(Instant::now)
}

#[derive(Serialize)]
pub struct ProcessRow {
    pub pid: u32,
    pub role: &'static str,
    pub label: String,
}

#[derive(Serialize)]
pub struct SidecarPortInfo {
    pub port: u16,
    pub source: &'static str,
    pub uptime_ms: u128,
}

pub fn current_sidecar_port() -> SidecarPortInfo {
    let (port, source) = match std::env::var("ERRORTA_SIDECAR_PORT") {
        Ok(raw) => match raw.parse::<u16>() {
            Ok(p) => (p, "env"),
            Err(_) => (8770, "default"),
        },
        Err(_) => (8770, "default"),
    };
    SidecarPortInfo {
        port,
        source,
        uptime_ms: shell_boot().elapsed().as_millis(),
    }
}

pub fn list_managed_processes() -> Vec<ProcessRow> {
    // v0.1 stub: the actual spawn-and-track lifecycle is owned by the Tauri
    // sidecar plugin and registered via the F003 Ollama work. Until that
    // lands, we return the shell PID so the frontend has something to render.
    let pid = std::process::id();
    vec![ProcessRow {
        pid,
        role: "shell",
        label: "errorta-shell".into(),
    }]
}

/// Restart the Python sidecar. v0.1 stub: relies on the sidecar plugin's
/// supervisor (not yet wired). Returns a textual status so the UI can
/// surface "deferred until sidecar plugin integration".
pub fn restart_sidecar() -> String {
    "deferred: sidecar plugin supervisor not yet wired (v0.5)".into()
}

/// Open the platform-native logs folder. Best-effort; failures are reported
/// back as a string so the frontend can surface them without panicking.
pub fn open_logs_folder() -> Result<String, String> {
    let path = logs_folder();
    std::fs::create_dir_all(&path).map_err(|e| e.to_string())?;
    let path_str = path.to_string_lossy().to_string();

    let status = {
        #[cfg(target_os = "macos")]
        {
            Command::new("open").arg(&path).status()
        }
        #[cfg(target_os = "windows")]
        {
            Command::new("explorer").arg(&path).status()
        }
        #[cfg(all(unix, not(target_os = "macos")))]
        {
            Command::new("xdg-open").arg(&path).status()
        }
    };
    status.map_err(|e| e.to_string())?;
    Ok(path_str)
}

/// F087-20: open an arbitrary (existing) folder/file in the OS file manager.
/// Only opens paths that exist; never creates anything.
pub fn open_path(path: String) -> Result<String, String> {
    let p = std::path::PathBuf::from(&path);
    if !p.exists() {
        return Err(format!("path does not exist: {path}"));
    }
    let status = {
        #[cfg(target_os = "macos")]
        {
            Command::new("open").arg(&p).status()
        }
        #[cfg(target_os = "windows")]
        {
            Command::new("explorer").arg(&p).status()
        }
        #[cfg(all(unix, not(target_os = "macos")))]
        {
            Command::new("xdg-open").arg(&p).status()
        }
    };
    status.map_err(|e| e.to_string())?;
    Ok(path)
}

fn logs_folder() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("ERRORTA_LOGS_DIR") {
        return std::path::PathBuf::from(p);
    }
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".into());
    std::path::PathBuf::from(home).join(".errorta").join("logs")
}

// =====================================================================
// F040-01 S5a — native CLI login launcher.
//
// One click launches the vendor's OWN login flow (`claude login` /
// `codex login` / `agent login`) in a terminal. Errorta never sees the
// token — the launched vendor CLI writes its credential to its own store.
//
// SECURITY DESIGN (the gate, as code):
//   * `provider` is matched against a CLOSED enum; the login subcommand is
//     the fixed literal `login`. No user-supplied flag/string becomes argv.
//   * The passed `binary_path` is validated: its basename MUST be in the
//     provider's allowlist (string check in the pure builder), AND it must
//     exist + be executable (runtime check in spawn). Reject otherwise.
//   * Children spawn with argv VECTORS. Where a terminal wrapper forces a
//     command string (macOS `osascript do script`, Windows `cmd /k`), the
//     string is built SOLELY from the validated argv with explicit quoting
//     + a total length cap. Injection payloads in `binary_path` (spaces,
//     quotes, `;`, `&&`, `$()`, backticks) stay literal.
//   * No token is read/stored/logged; nothing token-shaped crosses IPC.
//
// The weight lives in the PURE, unit-tested `build_login_launch` +
// quoting helpers; `spawn` is the thin, manually-QA'd part.
// =====================================================================

/// Closed set of subscription-CLI providers we can launch a login for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CliProvider {
    Claude,
    Codex,
    Cursor,
}

impl CliProvider {
    /// Parse the provider string. Accepts the bare class (`claude`) — the
    /// frontend strips the `_cli` suffix before invoking. Unknown → error.
    fn parse(raw: &str) -> Result<Self, LaunchError> {
        match raw {
            "claude" => Ok(CliProvider::Claude),
            "codex" => Ok(CliProvider::Codex),
            "cursor" => Ok(CliProvider::Cursor),
            other => Err(LaunchError::UnknownProvider(other.to_string())),
        }
    }

    /// Basenames a detected binary is allowed to have for this provider.
    /// Anything else is rejected — a hard allowlist, never a substring match.
    ///
    /// Cursor has three install shapes: the `agent` / `cursor-agent` binaries
    /// (invoked directly) and the app-bundle `cursor` LAUNCHER (which needs the
    /// two-part `cursor agent …` form). All three are allowlisted; the correct
    /// per-shape argv is built in `login_inner_argv`.
    fn allowed_basenames(self) -> &'static [&'static str] {
        match self {
            CliProvider::Claude => &["claude"],
            CliProvider::Codex => &["codex", "codex-cli"],
            CliProvider::Cursor => &["agent", "cursor-agent", "cursor"],
        }
    }

    /// Build the validated inner login argv for this provider, given the
    /// already-allowlisted binary path and its basename.
    ///
    /// The only literals introduced are the fixed subcommands (`"agent"`,
    /// `"login"`); no user-supplied string becomes argv. The basename allowlist
    /// (checked by the caller) is what gates the path.
    fn login_inner_argv(self, binary_path: &str, basename: &str) -> Vec<String> {
        match self {
            // App-bundle `cursor` launcher → `cursor agent login`.
            CliProvider::Cursor if basename == "cursor" => vec![
                binary_path.to_string(),
                "agent".to_string(),
                "login".to_string(),
            ],
            // Everything else (claude / codex / cursor `agent`|`cursor-agent`)
            // → `<bin> login`.
            _ => vec![binary_path.to_string(), "login".to_string()],
        }
    }
}

/// Target OS for the pure builder. Decoupled from `cfg!` so every branch is
/// unit-testable from a single test binary regardless of the host.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetOs {
    Macos,
    Windows,
    Linux,
}

impl TargetOs {
    /// The host OS at compile time. Spawn uses this; tests drive all three.
    pub fn host() -> Self {
        #[cfg(target_os = "macos")]
        {
            TargetOs::Macos
        }
        #[cfg(target_os = "windows")]
        {
            TargetOs::Windows
        }
        #[cfg(all(unix, not(target_os = "macos")))]
        {
            TargetOs::Linux
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum LaunchError {
    UnknownProvider(String),
    /// The basename of the passed path is not in the provider's allowlist.
    DisallowedBinary { basename: String },
    /// The path had no usable file-name component.
    EmptyBinaryName,
    /// The constructed terminal command string exceeded the safety cap.
    CommandTooLong { len: usize, cap: usize },
}

impl std::fmt::Display for LaunchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LaunchError::UnknownProvider(p) => write!(f, "unknown CLI provider: {p}"),
            LaunchError::DisallowedBinary { basename } => {
                write!(f, "binary name not allowed for this provider: {basename}")
            }
            LaunchError::EmptyBinaryName => write!(f, "binary path has no file name"),
            LaunchError::CommandTooLong { len, cap } => {
                write!(f, "launch command too long ({len} > {cap})")
            }
        }
    }
}

/// Total-length cap on any constructed terminal command STRING (osascript /
/// cmd). Defense-in-depth: a sane login argv is well under this; a payload
/// that balloons the string is rejected rather than spawned.
const COMMAND_LEN_CAP: usize = 4096;

/// A spawn-ready plan produced by the PURE builder. Holds only validated
/// data; `spawn` turns it into a `std::process::Command` and runs it.
#[derive(Debug, PartialEq, Eq)]
pub struct LaunchPlan {
    pub os: TargetOs,
    /// The validated inner argv: `[binary_path, "login"]` for most providers,
    /// `[binary_path, "agent", "login"]` for the app-bundle `cursor` launcher.
    pub inner_argv: Vec<String>,
    /// Program + args to spawn on macOS/Windows. On Linux this is `None`
    /// (the emulator is resolved at spawn time from `inner_argv`).
    pub program: Option<String>,
    pub args: Vec<String>,
}

/// What the Tauri command returns to the frontend.
#[derive(Serialize, Debug)]
pub struct LoginLaunch {
    pub launched: bool,
    /// `"terminal"` on success, `"unavailable"` when no terminal resolved.
    pub transport: &'static str,
    pub detail: String,
}

/// Quote a single argv token for an AppleScript `do script` string.
///
/// The token is wrapped in double quotes; embedded backslashes and double
/// quotes are backslash-escaped so AppleScript treats the whole thing as one
/// literal word. Shell metacharacters (`;`, `&&`, `$()`, backticks, spaces)
/// therefore stay INSIDE the quoted literal and are never interpreted.
fn quote_for_osascript(token: &str) -> String {
    let mut out = String::with_capacity(token.len() + 2);
    out.push('"');
    for ch in token.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            other => out.push(other),
        }
    }
    out.push('"');
    out
}

/// Build the inner shell command (one line) for an AppleScript `do script`
/// from a validated argv, quoting each token. The result is embedded inside
/// the `do script "..."` — so it is ALSO osascript-escaped by the caller.
fn join_for_osascript(argv: &[String]) -> String {
    argv.iter()
        .map(|a| quote_for_osascript(a))
        .collect::<Vec<_>>()
        .join(" ")
}

/// Quote a single argv token for a Windows `cmd` command string.
///
/// Wrap in double quotes and double any embedded double quotes (cmd's escape
/// for a literal quote inside a quoted run). Spaces and cmd metacharacters
/// (`&`, `|`, `<`, `>`, `^`) stay inside the quoted literal.
fn quote_for_cmd(token: &str) -> String {
    let mut out = String::with_capacity(token.len() + 2);
    out.push('"');
    for ch in token.chars() {
        if ch == '"' {
            out.push_str("\"\"");
        } else {
            out.push(ch);
        }
    }
    out.push('"');
    out
}

/// Join a validated argv into a single `cmd`-ready command string.
fn join_for_cmd(argv: &[String]) -> String {
    argv.iter()
        .map(|a| quote_for_cmd(a))
        .collect::<Vec<_>>()
        .join(" ")
}

/// Quote a single argv token for a generic POSIX shell command string
/// (used by xterm-style `-e "<cmd>"` emulators). Single-quote wrapping with
/// the standard `'\''` escape makes EVERYTHING inside literal.
fn quote_for_posix(token: &str) -> String {
    let mut out = String::with_capacity(token.len() + 2);
    out.push('\'');
    for ch in token.chars() {
        if ch == '\'' {
            out.push_str("'\\''");
        } else {
            out.push(ch);
        }
    }
    out.push('\'');
    out
}

fn join_for_posix(argv: &[String]) -> String {
    argv.iter()
        .map(|a| quote_for_posix(a))
        .collect::<Vec<_>>()
        .join(" ")
}

/// The basename (final path component) of a path, as a string.
fn basename_of(path: &str) -> Result<String, LaunchError> {
    std::path::Path::new(path)
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|n| !n.is_empty())
        .map(|n| n.to_string())
        .ok_or(LaunchError::EmptyBinaryName)
}

/// PURE builder: validate + construct the per-OS launch plan WITHOUT
/// spawning or touching the filesystem. The whole security weight is here
/// and is exhaustively unit-tested.
pub fn build_login_launch(
    provider: &str,
    binary_path: &str,
    os: TargetOs,
) -> Result<LaunchPlan, LaunchError> {
    let prov = CliProvider::parse(provider)?;

    // Allowlist the basename. (File existence / executability is checked at
    // spawn time so this stays pure/testable.) Traversal-y or wrong names
    // (e.g. ".../bin/rm", "claude;rm -rf") are rejected here because their
    // basename is not in the allowlist.
    let base = basename_of(binary_path)?;
    if !prov.allowed_basenames().contains(&base.as_str()) {
        return Err(LaunchError::DisallowedBinary { basename: base });
    }

    let inner_argv = prov.login_inner_argv(binary_path, &base);

    let (program, args) = match os {
        TargetOs::Macos => {
            // tell application "Terminal" to do script "<inner>"
            // The inner command is itself osascript-quoted, then we escape
            // it once more for the outer `do script "..."` string literal.
            let inner = join_for_osascript(&inner_argv);
            let script = format!(
                "tell application \"Terminal\" to do script \"{}\"",
                inner.replace('\\', "\\\\").replace('"', "\\\"")
            );
            if script.len() > COMMAND_LEN_CAP {
                return Err(LaunchError::CommandTooLong {
                    len: script.len(),
                    cap: COMMAND_LEN_CAP,
                });
            }
            (
                Some("osascript".to_string()),
                vec!["-e".to_string(), script],
            )
        }
        TargetOs::Windows => {
            // cmd /c start "" cmd /k <quoted inner>
            let inner = join_for_cmd(&inner_argv);
            if inner.len() > COMMAND_LEN_CAP {
                return Err(LaunchError::CommandTooLong {
                    len: inner.len(),
                    cap: COMMAND_LEN_CAP,
                });
            }
            (
                Some("cmd".to_string()),
                vec![
                    "/c".to_string(),
                    "start".to_string(),
                    String::new(), // empty window title for `start`
                    "cmd".to_string(),
                    "/k".to_string(),
                    inner,
                ],
            )
        }
        TargetOs::Linux => {
            // The emulator is resolved at spawn time; carry the validated
            // inner argv + its posix-quoted form (length-capped) so the
            // spawn step can fit either argv-style or `-e "<cmd>"` shapes.
            let inner = join_for_posix(&inner_argv);
            if inner.len() > COMMAND_LEN_CAP {
                return Err(LaunchError::CommandTooLong {
                    len: inner.len(),
                    cap: COMMAND_LEN_CAP,
                });
            }
            (None, vec![inner])
        }
    };

    Ok(LaunchPlan {
        os,
        inner_argv,
        program,
        args,
    })
}

/// Best-effort check that a path exists and is executable.
fn is_executable_file(path: &str) -> bool {
    let p = std::path::Path::new(path);
    if !p.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        match std::fs::metadata(p) {
            Ok(m) => m.permissions().mode() & 0o111 != 0,
            Err(_) => false,
        }
    }
    #[cfg(not(unix))]
    {
        true
    }
}

/// Known Linux terminal emulators, in preference order. Each carries the arg
/// shape needed to run our command in it.
#[cfg(all(unix, not(target_os = "macos")))]
const LINUX_EMULATORS: &[&str] = &[
    "x-terminal-emulator",
    "gnome-terminal",
    "konsole",
    "xterm",
];

/// Resolve the first available Linux terminal emulator by probing PATH.
#[cfg(all(unix, not(target_os = "macos")))]
fn resolve_linux_emulator() -> Option<&'static str> {
    LINUX_EMULATORS
        .iter()
        .copied()
        .find(|bin| which_in_path(bin).is_some())
}

/// Tiny PATH lookup (avoids pulling in a `which` crate).
#[cfg(all(unix, not(target_os = "macos")))]
fn which_in_path(bin: &str) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Whether the native launcher should be offered on this host.
///
/// S5a ships macOS as the verified one-click path. Windows/Linux keep the
/// #129 copy-command floor until those hosts get real manual QA; the pure
/// builders remain unit-tested so enabling them later is a narrow policy flip.
pub fn login_launch_available() -> bool {
    login_launch_available_for(TargetOs::host())
}

fn login_launch_available_for(os: TargetOs) -> bool {
    match os {
        TargetOs::Macos => true,
        TargetOs::Windows | TargetOs::Linux => false,
    }
}

/// THIN spawn step — the only un-unit-testable part. Confirms the binary
/// exists + is executable (the runtime check the builder deferred), then
/// spawns the validated plan with a sane environment.
pub fn spawn_login(plan: &LaunchPlan) -> Result<LoginLaunch, String> {
    // The validated binary path is `inner_argv[0]`. Confirm it for real now.
    let binary_path = plan
        .inner_argv
        .first()
        .ok_or_else(|| "internal: empty launch argv".to_string())?;
    if !is_executable_file(binary_path) {
        return Err(format!(
            "binary not found or not executable: {binary_path}"
        ));
    }

    match plan.os {
        TargetOs::Macos | TargetOs::Windows => {
            let program = plan
                .program
                .as_deref()
                .ok_or_else(|| "internal: missing launcher program".to_string())?;
            let mut cmd = Command::new(program);
            cmd.args(&plan.args);
            sanitize_env(&mut cmd);
            cmd.spawn().map_err(|e| e.to_string())?;
            Ok(LoginLaunch {
                launched: true,
                transport: "terminal",
                detail: "Login opened in a terminal".to_string(),
            })
        }
        TargetOs::Linux => spawn_login_linux(plan),
    }
}

#[cfg(all(unix, not(target_os = "macos")))]
fn spawn_login_linux(plan: &LaunchPlan) -> Result<LoginLaunch, String> {
    let emulator = match resolve_linux_emulator() {
        Some(e) => e,
        None => {
            return Ok(LoginLaunch {
                launched: false,
                transport: "unavailable",
                detail: "no terminal emulator found".to_string(),
            });
        }
    };
    // `args[0]` is the posix-quoted inner command string from the builder.
    let inner_cmd = plan
        .args
        .first()
        .ok_or_else(|| "internal: missing linux launch command".to_string())?;
    let mut cmd = Command::new(emulator);
    match emulator {
        // gnome-terminal: pass the argv after `--` (no shell wrapper).
        "gnome-terminal" => {
            cmd.arg("--");
            cmd.args(&plan.inner_argv);
        }
        // konsole: `-e <argv>`.
        "konsole" => {
            cmd.arg("-e");
            cmd.args(&plan.inner_argv);
        }
        // xterm / x-terminal-emulator: `-e "<cmd>"` (single command string).
        _ => {
            cmd.arg("-e");
            cmd.arg(inner_cmd);
        }
    }
    sanitize_env(&mut cmd);
    cmd.spawn().map_err(|e| e.to_string())?;
    Ok(LoginLaunch {
        launched: true,
        transport: "terminal",
        detail: format!("Login opened in {emulator}"),
    })
}

#[cfg(not(all(unix, not(target_os = "macos"))))]
fn spawn_login_linux(_plan: &LaunchPlan) -> Result<LoginLaunch, String> {
    Ok(LoginLaunch {
        launched: false,
        transport: "unavailable",
        detail: "linux terminal launch not supported on this build".to_string(),
    })
}

/// Environment hygiene: the launched vendor login inherits the user's normal
/// env (it NEEDS PATH/HOME to find its own credential store + browser), but
/// we strip token/secret-shaped variables so no unrelated app/provider secret
/// leaks into a selected local executable. We never INJECT a secret.
fn sanitize_env(cmd: &mut Command) {
    for (key, _) in std::env::vars_os() {
        if let Some(k) = key.to_str() {
            if is_sensitive_child_env_key(k) {
                cmd.env_remove(k);
            }
        }
    }
}

fn is_sensitive_child_env_key(key: &str) -> bool {
    let upper = key.to_ascii_uppercase();
    if upper.starts_with("ERRORTA_") {
        return true;
    }
    const MARKERS: &[&str] = &[
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "API_KEY",
        "APIKEY",
        "ACCESS_KEY",
        "PRIVATE_KEY",
        "CLIENT_SECRET",
    ];
    MARKERS.iter().any(|marker| upper.contains(marker))
}

/// Public entry used by the Tauri command facade.
pub fn launch_cli_login(provider: String, binary_path: String) -> Result<LoginLaunch, String> {
    let plan =
        build_login_launch(&provider, &binary_path, TargetOs::host()).map_err(|e| e.to_string())?;
    spawn_login(&plan)
}

#[cfg(test)]
mod login_tests {
    use super::*;

    // ---- provider parsing -------------------------------------------------

    #[test]
    fn parses_known_providers() {
        assert_eq!(CliProvider::parse("claude").unwrap(), CliProvider::Claude);
        assert_eq!(CliProvider::parse("codex").unwrap(), CliProvider::Codex);
        assert_eq!(CliProvider::parse("cursor").unwrap(), CliProvider::Cursor);
    }

    #[test]
    fn rejects_unknown_provider() {
        let err = CliProvider::parse("claude_cli").unwrap_err();
        assert!(matches!(err, LaunchError::UnknownProvider(_)));
        assert!(matches!(
            CliProvider::parse("evil").unwrap_err(),
            LaunchError::UnknownProvider(_)
        ));
        assert!(matches!(
            CliProvider::parse("").unwrap_err(),
            LaunchError::UnknownProvider(_)
        ));
    }

    #[test]
    fn build_rejects_unknown_provider() {
        let err = build_login_launch("nope", "/usr/bin/claude", TargetOs::Macos).unwrap_err();
        assert!(matches!(err, LaunchError::UnknownProvider(_)));
    }

    // ---- basename allowlist ----------------------------------------------

    #[test]
    fn accepts_allowed_basenames_per_provider() {
        assert!(build_login_launch("claude", "/usr/local/bin/claude", TargetOs::Macos).is_ok());
        assert!(build_login_launch("codex", "/opt/bin/codex", TargetOs::Macos).is_ok());
        assert!(build_login_launch("codex", "/opt/bin/codex-cli", TargetOs::Macos).is_ok());
        assert!(build_login_launch("cursor", "/opt/homebrew/bin/agent", TargetOs::Macos).is_ok());
        assert!(
            build_login_launch("cursor", "/opt/homebrew/bin/cursor-agent", TargetOs::Macos)
                .is_ok()
        );
        // The app-bundle `cursor` launcher is now allowlisted too.
        assert!(build_login_launch(
            "cursor",
            "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
            TargetOs::Macos
        )
        .is_ok());
    }

    // ---- cursor per-shape argv -------------------------------------------

    #[test]
    fn cursor_launcher_uses_two_part_agent_login_argv() {
        // The app-bundle `cursor` LAUNCHER needs `cursor agent login`.
        let plan =
            build_login_launch("cursor", "/opt/bin/cursor", TargetOs::Macos).unwrap();
        assert_eq!(
            plan.inner_argv,
            vec!["/opt/bin/cursor", "agent", "login"]
        );
        // The full app-bundle path resolves its basename to `cursor` and is
        // accepted.
        let plan = build_login_launch(
            "cursor",
            "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
            TargetOs::Macos,
        )
        .unwrap();
        assert_eq!(
            plan.inner_argv,
            vec![
                "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
                "agent",
                "login"
            ]
        );
    }

    #[test]
    fn cursor_direct_binaries_use_login_argv() {
        // `agent` and `cursor-agent` are invoked directly: `<bin> login`.
        let plan = build_login_launch("cursor", "/opt/bin/agent", TargetOs::Macos).unwrap();
        assert_eq!(plan.inner_argv, vec!["/opt/bin/agent", "login"]);
        let plan =
            build_login_launch("cursor", "/opt/bin/cursor-agent", TargetOs::Macos).unwrap();
        assert_eq!(plan.inner_argv, vec!["/opt/bin/cursor-agent", "login"]);
    }

    #[test]
    fn cursor_launcher_quotes_three_token_argv_for_each_os() {
        // The 3-token argv flows through every per-OS quoting path with the
        // `agent` + `login` subcommands both present.
        for os in [TargetOs::Macos, TargetOs::Windows, TargetOs::Linux] {
            let plan = build_login_launch("cursor", "/opt/bin/cursor", os).unwrap();
            assert_eq!(plan.inner_argv, vec!["/opt/bin/cursor", "agent", "login"]);
            let rendered = match os {
                TargetOs::Macos => plan.args[1].clone(),
                TargetOs::Windows => plan.args[5].clone(),
                TargetOs::Linux => plan.args[0].clone(),
            };
            assert!(rendered.contains("agent"));
            assert!(rendered.contains("login"));
            assert!(rendered.contains("cursor"));
        }
    }

    #[test]
    fn rejects_cursor_decoy_basenames() {
        // `cursor-helper` is NOT in the allowlist (no substring match).
        assert!(matches!(
            build_login_launch("cursor", "/opt/bin/cursor-helper", TargetOs::Macos).unwrap_err(),
            LaunchError::DisallowedBinary { .. }
        ));
        // arbitrary binary stays rejected.
        assert!(matches!(
            build_login_launch("cursor", "/usr/bin/rm", TargetOs::Macos).unwrap_err(),
            LaunchError::DisallowedBinary { .. }
        ));
    }

    #[test]
    fn rejects_wrong_basename_for_provider() {
        // `agent` is cursor's, not claude's.
        assert!(matches!(
            build_login_launch("claude", "/usr/bin/agent", TargetOs::Macos).unwrap_err(),
            LaunchError::DisallowedBinary { .. }
        ));
        // arbitrary binary.
        assert!(matches!(
            build_login_launch("claude", "/usr/bin/rm", TargetOs::Macos).unwrap_err(),
            LaunchError::DisallowedBinary { .. }
        ));
        // codex's allowlist does NOT include `agent`.
        assert!(matches!(
            build_login_launch("codex", "/usr/bin/agent", TargetOs::Macos).unwrap_err(),
            LaunchError::DisallowedBinary { .. }
        ));
    }

    #[test]
    fn rejects_traversal_and_decoy_names() {
        // A path whose basename is a decoy command, not the allowlisted one.
        assert!(matches!(
            build_login_launch("claude", "/tmp/../bin/sh", TargetOs::Macos).unwrap_err(),
            LaunchError::DisallowedBinary { .. }
        ));
        // basename literally contains injection chars → not in allowlist.
        assert!(matches!(
            build_login_launch("claude", "/tmp/claude;rm -rf ~", TargetOs::Macos).unwrap_err(),
            LaunchError::DisallowedBinary { .. }
        ));
    }

    #[test]
    fn rejects_empty_binary_name() {
        // A bare root / empty string has no usable file-name component.
        assert!(matches!(
            build_login_launch("claude", "/", TargetOs::Macos).unwrap_err(),
            LaunchError::EmptyBinaryName
        ));
        assert!(matches!(
            build_login_launch("claude", "", TargetOs::Macos).unwrap_err(),
            LaunchError::EmptyBinaryName
        ));
    }

    // ---- per-OS plan construction ----------------------------------------

    #[test]
    fn macos_plan_uses_osascript_with_login_argv() {
        let plan =
            build_login_launch("claude", "/usr/local/bin/claude", TargetOs::Macos).unwrap();
        assert_eq!(plan.program.as_deref(), Some("osascript"));
        assert_eq!(plan.args.len(), 2);
        assert_eq!(plan.args[0], "-e");
        let script = &plan.args[1];
        assert!(script.contains("tell application \\\"Terminal\\\"")
            || script.contains("tell application \"Terminal\""));
        // The fixed `login` subcommand and the binary path are both present.
        assert!(script.contains("login"));
        assert!(script.contains("claude"));
        assert_eq!(plan.inner_argv, vec!["/usr/local/bin/claude", "login"]);
    }

    #[test]
    fn windows_plan_uses_cmd_start_k() {
        // Use a forward-slash path so basename resolves to `codex` portably
        // under the host test runner (which parses paths with unix `Path`).
        let plan = build_login_launch("codex", "/usr/bin/codex", TargetOs::Windows).unwrap();
        assert_eq!(plan.program.as_deref(), Some("cmd"));
        assert_eq!(plan.args[0], "/c");
        assert_eq!(plan.args[1], "start");
        assert_eq!(plan.args[2], "");
        assert_eq!(plan.args[3], "cmd");
        assert_eq!(plan.args[4], "/k");
        assert!(plan.args[5].contains("login"));
        assert!(plan.args[5].contains("codex"));
    }

    #[test]
    fn linux_plan_carries_inner_argv_and_quoted_command() {
        let plan = build_login_launch("cursor", "/opt/bin/agent", TargetOs::Linux).unwrap();
        assert!(plan.program.is_none());
        assert_eq!(plan.inner_argv, vec!["/opt/bin/agent", "login"]);
        // The single carried arg is the posix-quoted command string.
        assert_eq!(plan.args.len(), 1);
        assert!(plan.args[0].contains("login"));
        assert!(plan.args[0].contains("agent"));
    }

    // ---- quoting helpers vs. injection payloads --------------------------

    #[test]
    fn osascript_quoting_keeps_injection_literal() {
        // A token full of shell + applescript metacharacters.
        let payload = r#"/tmp/a b;rm -rf ~ && $(touch pwned) `id` "x""#;
        let q = quote_for_osascript(payload);
        // It is a single double-quoted run; the only unescaped quotes are the
        // wrapping pair. Every interior `"` is backslash-escaped.
        assert!(q.starts_with('"') && q.ends_with('"'));
        assert!(q.contains("\\\"")); // the interior quote got escaped
        // Metacharacters are present but INSIDE the literal (not stripped).
        assert!(q.contains(";rm -rf ~"));
        assert!(q.contains("$(touch pwned)"));
        assert!(q.contains("`id`"));
        // Joining a real argv keeps each token a separate quoted run.
        let joined = join_for_osascript(&[payload.to_string(), "login".to_string()]);
        assert!(joined.ends_with("\"login\""));
    }

    #[test]
    fn cmd_quoting_keeps_injection_literal() {
        let payload = r#"C:\a b\codex & del * | echo "x""#;
        let q = quote_for_cmd(payload);
        assert!(q.starts_with('"') && q.ends_with('"'));
        // interior quote doubled (cmd literal-quote escape).
        assert!(q.contains("\"\"x\"\""));
        assert!(q.contains("& del *"));
        assert!(q.contains("| echo"));
    }

    #[test]
    fn posix_quoting_keeps_injection_literal() {
        let payload = "/tmp/a b;rm -rf ~ && $(id) `whoami` 'q'";
        let q = quote_for_posix(payload);
        assert!(q.starts_with('\'') && q.ends_with('\''));
        // single-quote inside is broken out as '\'' — nothing executes.
        assert!(q.contains("'\\''"));
        assert!(q.contains("$(id)"));
        assert!(q.contains("`whoami`"));
        assert!(q.contains(";rm -rf ~"));
    }

    #[test]
    fn command_length_cap_enforced() {
        // A pathological basename that's in the allowlist but a huge path —
        // craft a path whose basename is `claude` but with a giant directory
        // prefix so the constructed command string blows the cap.
        let huge_dir = "/".to_string() + &"a".repeat(COMMAND_LEN_CAP + 100);
        let path = format!("{huge_dir}/claude");
        let err = build_login_launch("claude", &path, TargetOs::Macos).unwrap_err();
        assert!(matches!(err, LaunchError::CommandTooLong { .. }));
    }

    #[test]
    fn build_is_pure_does_not_touch_fs() {
        // A non-existent path with an allowlisted basename still builds a plan
        // (existence is checked only at spawn time) — proving the builder is
        // pure and decoupled from the filesystem.
        let plan =
            build_login_launch("claude", "/nonexistent/path/claude", TargetOs::Macos).unwrap();
        assert_eq!(plan.inner_argv[1], "login");
    }

    #[test]
    fn availability_policy_is_macos_only_until_host_qa() {
        assert!(login_launch_available_for(TargetOs::Macos));
        assert!(!login_launch_available_for(TargetOs::Windows));
        assert!(!login_launch_available_for(TargetOs::Linux));
    }

    #[test]
    fn child_env_scrub_catches_secrets_but_keeps_normal_runtime_vars() {
        for key in [
            "ERRORTA_AIAR_REMOTE_TOKEN",
            "OPENAI_API_KEY",
            "CURSOR_API_KEY",
            "FOO_SECRET",
            "client_secret",
            "ACCESS_KEY_ID",
            "PRIVATE_KEY_PATH",
        ] {
            assert!(is_sensitive_child_env_key(key), "{key}");
        }
        for key in ["PATH", "HOME", "USER", "SHELL", "SSH_AUTH_SOCK"] {
            assert!(!is_sensitive_child_env_key(key), "{key}");
        }
    }
}
