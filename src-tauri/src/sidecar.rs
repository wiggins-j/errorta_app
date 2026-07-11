//! Python sidecar lifecycle management.
//!
//! Owns the spawn/terminate/restart story for the PyInstaller `errorta-sidecar`
//! binary. The binary is registered under `bundle.externalBin` in
//! `tauri.conf.json` so `tauri-plugin-shell`'s `sidecar()` machinery can locate
//! and launch the platform-specific build (target-triple suffix added by Tauri).
//!
//! Flow:
//!   1. Allocate a free port by binding `127.0.0.1:0` and dropping the listener.
//!   2. Spawn the sidecar with `ERRORTA_SIDECAR_PORT` in its env.
//!   3. Poll `http://127.0.0.1:<port>/healthz` for up to 10s.
//!   4. Store the running `CommandChild` + port in a managed `SidecarHandle`.
//!
//! Notes:
//!   - In dev (`tauri dev`) the sidecar binary may not exist yet (PyInstaller
//!     hasn't run). `spawn_sidecar` returns an error in that case and the user
//!     can run the sidecar manually (`python -m errorta_app.server` on 8770);
//!     the frontend's `getSidecarBase` already falls back to that port.
//!   - On window close / app exit, `terminate()` is invoked to kill the child
//!     so we never leak a zombie process.

use std::net::TcpListener;
use std::sync::atomic::{AtomicU16, AtomicU64, AtomicU8, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::{AppHandle, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Name of the external binary as declared in `tauri.conf.json`.
const SIDECAR_BIN: &str = "errorta-sidecar";

/// Health-poll timeout. The frozen sidecar bundles the local-AI stack (PyTorch +
/// ChromaDB + sentence-transformers) and extracts/loads it from the PyInstaller
/// archive on launch — a cold start of ~60-90s on a cold disk cache, not the
/// ~1-2s of the old lightweight build. Give it a generous budget so a slow but
/// healthy boot isn't logged as a spawn failure (which also kept the F-INFRA-09
/// crash-on-launch watchdog from mis-firing a rollback on a slow start). The
/// window never waits on this — spawn runs on a background thread and the port
/// is published (`set_port`) before the health wait, so the frontend reaches the
/// sidecar the moment it's healthy and shows a non-blocking "starting" banner
/// until then (F069).
const HEALTHZ_TIMEOUT: Duration = Duration::from_secs(120);
const HEALTHZ_POLL_INTERVAL: Duration = Duration::from_millis(250);

/// Cap on the stored `last_error` so a pathological error string can't bloat
/// the handle or the `sidecar_startup_state` payload the splash renders.
const MAX_LAST_ERROR_LEN: usize = 500;

/// F103 — coarse lifecycle state for the startup splash. The frontend gate
/// reads this (via `sidecar_startup_state`) instead of inferring lifecycle from
/// `sidecar_port == 0` plus failed HTTP attempts, so the splash can show honest
/// "starting" vs "failed" copy without scraping logs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StartupState {
    NotStarted,
    Starting,
    Healthy,
    Failed,
    Terminated,
}

impl StartupState {
    fn as_u8(self) -> u8 {
        match self {
            StartupState::NotStarted => 0,
            StartupState::Starting => 1,
            StartupState::Healthy => 2,
            StartupState::Failed => 3,
            StartupState::Terminated => 4,
        }
    }

    fn from_u8(v: u8) -> Self {
        match v {
            1 => StartupState::Starting,
            2 => StartupState::Healthy,
            3 => StartupState::Failed,
            4 => StartupState::Terminated,
            _ => StartupState::NotStarted,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            StartupState::NotStarted => "not_started",
            StartupState::Starting => "starting",
            StartupState::Healthy => "healthy",
            StartupState::Failed => "failed",
            StartupState::Terminated => "terminated",
        }
    }
}

/// Milliseconds since the UNIX epoch, or 0 if the clock is before it.
fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[derive(Debug, Default)]
pub struct SidecarHandle {
    child: Mutex<Option<CommandChild>>,
    port: AtomicU16,
    /// Monotonic spawn id. Bumped on every spawn so a late `Terminated` from a
    /// prior child can only clear the handle if it is still the current
    /// generation — robust even if the kernel re-allocates the same port.
    generation: AtomicU64,
    /// Serializes the full spawn sequence (terminate prior child +
    /// allocate port + spawn + wait_for_healthz). Without this, concurrent
    /// `restart_sidecar` calls (or a frontend-triggered restart racing the
    /// initial setup() spawn) can interleave: one caller's terminate kills
    /// the other caller's freshly-spawned child, leaving no sidecar running.
    spawn_lock: Mutex<()>,
    /// F103 — coarse lifecycle state (see `StartupState`), stored as a u8 so
    /// `sidecar_startup_state` can read it without taking a lock.
    startup_state: AtomicU8,
    /// Epoch-ms when the current spawn began (0 = never). Drives `elapsed_ms`.
    started_at_ms: AtomicU64,
    /// Last spawn/health failure message (bounded), surfaced to the splash.
    last_error: Mutex<Option<String>>,
}

impl SidecarHandle {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn port(&self) -> u16 {
        self.port.load(Ordering::Relaxed)
    }

    fn set_port(&self, port: u16) {
        self.port.store(port, Ordering::Relaxed);
    }

    fn replace_child(&self, new_child: Option<CommandChild>) -> Option<CommandChild> {
        let mut guard = self.child.lock().expect("sidecar child lock poisoned");
        std::mem::replace(&mut *guard, new_child)
    }

    /// Best-effort terminate. Safe to call even if no child is running.
    pub fn terminate(&self) {
        if let Some(child) = self.replace_child(None) {
            let _ = child.kill();
        }
        self.set_port(0);
    }

    /// Allocate the next spawn generation id.
    fn next_generation(&self) -> u64 {
        self.generation.fetch_add(1, Ordering::Relaxed) + 1
    }

    /// Clear the handle ONLY if the child that just exited is still the current
    /// generation. Generation (not port) is the key, so even if the kernel
    /// re-allocates the same ephemeral port to a fresh child, a late
    /// `Terminated` from the prior child can't clobber it. (F063 A2.)
    fn clear_if_generation(&self, generation: u64) {
        let mut guard = self.child.lock().expect("sidecar child lock poisoned");
        if self.generation.load(Ordering::Relaxed) == generation {
            self.set_port(0);
            *guard = None;
            // F103 — only flag the lifecycle as terminated for an *unexpected*
            // exit of the current child. A restart bumps the generation before
            // its child can exit, so this never clobbers a fresh `Starting`.
            self.set_startup_state(StartupState::Terminated);
        }
    }

    // ---- F103 startup-state plumbing ------------------------------------

    pub fn startup_state(&self) -> StartupState {
        StartupState::from_u8(self.startup_state.load(Ordering::Relaxed))
    }

    fn set_startup_state(&self, state: StartupState) {
        self.startup_state.store(state.as_u8(), Ordering::Relaxed);
    }

    /// Begin a spawn attempt: stamp the start time, clear any prior error, and
    /// move to `Starting`. Call this before the actual spawn work.
    fn mark_starting(&self) {
        self.started_at_ms.store(now_ms(), Ordering::Relaxed);
        if let Ok(mut g) = self.last_error.lock() {
            *g = None;
        }
        self.set_startup_state(StartupState::Starting);
    }

    /// Record a bounded failure message and move to `Failed`.
    fn mark_failed(&self, message: &str) {
        if let Ok(mut g) = self.last_error.lock() {
            let mut msg = message.to_string();
            if msg.len() > MAX_LAST_ERROR_LEN {
                msg.truncate(MAX_LAST_ERROR_LEN);
            }
            *g = Some(msg);
        }
        self.set_startup_state(StartupState::Failed);
    }

    /// Milliseconds since the current spawn began (0 if never started).
    fn elapsed_ms(&self) -> u64 {
        let started = self.started_at_ms.load(Ordering::Relaxed);
        if started == 0 {
            0
        } else {
            now_ms().saturating_sub(started)
        }
    }

    fn last_error(&self) -> Option<String> {
        self.last_error.lock().ok().and_then(|g| g.clone())
    }
}

// --------------------------------------------------------------------------- //
// F147 S9b — single-instance ADOPTION.
//
// Before spawning, the app checks whether a healthy sidecar built from the SAME
// commit is already advertised in `${ERRORTA_HOME}/sidecar.json` (e.g. a
// CLI-started one). If so it ADOPTS that port instead of spawning a competing
// second sidecar (two sidecars on one store corrupt in-flight runs — §4.2). The
// governing rule is SAFE FALLBACK: adoption engages ONLY on a positive match
// (a build stamp we can read, a live /healthz, and an equal build commit); any
// uncertainty falls through to spawning our own sidecar — today's behavior.
// --------------------------------------------------------------------------- //

/// Our own build commit, stamped at compile time by `build.rs` (git HEAD, or
/// empty when git was unavailable). Empty ⇒ we cannot confirm a commit match, so
/// adoption is disabled and we always spawn (safe fallback).
fn own_build_commit() -> Option<String> {
    let c = env!("ERRORTA_BUILD_COMMIT").trim();
    if c.is_empty() {
        None
    } else {
        Some(c.to_string())
    }
}

/// `${ERRORTA_HOME}/sidecar.json` — the sidecar discovery file. Mirrors
/// `errorta_app.sidecar_advert.sidecar_json_path()` /
/// `errorta_cli.config.sidecar_record_path`.
fn sidecar_json_path() -> std::path::PathBuf {
    crate::paths::errorta_home().join("sidecar.json")
}

/// Directory holding one pidfile per live watchdog client. Mirrors
/// `errorta_app.parent_watchdog.clients_dir()`.
fn clients_dir() -> std::path::PathBuf {
    crate::paths::errorta_home().join("sidecar-clients")
}

/// Register THIS app process as a live watchdog client of the shared sidecar
/// (write a pidfile named/holding our pid, matching the S9a registry format), so
/// an adopted sidecar refcounts us and does not exit while we're using it.
/// Best-effort; a failure is non-fatal.
fn register_client_pidfile() {
    let dir = clients_dir();
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let pid = std::process::id();
    let _ = std::fs::write(dir.join(pid.to_string()), pid.to_string());
}

/// Remove this app's watchdog-client pidfile (a clean disconnect on app exit).
/// Best-effort; a missing file is fine.
pub fn unregister_client_pidfile() {
    let pid = std::process::id();
    let _ = std::fs::remove_file(clients_dir().join(pid.to_string()));
}

/// Read + parse `${ERRORTA_HOME}/sidecar.json`, or `None` if absent/unreadable/
/// corrupt (all of which mean "no discoverable sidecar" → spawn our own).
fn read_sidecar_advert() -> Option<serde_json::Value> {
    let bytes = std::fs::read(sidecar_json_path()).ok()?;
    serde_json::from_slice::<serde_json::Value>(&bytes).ok()
}

/// GET `/healthz` on loopback `port` and parse the JSON body. Bounded read; never
/// blocks app boot. `None` on any unreachable/slow/non-2xx/unparseable response
/// (→ we don't adopt, we spawn — safe fallback).
fn probe_healthz_json(port: u16) -> Option<serde_json::Value> {
    use std::io::{Read, Write};

    let host_port = format!("127.0.0.1:{port}");
    let addr = host_port.parse().ok()?;
    let mut stream =
        std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(500)).ok()?;
    stream
        .set_read_timeout(Some(Duration::from_millis(1000)))
        .ok()?;
    stream
        .set_write_timeout(Some(Duration::from_millis(500)))
        .ok()?;
    let req = format!(
        "GET /healthz HTTP/1.1\r\nHost: {host_port}\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).ok()?;

    let mut raw: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&chunk[..n]);
                if raw.len() > 64 * 1024 {
                    break; // cap — a pathological body can't bloat the probe
                }
            }
            Err(_) => break, // timeout / reset — parse whatever we have below
        }
    }

    let text = String::from_utf8_lossy(&raw);
    if !(text.starts_with("HTTP/1.1 2") || text.starts_with("HTTP/1.0 2")) {
        return None;
    }
    // Header/body split (headers are ASCII, so the byte index is stable).
    let idx = text.find("\r\n\r\n")?;
    let body = text[idx + 4..].trim();
    serde_json::from_str::<serde_json::Value>(body).ok()
}

/// Try to adopt an already-running, healthy, SAME-BUILD sidecar advertised on
/// disk. Returns `Some(port)` only on a positive confirmation; `None` (spawn our
/// own) on any uncertainty.
fn try_adopt_existing_sidecar() -> Option<u16> {
    // No build stamp ⇒ can't confirm a match ⇒ never adopt (safe fallback).
    let want = own_build_commit()?;
    let advert = read_sidecar_advert()?;
    let port = u16::try_from(advert.get("port")?.as_u64()?).ok()?;
    if port == 0 {
        return None;
    }
    let body = probe_healthz_json(port)?; // must be live + 2xx
    let their_commit = body.get("build")?.get("commit")?.as_str()?;
    if their_commit == want {
        Some(port)
    } else {
        None // version-skewed peer — don't co-drive it, spawn our own instead
    }
}

/// Allocate a free TCP port by binding to :0 and immediately releasing.
/// There is a small race window between drop and re-bind, but it's the same
/// trick most local dev tooling uses and is good enough here.
fn allocate_free_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0")
        .map_err(|e| format!("could not bind ephemeral port: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("could not read ephemeral local addr: {e}"))?
        .port();
    drop(listener);
    Ok(port)
}

/// Block until `/healthz` responds with 200 or the timeout elapses.
fn wait_for_healthz(port: u16) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{port}/healthz");
    let start = Instant::now();
    loop {
        if let Ok(stream) = std::net::TcpStream::connect_timeout(
            &format!("127.0.0.1:{port}")
                .parse()
                .map_err(|e: std::net::AddrParseError| e.to_string())?,
            Duration::from_millis(250),
        ) {
            // Once the port accepts TCP, do a minimal HTTP/1.1 GET to make sure
            // the FastAPI app is actually serving (not just uvicorn binding).
            drop(stream);
            if probe_http(&url).is_ok() {
                return Ok(());
            }
        }
        if start.elapsed() >= HEALTHZ_TIMEOUT {
            return Err(format!(
                "sidecar /healthz did not respond within {:?}",
                HEALTHZ_TIMEOUT
            ));
        }
        std::thread::sleep(HEALTHZ_POLL_INTERVAL);
    }
}

/// Minimal blocking HTTP/1.1 GET to avoid pulling in a full HTTP client dep.
/// Returns Ok(()) if status starts with `HTTP/1.1 2`.
fn probe_http(url: &str) -> Result<(), String> {
    // Parse out host:port/path; we know the shape (http://127.0.0.1:PORT/healthz).
    let rest = url.strip_prefix("http://").ok_or("not http")?;
    let (host_port, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let mut stream = std::net::TcpStream::connect_timeout(
        &host_port
            .parse()
            .map_err(|e: std::net::AddrParseError| e.to_string())?,
        Duration::from_millis(500),
    )
    .map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_millis(750)))
        .map_err(|e| e.to_string())?;
    use std::io::{Read, Write};
    let req = format!("GET {path} HTTP/1.1\r\nHost: {host_port}\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut buf = [0u8; 32];
    let n = stream.read(&mut buf).map_err(|e| e.to_string())?;
    let head = std::str::from_utf8(&buf[..n]).unwrap_or("");
    if head.starts_with("HTTP/1.1 2") || head.starts_with("HTTP/1.0 2") {
        Ok(())
    } else {
        Err(format!("non-2xx status line: {head:?}"))
    }
}

/// Spawn the sidecar (force). Stores the child + port in the managed handle on
/// success. If a sidecar is already running, terminate it first.
pub fn spawn_sidecar(app: &AppHandle) -> Result<u16, String> {
    let handle = app.state::<SidecarHandle>();
    // Hold spawn_lock across the entire terminate → allocate → spawn →
    // wait_for_healthz sequence so concurrent callers serialize cleanly.
    // We intentionally clear poisoning: if a prior spawn panicked, we still
    // want subsequent restarts to be able to run.
    let _spawn_guard = handle
        .spawn_lock
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    spawn_sidecar_locked(app, &handle)
}

/// State-tracking wrapper around `spawn_sidecar_inner`. Assumes the caller
/// holds `spawn_lock`. Stamps the F103 lifecycle states (`starting` →
/// `healthy`/`failed`) so the startup splash has honest copy. Both
/// `spawn_sidecar` and `ensure_sidecar` route through here.
fn spawn_sidecar_locked(app: &AppHandle, handle: &SidecarHandle) -> Result<u16, String> {
    handle.mark_starting();
    let result = spawn_sidecar_inner(app, handle);
    match &result {
        Ok(_) => handle.set_startup_state(StartupState::Healthy),
        Err(e) => handle.mark_failed(e),
    }
    result
}

/// The spawn body. Assumes the caller holds `spawn_lock` (so `spawn_sidecar`
/// and `ensure_sidecar` share it without re-entrant locking).
fn spawn_sidecar_inner(app: &AppHandle, handle: &SidecarHandle) -> Result<u16, String> {
    handle.terminate();

    // F147 S9b — single-instance ADOPTION. If a healthy sidecar built from the
    // SAME commit is already advertised in ${ERRORTA_HOME}/sidecar.json (e.g. a
    // CLI-started one), adopt its port instead of spawning a competing second
    // sidecar. Register as a watchdog client either way (so an adopted sidecar
    // refcounts us). Any uncertainty (no build stamp, no/stale/mismatched
    // advert, unreachable /healthz) falls through to spawning our own — the
    // safe fallback (worst case: concurrency doesn't engage, never two sidecars).
    if let Some(port) = try_adopt_existing_sidecar() {
        // We hold no child in the adopt case: `terminate()` on exit is a no-op,
        // and `ensure_sidecar` re-probes /healthz and spawns our own if the
        // adopted sidecar later dies.
        handle.set_port(port);
        register_client_pidfile();
        eprintln!(
            "[errorta] adopted existing sidecar on 127.0.0.1:{port} (single-instance)"
        );
        return Ok(port);
    }

    let port = allocate_free_port()?;
    let port_str = port.to_string();

    let cmd = app
        .shell()
        .sidecar(SIDECAR_BIN)
        .map_err(|e| format!("sidecar binary `{SIDECAR_BIN}` not found: {e}"))?
        .env("ERRORTA_SIDECAR_PORT", &port_str)
        // F063 A3: tell the sidecar our PID so it can self-exit if this shell
        // dies without cleanup (SIGKILL / crash / replaced on disk).
        .env("ERRORTA_PARENT_PID", std::process::id().to_string())
        // F147 S9b: stamp who spawned this sidecar so its /healthz + sidecar.json
        // advertisement report `started_by=app` (the CLI reads this to know it's
        // adopting the desktop app's shared sidecar).
        .env("ERRORTA_STARTED_BY", "app")
        .env(
            "ERRORTA_LOG_LEVEL",
            std::env::var("ERRORTA_LOG_LEVEL").unwrap_or_else(|_| "info".into()),
        );

    let (mut rx, child) = cmd
        .spawn()
        .map_err(|e| format!("could not spawn `{SIDECAR_BIN}`: {e}"))?;

    handle.replace_child(Some(child));
    handle.set_port(port);
    let generation = handle.next_generation();
    let exit_app = app.clone();

    // Drain the sidecar's stdout/stderr so the pipe never blocks. v0.1 just
    // forwards everything to the Rust shell's stderr — a structured log sink
    // is a v0.5 problem.
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    eprintln!("[sidecar/out] {}", String::from_utf8_lossy(&line).trim_end());
                }
                CommandEvent::Stderr(line) => {
                    eprintln!("[sidecar/err] {}", String::from_utf8_lossy(&line).trim_end());
                }
                CommandEvent::Error(err) => eprintln!("[sidecar/error] {err}"),
                CommandEvent::Terminated(payload) => {
                    eprintln!("[sidecar/terminated] code={:?}", payload.code);
                    // F063 A2: mark the handle dead so sidecar_port returns 0 —
                    // but only if this is still the current generation, so a
                    // restart's fresh child is never clobbered (even on a
                    // re-allocated same port).
                    exit_app
                        .state::<SidecarHandle>()
                        .clear_if_generation(generation);
                    break;
                }
                _ => {}
            }
        }
    });

    wait_for_healthz(port).inspect_err(|_e| {
        // If healthz failed, kill the child so we don't leak.
        handle.terminate();
    })?;

    // F147 S9b: register as a watchdog client of the sidecar we just spawned
    // (belt-and-suspenders alongside ERRORTA_PARENT_PID; symmetric with the
    // adopt path so a co-driving CLI + this app refcount the shared sidecar).
    register_client_pidfile();

    Ok(port)
}

/// Tauri command: returns the current sidecar port (0 if not running).
#[tauri::command]
pub fn sidecar_port(handle: tauri::State<'_, SidecarHandle>) -> u16 {
    handle.port()
}

/// Tauri command (F103): non-blocking lifecycle snapshot for the startup
/// splash. Reads atomics only — never waits on health — so the splash can poll
/// it cheaply and show honest "starting" vs "failed" copy.
#[tauri::command]
pub fn sidecar_startup_state(handle: tauri::State<'_, SidecarHandle>) -> serde_json::Value {
    serde_json::json!({
        "state": handle.startup_state().as_str(),
        "port": handle.port(),
        "elapsed_ms": handle.elapsed_ms(),
        "last_error": handle.last_error(),
    })
}

/// Tauri command: kill the running sidecar and respawn. Returns the new port.
#[tauri::command]
pub fn restart_sidecar(app: AppHandle) -> Result<u16, String> {
    spawn_sidecar(&app)
}

/// Tauri command (F063 A2): return a port that is guaranteed live. If the
/// current child is healthy, returns its port; otherwise respawns and returns
/// the new port. Called by the frontend self-heal on a transport failure so a
/// dead/changed sidecar recovers instead of stranding the UI on a dead port.
///
/// Double-checked under `spawn_lock`: concurrent self-heals don't spawn-kill-
/// spawn — the second caller sees the freshly-spawned healthy child and
/// returns its port.
#[tauri::command]
pub fn ensure_sidecar(app: AppHandle) -> Result<u16, String> {
    let handle = app.state::<SidecarHandle>();
    let _spawn_guard = handle
        .spawn_lock
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let port = handle.port();
    if port != 0 {
        let url = format!("http://127.0.0.1:{port}/healthz");
        if probe_http(&url).is_ok() {
            return Ok(port);
        }
    }
    spawn_sidecar_locked(&app, &handle)
}

/// Tauri command: cheap snapshot of managed child processes. v0.1 only owns
/// the sidecar; future Ollama supervisor will extend this.
#[tauri::command]
pub fn processes(handle: tauri::State<'_, SidecarHandle>) -> serde_json::Value {
    let port = handle.port();
    let running = port != 0;
    serde_json::json!({
        "processes": [
            {
                "role": "sidecar",
                "label": "errorta-sidecar",
                "port": port,
                "running": running,
            }
        ]
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_handle_is_not_started() {
        let h = SidecarHandle::new();
        assert_eq!(h.startup_state(), StartupState::NotStarted);
        assert_eq!(h.port(), 0);
        assert_eq!(h.elapsed_ms(), 0);
        assert_eq!(h.last_error(), None);
    }

    #[test]
    fn mark_starting_moves_to_starting_and_stamps_time() {
        let h = SidecarHandle::new();
        h.mark_starting();
        assert_eq!(h.startup_state(), StartupState::Starting);
        // elapsed should be measurable (>= 0) and last_error cleared.
        assert!(h.elapsed_ms() < 60_000, "elapsed should be near-zero on a fresh start");
        assert_eq!(h.last_error(), None);
    }

    #[test]
    fn successful_spawn_reports_healthy() {
        let h = SidecarHandle::new();
        h.mark_starting();
        h.set_startup_state(StartupState::Healthy);
        assert_eq!(h.startup_state(), StartupState::Healthy);
        assert_eq!(h.last_error(), None);
    }

    #[test]
    fn mark_failed_records_bounded_error() {
        let h = SidecarHandle::new();
        h.mark_starting();
        let long = "x".repeat(MAX_LAST_ERROR_LEN * 3);
        h.mark_failed(&long);
        assert_eq!(h.startup_state(), StartupState::Failed);
        let stored = h.last_error().expect("error should be stored");
        assert_eq!(stored.len(), MAX_LAST_ERROR_LEN);
    }

    #[test]
    fn mark_starting_clears_prior_error() {
        let h = SidecarHandle::new();
        h.mark_failed("boom");
        assert!(h.last_error().is_some());
        h.mark_starting();
        assert_eq!(h.last_error(), None);
        assert_eq!(h.startup_state(), StartupState::Starting);
    }

    #[test]
    fn terminated_only_for_current_generation() {
        let h = SidecarHandle::new();
        h.mark_starting();
        h.set_startup_state(StartupState::Healthy);
        let gen = h.next_generation();
        // A late Terminated from a PRIOR generation must not clobber state.
        h.clear_if_generation(gen - 1);
        assert_eq!(h.startup_state(), StartupState::Healthy);
        // The current generation's exit flips to Terminated and clears the port.
        h.set_port(12345);
        h.clear_if_generation(gen);
        assert_eq!(h.startup_state(), StartupState::Terminated);
        assert_eq!(h.port(), 0);
    }

    #[test]
    fn startup_state_round_trips_through_u8() {
        for s in [
            StartupState::NotStarted,
            StartupState::Starting,
            StartupState::Healthy,
            StartupState::Failed,
            StartupState::Terminated,
        ] {
            assert_eq!(StartupState::from_u8(s.as_u8()), s);
        }
        // Unknown discriminants decode to NotStarted (fail-safe).
        assert_eq!(StartupState::from_u8(99), StartupState::NotStarted);
    }
}
