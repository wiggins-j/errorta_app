// F-DIST-01 — shared sanitizer for the server-supplied `update_url`.
// The value arrives from a check-in response and must never be rendered into an
// href without a scheme check: a malicious or MITM'd response could otherwise
// inject a `javascript:` / `data:` URL. Only https is allowed through.
export function safeUpdateUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}
