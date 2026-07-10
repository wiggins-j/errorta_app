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

- Errorta is a local-first desktop app. The Errorta project does
  not operate any server component on behalf of users.
- **Out of scope** for Errorta security reports: vulnerabilities
  in Ollama, the user's local OS, or third-party SourceConnector
  endpoints. Report those to the upstream projects directly.
- **In scope**: vulnerabilities in the Tauri shell, the Python
  sidecar (`errorta_app.*`), the React frontend, the export /
  import bundle codepaths, and the brief-collection compliance
  gate.
