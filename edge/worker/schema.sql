-- F-DIST-01 check-in service — D1 schema (spec §4).
-- Apply with: wrangler d1 execute errorta-alpha --file schema.sql
--
-- No PII: the person<->code mapping lives only in the maintainer's private admin
-- notes (spec §16), never here. device_id is the app-generated anonymous UUID.

-- Invite codes minted by the maintainer.
CREATE TABLE IF NOT EXISTS invite_codes (
  code            TEXT PRIMARY KEY,          -- "ERRT-7F3K-9Q2M" (Crockford base32, dash-grouped)
  label           TEXT,                      -- free note ("twitter wave 1", tester name)
  max_activations INTEGER NOT NULL DEFAULT 1,
  activations     INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER NOT NULL,          -- epoch seconds UTC
  expires_at      INTEGER,                   -- code redemption deadline (NULL = none)
  disabled        INTEGER NOT NULL DEFAULT 0 -- 1 = cannot be redeemed further
);

-- One row per activated device (a "seat"). Grace lives in the signed token, not
-- here — the server just re-issues now+GRACE_DAYS on each check-in.
CREATE TABLE IF NOT EXISTS licenses (
  device_id       TEXT PRIMARY KEY,          -- app-generated UUIDv4
  code            TEXT NOT NULL REFERENCES invite_codes(code),
  status          TEXT NOT NULL DEFAULT 'active', -- active | revoked
  platform        TEXT,                      -- "macos-arm64" etc.
  app_version     TEXT,                      -- last-seen build
  activated_at    INTEGER NOT NULL,
  last_seen_at    INTEGER,                   -- last successful heartbeat
  revoked_at      INTEGER,
  revoke_reason   TEXT
);

-- Build registry drives stale-build retirement. eol_required=1 forces update.
CREATE TABLE IF NOT EXISTS builds (
  build_id        TEXT PRIMARY KEY,          -- app_version string, e.g. "0.6.0-alpha.3"
  released_at     INTEGER NOT NULL,
  eol_at          INTEGER,                   -- past this -> heartbeat returns build_eol
  eol_required    INTEGER NOT NULL DEFAULT 0,-- 1 -> required:true (hard update)
  update_url      TEXT
);

-- Aggregatable metrics events (Tier-1 floor + Tier-2 extras). Names only.
CREATE TABLE IF NOT EXISTS metrics_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id       TEXT NOT NULL,
  event           TEXT NOT NULL,             -- from the allowlisted catalog
  count           INTEGER NOT NULL DEFAULT 1,
  bucket          TEXT,                      -- e.g. latency bucket label; NEVER content
  app_version     TEXT,
  platform        TEXT,
  received_at     INTEGER NOT NULL,
  tier            TEXT NOT NULL              -- 'floor' | 'extra'
);
CREATE INDEX IF NOT EXISTS idx_metrics_device ON metrics_events(device_id);

-- Feedback tickets (redacted bundle bytes in R2, keyed by bundle_r2_key).
CREATE TABLE IF NOT EXISTS feedback (
  ticket_id       TEXT PRIMARY KEY,          -- uuid
  device_id       TEXT,                      -- optional; tester may report unbound
  kind            TEXT NOT NULL,             -- 'crash' | 'suggestion' | 'bug'
  message         TEXT,                      -- freeform, tester-authored
  bundle_r2_key   TEXT,                      -- redacted diagnostic bundle in R2 (private bucket)
  app_version     TEXT,
  created_at      INTEGER NOT NULL
);
