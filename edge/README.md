# Errorta alpha check-in service (`edge/`)

The F-DIST-01 backend: a single Cloudflare Worker (`api.errorta.app`) that handles
**activate / heartbeat / metrics / feedback**. It is the only stateful component
of alpha delivery — installers and the updater feed are static on GitHub Pages and
never touch this Worker.

Spec: `../docs/specs/F-DIST-01-alpha-delivery-licensing-telemetry.md`
Plan: `../docs/superpowers/plans/2026-07-01-F-DIST-01-alpha-delivery.md` (slices 0–3)

This is deployed **manually** with `wrangler` (no GitHub Actions — locked off).

## Layout

```
edge/
  worker/            the Worker (TypeScript, dependency-free runtime)
    src/             token, codes, catalog, db seam, handlers, router
    test/            vitest unit tests (plain node; no workerd needed)
    schema.sql       D1 schema
    wrangler.toml    bindings + vars
  tools/
    gen-keypair.mjs  mint the Ed25519 license keypair (slice 0)
    mint-code.mjs    create an invite code in D1 (slice 1)
    mark-eol.mjs     retire a stale build (slice 8)
```

## Endpoints (`/v1/*`, JSON, no CORS, `X-Errorta-Client: errorta-desktop` required)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/health` | liveness |
| POST | `/v1/activate` | redeem a code, bind device, issue a signed token |
| POST | `/v1/heartbeat` | status (active/revoked/build_eol) + fresh token + floor counters |
| POST | `/v1/metrics` | batched opt-out extras (active devices; allowlisted dimensions only) |
| POST | `/v1/feedback` | multipart: redacted bundle -> R2 + a ticket row; bundle requests fail closed if R2 is unavailable |

Body caps: 64 KiB JSON; 8,000-character feedback message; 5 MiB feedback bundle.
Per-IP rate limiting for `/v1/activate` and `/v1/feedback` must be configured as
Cloudflare **edge rules**. It is external account state and is not established by
this repository or by `wrangler deploy`; verify it in the dashboard before launch.

## First-time deploy

```sh
cd edge/worker
npm install

# 1. D1
wrangler d1 create errorta-alpha                       # paste database_id into wrangler.toml
wrangler d1 execute errorta-alpha --file schema.sql

# 2. R2 (PRIVATE bucket — no public access; required for diagnostic bundles)
wrangler r2 bucket create errorta-alpha-feedback

# 3. License keypair
node ../tools/gen-keypair.mjs
#   -> PUBLIC  key: embed as LICENSE_PUBKEY_B64 in python/errorta_alpha/config.py
#   -> PRIVATE key: wrangler secret put LICENSE_SIGNING_KEY   (never commit)

# 4. Deploy + attach the custom domain api.errorta.app (Workers route)
wrangler deploy
```

## Day-2 ops

```sh
node ../tools/mint-code.mjs "wave 1 - alice" --max 1        # create + print an invite code
node ../tools/mint-code.mjs "team seat" --max 3 --expires 2026-09-01
node ../tools/mark-eol.mjs 0.6.0-alpha.2 --required         # force update off a bad build

# revoke a specific device (cut off a tester):
wrangler d1 execute errorta-alpha --command \
  "UPDATE licenses SET status='revoked', revoked_at=strftime('%s','now'), revoke_reason='left program' WHERE device_id='<uuid>';"
```

## Tests

```sh
cd edge/worker
npm test           # vitest — token wire format, codes, catalog, all four handlers
npm run typecheck  # tsc --noEmit
```

The token wire format is cross-checked against the Python sidecar verifier
(`errorta_alpha.token.verify`) — a JS-signed token must verify in Python, which is
the interop guarantee this whole scheme rests on.

## Prerequisites (external, not code)

- Cloudflare account + `api.errorta.app` subdomain (Workers route).
- `help@errorta.app` SPF/DKIM (F-INFRA-05 DNS) before mailing codes, or they spam.
- Wire the printed **public** key into `python/errorta_alpha/config.py`
  (`LICENSE_PUBKEY_B64`), replacing the slice-4 placeholder, before real tokens
  are issued.
