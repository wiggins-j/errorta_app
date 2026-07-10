#!/usr/bin/env node
// Mint an invite code and insert it into D1 (slice 1). Maintainer-only; the
// tester-facing site has no auth surface. The person<->code mapping is your
// private admin data — keep it out of D1.
//
//   node edge/tools/mint-code.mjs "<label>" [--max N] [--expires YYYY-MM-DD] [--print-sql]
//
// By default it shells out to `wrangler d1 execute errorta-alpha`. With
// --print-sql it only prints the INSERT so you can run it however you like.
// Email delivery is intentionally out of this script: paste the printed code
// into your transactional-email flow (help@errorta.app; needs SPF/DKIM — see
// ../README.md), so a missing mail credential never blocks code creation.

import { execFileSync } from "node:child_process";

// Keep in lockstep with worker/src/codes.ts (source of truth for the format).
const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"; // no I L O U
function genCode() {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  const c = [...bytes].map((b) => ALPHABET[b % 32]);
  return `ERRT-${c.slice(0, 4).join("")}-${c.slice(4, 8).join("")}`;
}

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
function flag(name) {
  return process.argv.includes(name);
}

const label = process.argv[2] && !process.argv[2].startsWith("--") ? process.argv[2] : "";
const max = Number.parseInt(arg("--max", "1"), 10) || 1;
const expiresRaw = arg("--expires");
const expiresAt = expiresRaw ? Math.floor(new Date(expiresRaw + "T00:00:00Z").getTime() / 1000) : null;
const now = Math.floor(Date.now() / 1000);
const code = genCode();

const sql =
  `INSERT INTO invite_codes (code, label, max_activations, activations, created_at, expires_at, disabled) ` +
  `VALUES ('${code}', ${label ? `'${label.replace(/'/g, "''")}'` : "NULL"}, ${max}, 0, ${now}, ` +
  `${expiresAt ?? "NULL"}, 0);`;

if (flag("--print-sql")) {
  console.log(sql);
} else {
  execFileSync(
    "npx",
    ["wrangler", "d1", "execute", "errorta-alpha", "--remote", "--command", sql],
    { stdio: "inherit" },
  );
  console.log(`\nMinted code: ${code}  (max_activations=${max}${expiresRaw ? `, expires ${expiresRaw}` : ""})`);
  console.log("Send this code to the approved tester by email.");
}
