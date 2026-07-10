// F109 — one-shot pending-prompt handoff.
//
// The welcome "Suggested prompt" Run button deep-links to the Judge feature via
// the `errorta:navigate` event carrying a `prompt`. App.tsx stashes that prompt
// here; the judge feature consumes it exactly once on mount and clears it, so a
// later navigation to Judge (by itself) never re-prefills a stale prompt.
//
// Module-level (not a persistent global / sessionStorage) so there is no storage
// cleanup and the value cannot survive a reload.

let pending: string | null = null;

/** Stash a prompt to be consumed once by the judge feature on its next mount. */
export function setPendingPrompt(prompt: string): void {
  pending = prompt;
}

/**
 * Read-and-clear the pending prompt. Returns null when nothing is pending (e.g.
 * a plain navigation to Judge, or a second mount after the value was consumed).
 */
export function consumePendingPrompt(): string | null {
  const value = pending;
  pending = null;
  return value;
}
