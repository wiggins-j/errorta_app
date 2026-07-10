/**
 * F143: compact token count for display — 12,345 / 1.2M. Keeps small numbers
 * exact (they read as real spend) and abbreviates large ones so a headline total
 * doesn't wrap. A pure formatter with no dependencies, deliberately separate from
 * the API client so tests that mock the client still get a real implementation.
 */
export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "0";
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return n.toLocaleString("en-US");
  // Promote to the next unit when one-decimal rounding would spill past 999.9
  // (e.g. 999,999,999 must read "1.0B", not "1000.0M").
  const millions = n / 1_000_000;
  if (millions < 999.95) return `${millions.toFixed(1)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}
