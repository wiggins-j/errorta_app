#!/usr/bin/env node
// Mint the Ed25519 license signing keypair (slice 0).
//
//   node edge/tools/gen-keypair.mjs                 # prints both halves
//   node edge/tools/gen-keypair.mjs --set-secret    # sets the Worker secret
//                                                    # directly, prints ONLY the
//                                                    # public key (recommended)
//
// The private key must NEVER be committed or displayed. With --set-secret it is
// piped straight into `wrangler secret put LICENSE_SIGNING_KEY` and never printed,
// so it can't end up in shell history, a log, or a chat transcript. Run
// --set-secret from `edge/worker/` (where wrangler.toml lives) so the Worker
// name resolves. Embed the printed PUBLIC KEY in python/errorta_alpha/config.py.

import { spawnSync } from "node:child_process";

const setSecret = process.argv.includes("--set-secret");

const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const pubRaw = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
const pkcs8 = new Uint8Array(await crypto.subtle.exportKey("pkcs8", pair.privateKey));
const b64 = (u8) => Buffer.from(u8).toString("base64");
const pubB64 = b64(pubRaw);
const privB64 = b64(pkcs8);

if (setSecret) {
  // Pipe the private key into wrangler via stdin — it never touches argv, the
  // shell, or stdout.
  const res = spawnSync("npx", ["wrangler", "secret", "put", "LICENSE_SIGNING_KEY"], {
    input: privB64,
    stdio: ["pipe", "inherit", "inherit"],
  });
  if (res.status !== 0) {
    console.error("\nFAILED to set the Worker secret. Run this from edge/worker/ and retry.");
    process.exit(res.status ?? 1);
  }
  console.log("\n=== Ed25519 license keypair ===");
  console.log("Worker secret LICENSE_SIGNING_KEY set. The private key was not displayed.");
  console.log("\nPUBLIC KEY (base64 raw) — give this to embed as LICENSE_PUBKEY_B64:");
  console.log(pubB64);
} else {
  console.log("=== Ed25519 license keypair ===\n");
  console.log("PUBLIC KEY (base64 raw) — embed as LICENSE_PUBKEY_B64 in the app:");
  console.log(pubB64, "\n");
  console.log("PRIVATE KEY (base64 PKCS8) — set as the Worker secret, never commit:");
  console.log("  echo '" + privB64 + "' | wrangler secret put LICENSE_SIGNING_KEY\n");
  console.warn("Tip: `--set-secret` avoids ever printing the private key.");
}
