// Invite code format: ERRT-XXXX-XXXX using a Crockford-style base32 alphabet
// with the visually ambiguous letters (I, L, O, U) removed.

export const CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"; // no I L O U
const GROUP = 4;
const GROUPS = 2; // XXXX-XXXX after the ERRT- prefix
const CODE_RE = /^ERRT-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$/;

/** Uppercase + trim; callers compare/store the normalized form. */
export function normalizeCode(raw: string): string {
  return (raw || "").trim().toUpperCase();
}

export function isValidCodeFormat(code: string): boolean {
  return CODE_RE.test(code);
}

/**
 * Generate a code from a byte source (crypto.getRandomValues in the tool). Each
 * character consumes one random byte via rejection-free modulo over the 32-char
 * alphabet (32 divides 256 evenly, so modulo is unbiased here).
 */
export function generateCode(randomBytes: Uint8Array): string {
  const need = GROUP * GROUPS;
  if (randomBytes.length < need) throw new Error(`need >= ${need} random bytes`);
  const chars: string[] = [];
  for (let i = 0; i < need; i++) chars.push(CODE_ALPHABET[randomBytes[i] % 32]);
  const groups: string[] = [];
  for (let g = 0; g < GROUPS; g++) groups.push(chars.slice(g * GROUP, g * GROUP + GROUP).join(""));
  return `ERRT-${groups.join("-")}`;
}
