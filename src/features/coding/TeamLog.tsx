import { useCallback, useEffect, useState } from "react";

import { getTeamLog, type TeamLogEntry } from "../../lib/api/coding";

function formatTime(at: string): string {
  // The ledger stamps ISO-8601; show local HH:MM:SS, fall back to the raw value.
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return at;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// Short role tag shown as a colored badge before each entry.
const ROLE_TAG: Record<string, string> = {
  pm: "PM",
  dev: "DEV",
  reviewer: "REV",
  tester: "TEST",
  system: "SYS",
  // F105: a human edit is the user, not a team member.
  user: "YOU",
};

function roleTag(role: string): string {
  return ROLE_TAG[role] || (role || "—").toUpperCase();
}

export interface TeamLogProps {
  projectId: string;
}

/**
 * A human-readable, chronological narrative of what the Coding Team did
 * (North Star → specs/plans/approvals → tasks → dev/review/test/merge),
 * rendered as a collapsed-by-default panel. Self-contained: fetches its own
 * data and refreshes while open so it stays current during a live run.
 */
export default function TeamLog({ projectId }: TeamLogProps) {
  const [entries, setEntries] = useState<TeamLogEntry[]>([]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setEntries(await getTeamLog(projectId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll only while expanded so a collapsed panel costs nothing.
  useEffect(() => {
    if (!open) return undefined;
    const id = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(id);
  }, [open, load]);

  return (
    <details
      className="coding-panel coding-team-log"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>
        <span>Team Log</span>
        <span className="coding-count">{entries.length}</span>
      </summary>
      <section aria-label="Team Log">
        {error ? (
          <p className="coding-error" role="alert">
            {error}
          </p>
        ) : null}
        {entries.length === 0 ? (
          <p className="coding-empty">No team activity yet.</p>
        ) : (
          <ol className="coding-team-log-list">
            {/* Newest on top: render the chronological ledger in reverse. */}
            {entries.slice().reverse().map((entry, idx) => (
              <li
                key={`${entry.at}-${idx}`}
                className={`coding-tl-item coding-tl-${entry.role || "system"}`}
              >
                <time className="coding-tl-time" dateTime={entry.at}>
                  {formatTime(entry.at)}
                </time>
                <span className={`coding-tl-tag coding-tl-tag-${entry.role || "system"}`}>
                  {roleTag(entry.role)}
                </span>
                {entry.member ? (
                  <span className="coding-tl-name">{entry.member}</span>
                ) : null}
                <span className="coding-tl-msg">{entry.message}</span>
              </li>
            ))}
          </ol>
        )}
      </section>
    </details>
  );
}
