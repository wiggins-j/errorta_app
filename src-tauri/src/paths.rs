//! Data-residency path resolver for the Tauri shell.
//!
//! F-INFRA-12 Phase B Slice 4 — the Rust shell needs to read (and later, in
//! Slice 5, write) `data-residency.json` on the laptop. It must agree byte-for-
//! byte with `errorta_app.paths.errorta_home()` on identical inputs:
//!
//!   1. `ERRORTA_HOME` if set and non-empty (after trim).
//!   2. Otherwise, `$HOME/.errorta` on Unix or `%USERPROFILE%\.errorta` on
//!      Windows.
//!
//! The Python module additionally consults two legacy env vars
//! (`ERRORTA_STATE_DIR`, `ERRORTA_DATA_DIR`) and logs a one-time deprecation
//! warning. The Rust shell only needs to mirror the *canonical* contract;
//! legacy fallback stays Python-side because the shell never made promises
//! about those vars in any prior version.
//!
//! A missing `data-residency.json` is NOT an error — operators install
//! Errorta and the file only appears after Settings → Apply.

use std::fs;
use std::io;
use std::path::PathBuf;

use serde::{Deserialize, Serialize, Serializer};

const CANONICAL_ENV: &str = "ERRORTA_HOME";

/// Return the base directory Errorta writes data into. Creates it on demand.
pub fn errorta_home() -> PathBuf {
    let base = match std::env::var(CANONICAL_ENV) {
        Ok(raw) if !raw.trim().is_empty() => PathBuf::from(raw.trim()),
        _ => default_home(),
    };
    if let Err(e) = fs::create_dir_all(&base) {
        eprintln!(
            "[errorta] could not create errorta_home {}: {e}",
            base.display()
        );
    }
    base
}

#[cfg(unix)]
fn default_home() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    if home.is_empty() {
        // Last-resort fallback so we never return an empty path. The
        // create_dir_all upstream will report the failure.
        PathBuf::from("/.errorta")
    } else {
        PathBuf::from(home).join(".errorta")
    }
}

#[cfg(windows)]
fn default_home() -> PathBuf {
    let profile = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_default();
    if profile.is_empty() {
        PathBuf::from(".errorta")
    } else {
        PathBuf::from(profile).join(".errorta")
    }
}

/// Path to the persisted residency config. Mirrors
/// `errorta_app.paths.data_residency_path()`.
pub fn data_residency_path() -> PathBuf {
    errorta_home().join("data-residency.json")
}

// ---------------------------------------------------------------------------
// ResidencyState — schema must match the Python dataclass at
// errorta_residency.config.ResidencyState exactly.
// ---------------------------------------------------------------------------

fn default_ssh_port() -> u16 {
    22
}

/// Serializer used for `cloud_token` — the token never lands on disk, even if
/// it lives in process memory. Matches `errorta_residency.config._save`.
fn serialize_token_as_null<S>(_value: &Option<String>, serializer: S) -> Result<S::Ok, S::Error>
where
    S: Serializer,
{
    serializer.serialize_none()
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "kebab-case")]
pub enum ResidencyMode {
    #[default]
    Local,
    SshRemote,
    Cloud,
}

impl ResidencyMode {
    /// Kebab-case string form, matching the Python `Literal` values. Used by
    /// the shell's startup log line.
    pub fn as_kebab(&self) -> &'static str {
        match self {
            ResidencyMode::Local => "local",
            ResidencyMode::SshRemote => "ssh-remote",
            ResidencyMode::Cloud => "cloud",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResidencyState {
    #[serde(default)]
    pub mode: ResidencyMode,

    #[serde(default)]
    pub ssh_host: Option<String>,

    #[serde(default = "default_ssh_port")]
    pub ssh_port: u16,

    #[serde(default)]
    pub ssh_key_path: Option<String>,

    #[serde(default)]
    pub ssh_username: Option<String>,

    #[serde(default)]
    pub remote_sidecar_port: Option<u16>,

    #[serde(default, skip_serializing, skip_deserializing)]
    pub local_tunnel_port: Option<u16>,

    #[serde(default)]
    pub cloud_url: Option<String>,

    // Always written as null on disk. In-memory value may be Some(_) after
    // Slice 5's set_residency command lands.
    #[serde(default, serialize_with = "serialize_token_as_null")]
    pub cloud_token: Option<String>,

    #[serde(default)]
    pub updated_at: Option<String>,
}

impl Default for ResidencyState {
    fn default() -> Self {
        Self {
            mode: ResidencyMode::default(),
            ssh_host: None,
            ssh_port: default_ssh_port(),
            ssh_key_path: None,
            ssh_username: None,
            remote_sidecar_port: None,
            local_tunnel_port: None,
            cloud_url: None,
            cloud_token: None,
            updated_at: None,
        }
    }
}

// ---------------------------------------------------------------------------
// Error types
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub enum ResidencyReadError {
    /// File exists but the bytes are not valid JSON or don't match the schema.
    Malformed(String),
    /// Any IO error other than "file does not exist".
    Io(String),
}

impl std::fmt::Display for ResidencyReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ResidencyReadError::Malformed(m) => write!(f, "malformed residency config: {m}"),
            ResidencyReadError::Io(m) => write!(f, "residency config IO error: {m}"),
        }
    }
}

impl std::error::Error for ResidencyReadError {}

#[derive(Debug)]
pub enum ResidencyWriteError {
    Io(String),
    Serde(String),
}

impl std::fmt::Display for ResidencyWriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ResidencyWriteError::Io(m) => write!(f, "residency write IO error: {m}"),
            ResidencyWriteError::Serde(m) => write!(f, "residency write serde error: {m}"),
        }
    }
}

impl std::error::Error for ResidencyWriteError {}

// ---------------------------------------------------------------------------
// Read / write
// ---------------------------------------------------------------------------

/// Read the persisted residency state.
///
/// Returns `Ok(ResidencyState::default())` if the file does not exist (the
/// operator just installed Errorta and never visited Settings). Returns
/// `Err(Malformed)` if the file exists but doesn't parse; the caller chooses
/// whether to surface or fall back. Returns `Err(Io)` for any other IO
/// failure (permission denied, etc.).
pub fn read_residency() -> Result<ResidencyState, ResidencyReadError> {
    let path = data_residency_path();
    let bytes = match fs::read(&path) {
        Ok(b) => b,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            return Ok(ResidencyState::default());
        }
        Err(e) => return Err(ResidencyReadError::Io(e.to_string())),
    };
    let text = std::str::from_utf8(&bytes)
        .map_err(|e| ResidencyReadError::Malformed(format!("invalid utf-8: {e}")))?;
    serde_json::from_str::<ResidencyState>(text)
        .map_err(|e| ResidencyReadError::Malformed(e.to_string()))
}

/// Atomic JSON write: serialize → tmp sibling → rename. Mirrors the Python
/// `errorta_residency.config._save` pattern.
pub fn write_residency(state: &ResidencyState) -> Result<(), ResidencyWriteError> {
    let path = data_residency_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| ResidencyWriteError::Io(e.to_string()))?;
    }
    let json = serde_json::to_string_pretty(state)
        .map_err(|e| ResidencyWriteError::Serde(e.to_string()))?;
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, json).map_err(|e| ResidencyWriteError::Io(e.to_string()))?;
    fs::rename(&tmp, &path).map_err(|e| ResidencyWriteError::Io(e.to_string()))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Managed-state newtype — wraps the residency state in a Mutex so Slice 5's
// set_residency command can mutate it through Tauri's State<_> machinery.
// ---------------------------------------------------------------------------

/// Tauri-managed wrapper around the active residency state. Slice 5 will add
/// the `set_residency` / `get_residency` commands on top of this.
pub struct ResidencyStore {
    inner: std::sync::Mutex<ResidencyState>,
}

impl ResidencyStore {
    pub fn new(state: ResidencyState) -> Self {
        Self {
            inner: std::sync::Mutex::new(state),
        }
    }

    #[allow(dead_code)] // wired up in Slice 5
    pub fn snapshot(&self) -> ResidencyState {
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .clone()
    }

    pub fn replace(&self, new_state: ResidencyState) {
        let mut guard = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        *guard = new_state;
    }
}
