# Security

## Reporting a vulnerability

- Primary channel: email <help@errorta.app>.
- Alternative (if you prefer to keep the report inside GitHub):
  the private vulnerability report flow at
  <https://github.com/wiggins-j/errorta_app/security/advisories/new>.
- A PGP key is intentionally not published in v1.0; if you need
  encrypted transit, propose a key in your initial report and
  the maintainer will respond with a key exchange.

## Acknowledgement window

- 48 hours to first human response.

## Scope

Errorta is a **local-first** desktop app: the core AI functionality
(retrieval, judging, grounding, corrections, Council, the Coding Team)
runs on your machine and against the AIAR instance you point it at. Your
documents and corpora never leave your machine as part of normal use.

Errorta does, however, operate **one** small hosted component, and it is
in scope:

- **The alpha check-in service (`api.errorta.app`).** A Cloudflare Worker
  (source in [`edge/`](edge/)) used **only** by builds compiled with the
  alpha gate (`ERRORTA_ALPHA_GATE=1`). It handles invite-code activation,
  device-bound license heartbeats, opt-out usage metrics, and redacted
  feedback bundles (D1 + a private R2 bucket). A **normal (non-gated)
  build never contacts it** — that is the permanent v1.0 posture. What the
  gated build sends: a disclosed telemetry floor (event *counts*, no
  document or prompt content) plus opt-out extras.
- **In scope**: the Tauri shell, the Python sidecar (`errorta_app.*`),
  the React frontend, the export / import bundle codepaths, the
  brief-collection compliance gate, the tool sandbox (`errorta_tools`),
  the mobile LAN listener + pairing (`errorta_mobile`), the SSH-remote /
  residency codepaths, and the alpha check-in Worker (`edge/`).
- **Out of scope**: vulnerabilities in Ollama, the user's local OS, the
  model providers you configure, or third-party SourceConnector
  endpoints. Report those to the upstream projects directly.

## Notes on the local attack surface

Errorta is a desktop app that can run code-executing agents and reach the
network on your behalf. Mitigations in place: the sidecar binds to
`127.0.0.1` only; CORS origins are enumerated, not wildcarded; the mobile
LAN listener is off by default and requires owner-approved, TLS-pinned
pairing; tool `code_exec` runs under a per-platform hardened sandbox
(seatbelt / bubblewrap / Docker); and web fetches are SSRF-guarded. The
Tauri webview is granted `shell`/`fs` capabilities and `unsafe-eval` in
its CSP to support these features — a documented, known-broad surface we
intend to narrow toward scoped Rust commands over time. Reports that
exploit this surface are welcome and in scope.
