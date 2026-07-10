// F008 Briefs — shared types for the briefs feature pane.

export type BriefStateValue =
  | "DRAFT"
  | "VALIDATING"
  | "RUNNING"
  | "PAUSED"
  | "COMPLETED"
  | "FAILED"
  | "ARCHIVED";

export const TERMINAL_STATES: ReadonlySet<BriefStateValue> = new Set([
  "COMPLETED",
  "FAILED",
  "ARCHIVED",
]);

/**
 * Mirror of `errorta_briefs.lifecycle.LIFECYCLE_TRANSITIONS`. Keep in sync with
 * the Python FSM — the table itself never moves so duplication is cheap and
 * lets the UI gate buttons without a round-trip.
 */
export const LIFECYCLE_TRANSITIONS: Record<BriefStateValue, ReadonlySet<BriefStateValue>> = {
  DRAFT: new Set(["VALIDATING"]),
  VALIDATING: new Set(["DRAFT", "RUNNING", "FAILED"]),
  RUNNING: new Set(["PAUSED", "COMPLETED", "FAILED"]),
  PAUSED: new Set(["RUNNING", "ARCHIVED", "FAILED"]),
  COMPLETED: new Set(["RUNNING", "ARCHIVED"]),
  FAILED: new Set(["DRAFT", "ARCHIVED"]),
  ARCHIVED: new Set(),
};
