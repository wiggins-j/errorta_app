// Ed25519 license token signing.
//
// Wire format MUST match the sidecar verifier (python/errorta_alpha/token.py):
//   token = base64url(canonicalJSON(payload)) + "." + base64url(signature)
//   canonicalJSON = JSON with sorted keys and no whitespace
//                   (python json.dumps(sort_keys=True, separators=(",",":")))
//   signature     = Ed25519 over the ASCII bytes of the base64url payload string
// base64url is unpadded on both sides.

export interface LicensePayload {
  v: number;
  device_id: string;
  code: string;
  issued_at: number;
  grace_until: number;
  program: string;
  build_channel: string;
}

function b64urlEncode(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Canonical JSON: sorted top-level keys, no whitespace — matches the sidecar. */
export function canonicalJson(payload: object): string {
  return JSON.stringify(payload, Object.keys(payload).sort());
}

/** Import a base64 PKCS8 Ed25519 private key (the Worker's LICENSE_SIGNING_KEY). */
export async function importSigningKey(pkcs8Base64: string): Promise<CryptoKey> {
  const raw = Uint8Array.from(atob(pkcs8Base64), (c) => c.charCodeAt(0));
  return crypto.subtle.importKey("pkcs8", raw, { name: "Ed25519" }, false, ["sign"]);
}

/** Sign a payload and return the compact token string. */
export async function signToken(
  payload: LicensePayload,
  signingKey: CryptoKey,
): Promise<string> {
  const payloadB64 = b64urlEncode(new TextEncoder().encode(canonicalJson(payload)));
  const sig = await crypto.subtle.sign(
    { name: "Ed25519" },
    signingKey,
    new TextEncoder().encode(payloadB64),
  );
  return `${payloadB64}.${b64urlEncode(new Uint8Array(sig))}`;
}

/** Build the standard alpha license payload. */
export function makePayload(
  deviceId: string,
  code: string,
  now: number,
  graceDays: number,
): LicensePayload {
  return {
    v: 1,
    device_id: deviceId,
    code,
    issued_at: now,
    grace_until: now + graceDays * 86400,
    program: "alpha",
    build_channel: "alpha",
  };
}
