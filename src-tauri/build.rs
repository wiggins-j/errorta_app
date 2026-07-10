fn main() {
    // Forward the build-time TARGET triple (e.g. aarch64-apple-darwin,
    // x86_64-unknown-linux-gnu) into the compiled binary as TARGET_TRIPLE.
    // Used by `remote_sidecar::resolve_local_sidecar_path` to find the
    // bundled `errorta-sidecar-<triple>` binary on disk.
    let target = std::env::var("TARGET").unwrap_or_default();
    println!("cargo:rustc-env=TARGET_TRIPLE={target}");
    println!("cargo:rerun-if-env-changed=TARGET");

    tauri_build::build()
}
