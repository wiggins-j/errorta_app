// F008-DIFF — Side-by-side markdown diff for brief history snapshots.
//
// Lightly stateful: tracks which header side has keyboard focus and renders an
// arrow glyph next to that header. The parent (BriefHistoryDropdown) owns
// snapshot resolution; arrow swaps only operate on the LEFT side per the
// BRIEF-DIFF-KBD spec. ArrowUp swaps to the previous-in-list (older) snapshot;
// ArrowDown swaps to the next-in-list (newer) snapshot; Escape closes.

import { useEffect, useState } from "react";

export const MAX_DIFF_LINES = 5000;

export type DiffRow = { kind: "eq" | "add" | "del"; text: string };

/**
 * Hand-rolled line-level LCS diff.
 *
 * Returns a single sentinel row when either side exceeds MAX_DIFF_LINES so we
 * don't allocate an O(n*m) DP table for pathological inputs.
 */
export function diffLines(a: string, b: string): DiffRow[] {
  const aLines = a.split("\n");
  const bLines = b.split("\n");
  if (aLines.length > MAX_DIFF_LINES || bLines.length > MAX_DIFF_LINES) {
    return [{ kind: "eq", text: "(diff too large to display)" }];
  }
  const n = aLines.length;
  const m = bLines.length;
  // dp[i][j] = LCS length of aLines[i:] and bLines[j:]
  const dp: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (aLines[i] === bLines[j]) {
        dp[i][j] = dp[i + 1][j + 1] + 1;
      } else {
        dp[i][j] = dp[i + 1][j] >= dp[i][j + 1] ? dp[i + 1][j] : dp[i][j + 1];
      }
    }
  }
  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (aLines[i] === bLines[j]) {
      rows.push({ kind: "eq", text: aLines[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ kind: "del", text: aLines[i] });
      i++;
    } else {
      rows.push({ kind: "add", text: bLines[j] });
      j++;
    }
  }
  while (i < n) {
    rows.push({ kind: "del", text: aLines[i] });
    i++;
  }
  while (j < m) {
    rows.push({ kind: "add", text: bLines[j] });
    j++;
  }
  return rows;
}

interface Props {
  leftMarkdown: string;
  rightMarkdown: string;
  leftTimestamp: string;
  rightTimestamp: string;
  onClose: () => void;
  // BRIEF-DIFF-KBD additions.
  canSwapLeftPrev?: boolean;
  canSwapLeftNext?: boolean;
  onSwapLeftPrev?: () => void;
  onSwapLeftNext?: () => void;
  onRestoreLeft?: () => void | Promise<void>;
  onRestoreRight?: () => void | Promise<void>;
  restoring?: "left" | "right" | null;
}

export default function BriefHistoryDiffView({
  leftMarkdown,
  rightMarkdown,
  leftTimestamp,
  rightTimestamp,
  onClose,
  canSwapLeftPrev = false,
  canSwapLeftNext = false,
  onSwapLeftPrev,
  onSwapLeftNext,
  onRestoreLeft,
  onRestoreRight,
  restoring = null,
}: Props) {
  const [focusedSide, setFocusedSide] = useState<"left" | "right">("left");

  // Document-level keydown handler mirroring RefreshDiffModal.tsx pattern.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key === "ArrowUp") {
        if (canSwapLeftPrev && onSwapLeftPrev) {
          e.preventDefault();
          e.stopPropagation();
          onSwapLeftPrev();
        }
        return;
      }
      if (e.key === "ArrowDown") {
        if (canSwapLeftNext && onSwapLeftNext) {
          e.preventDefault();
          e.stopPropagation();
          onSwapLeftNext();
        }
        return;
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [
    onClose,
    canSwapLeftPrev,
    canSwapLeftNext,
    onSwapLeftPrev,
    onSwapLeftNext,
  ]);

  const rows = diffLines(leftMarkdown, rightMarkdown);

  // Build paired rows so the left and right columns are aligned: a del-only
  // row pads the right column, an add-only row pads the left column. eq rows
  // appear in both columns. This guarantees row-index parity between cols.
  type Cell = { text: string; cls: string } | null;
  const left: Cell[] = [];
  const right: Cell[] = [];
  for (const r of rows) {
    if (r.kind === "eq") {
      left.push({ text: r.text, cls: "briefs-diff-line-eq" });
      right.push({ text: r.text, cls: "briefs-diff-line-eq" });
    } else if (r.kind === "del") {
      left.push({ text: r.text, cls: "briefs-diff-line-del" });
      right.push(null);
    } else {
      left.push(null);
      right.push({ text: r.text, cls: "briefs-diff-line-add" });
    }
  }

  const renderCol = (cells: Cell[], colLabel: string) => (
    <div className="briefs-diff-col" aria-label={colLabel}>
      {cells.map((cell, idx) =>
        cell === null ? (
          <div key={idx} className="briefs-diff-line-pad">
            {" "}
          </div>
        ) : (
          <div key={idx} className={cell.cls}>
            {cell.text === "" ? " " : cell.text}
          </div>
        ),
      )}
    </div>
  );

  const restoringLeft = restoring === "left";
  const restoringRight = restoring === "right";

  return (
    <div data-testid="briefs-diff-view" className="briefs-diff-root">
      <div className="briefs-diff-header">
        <button
          type="button"
          className="briefs-diff-header-left"
          onClick={() => setFocusedSide("left")}
          aria-label={`Focus left header ${leftTimestamp}`}
          data-focused={focusedSide === "left" ? "true" : "false"}
          style={{
            background: "transparent",
            border: "none",
            textAlign: "left",
            cursor: "pointer",
            padding: 0,
            font: "inherit",
            color: "inherit",
          }}
        >
          {focusedSide === "left" && (
            <span
              className="briefs-diff-focus-arrow"
              aria-hidden="true"
              data-testid="briefs-diff-focus-arrow-left"
            >
              {"▶ "}
            </span>
          )}
          <span aria-live="polite" data-testid="briefs-diff-left-ts">
            {leftTimestamp}
          </span>
        </button>
        <button
          type="button"
          className="briefs-diff-header-right"
          onClick={() => setFocusedSide("right")}
          aria-label={`Focus right header ${rightTimestamp}`}
          data-focused={focusedSide === "right" ? "true" : "false"}
          style={{
            background: "transparent",
            border: "none",
            textAlign: "left",
            cursor: "pointer",
            padding: 0,
            font: "inherit",
            color: "inherit",
          }}
        >
          {focusedSide === "right" && (
            <span
              className="briefs-diff-focus-arrow"
              aria-hidden="true"
              data-testid="briefs-diff-focus-arrow-right"
            >
              {"▶ "}
            </span>
          )}
          <span data-testid="briefs-diff-right-ts">{rightTimestamp}</span>
        </button>
      </div>
      <div className="briefs-diff-grid">
        {renderCol(left, `Snapshot ${leftTimestamp}`)}
        {renderCol(right, `Snapshot ${rightTimestamp}`)}
      </div>
      <div className="briefs-diff-actions">
        {onRestoreLeft && (
          <button
            type="button"
            className="briefs-diff-restore-btn"
            onClick={() => {
              void onRestoreLeft();
            }}
            disabled={restoringLeft}
            aria-label="Restore left snapshot"
          >
            {restoringLeft ? "Restoring…" : "Restore Left"}
          </button>
        )}
        {onRestoreRight && (
          <button
            type="button"
            className="briefs-diff-restore-btn"
            onClick={() => {
              void onRestoreRight();
            }}
            disabled={restoringRight}
            aria-label="Restore right snapshot"
          >
            {restoringRight ? "Restoring…" : "Restore Right"}
          </button>
        )}
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}
