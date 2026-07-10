#!/usr/bin/env node
// Retire a stale build (slice 8). Marks a build EOL so the next heartbeat from a
// device on that version returns status:build_eol.
//
//   node edge/tools/mark-eol.mjs <build_id> [--required] [--url https://errorta.app/dl] [--print-sql]
//
// --required forces a hard update (required:true); otherwise it's a soft nudge.

import { execFileSync } from "node:child_process";

const buildId = process.argv[2];
if (!buildId || buildId.startsWith("--")) {
  console.error("usage: mark-eol.mjs <build_id> [--required] [--url URL] [--print-sql]");
  process.exit(1);
}
function arg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const required = process.argv.includes("--required") ? 1 : 0;
const url = arg("--url", "https://errorta.app/download");
const now = Math.floor(Date.now() / 1000);
const sqlString = (value) => `'${String(value).replace(/'/g, "''")}'`;

// Upsert: create the build row if absent, else mark it EOL now.
const sql =
  `INSERT INTO builds (build_id, released_at, eol_at, eol_required, update_url) ` +
  `VALUES (${sqlString(buildId)}, ${now}, ${now}, ${required}, ${sqlString(url)}) ` +
  `ON CONFLICT(build_id) DO UPDATE SET eol_at=${now}, eol_required=${required}, update_url=${sqlString(url)};`;

if (process.argv.includes("--print-sql")) {
  console.log(sql);
} else {
  execFileSync(
    "npx",
    ["wrangler", "d1", "execute", "errorta-alpha", "--remote", "--command", sql],
    { stdio: "inherit" },
  );
  console.log(`\nMarked ${buildId} EOL (required=${required === 1}).`);
}
