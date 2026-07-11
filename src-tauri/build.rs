fn main() {
    // Forward the build-time TARGET triple (e.g. aarch64-apple-darwin,
    // x86_64-unknown-linux-gnu) into the compiled binary as TARGET_TRIPLE.
    // Used by `remote_sidecar::resolve_local_sidecar_path` to find the
    // bundled `errorta-sidecar-<triple>` binary on disk.
    let target = std::env::var("TARGET").unwrap_or_default();
    println!("cargo:rustc-env=TARGET_TRIPLE={target}");
    println!("cargo:rerun-if-env-changed=TARGET");

    // F147 S9b — stamp the build commit (git HEAD) so the running app can
    // POSITIVELY confirm an already-running sidecar advertised in
    // ${ERRORTA_HOME}/sidecar.json is the SAME build before ADOPTING it
    // (single-instance adoption). This matches the full HEAD that
    // scripts/build-sidecar.sh stamps into the sidecar's _build_info.json, so a
    // normal `rebuild-app.sh` (which builds both from one clean HEAD) makes the
    // two commits equal. `ERRORTA_BUILD_COMMIT` env wins if a build sets it.
    // Empty (a source tarball with no git) ⇒ the app can't confirm a match and
    // therefore never adopts — it always spawns its own sidecar (safe fallback).
    let commit = std::env::var("ERRORTA_BUILD_COMMIT")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .or_else(|| {
            std::process::Command::new("git")
                .args(["rev-parse", "HEAD"])
                .output()
                .ok()
                .filter(|o| o.status.success())
                .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
                .filter(|s| !s.is_empty())
        })
        .unwrap_or_default();
    println!("cargo:rustc-env=ERRORTA_BUILD_COMMIT={commit}");
    println!("cargo:rerun-if-env-changed=ERRORTA_BUILD_COMMIT");

    tauri_build::build()
}
