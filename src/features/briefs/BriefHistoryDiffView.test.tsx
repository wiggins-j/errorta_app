import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import BriefHistoryDiffView, {
  diffLines,
  MAX_DIFF_LINES,
} from "./BriefHistoryDiffView";

describe("diffLines", () => {
  it("returns all eq rows for identical inputs", () => {
    const rows = diffLines("a\nb\nc", "a\nb\nc");
    expect(rows).toEqual([
      { kind: "eq", text: "a" },
      { kind: "eq", text: "b" },
      { kind: "eq", text: "c" },
    ]);
  });

  it("returns only adds when left is empty", () => {
    const rows = diffLines("", "x\ny");
    // "" splits to [""], so the empty line matches nothing -> del then adds.
    // Filter to additions for the assertion that adds appear.
    expect(rows.filter((r) => r.kind === "add")).toEqual([
      { kind: "add", text: "x" },
      { kind: "add", text: "y" },
    ]);
  });

  it("returns only dels when right is empty", () => {
    const rows = diffLines("x\ny", "");
    expect(rows.filter((r) => r.kind === "del")).toEqual([
      { kind: "del", text: "x" },
      { kind: "del", text: "y" },
    ]);
  });

  it("handles add-only relative to a shared prefix", () => {
    const rows = diffLines("a\nb", "a\nb\nc");
    expect(rows).toEqual([
      { kind: "eq", text: "a" },
      { kind: "eq", text: "b" },
      { kind: "add", text: "c" },
    ]);
  });

  it("handles del-only relative to a shared prefix", () => {
    const rows = diffLines("a\nb\nc", "a\nb");
    expect(rows).toEqual([
      { kind: "eq", text: "a" },
      { kind: "eq", text: "b" },
      { kind: "del", text: "c" },
    ]);
  });

  it("handles mixed add + del", () => {
    const rows = diffLines("a\nb\nc", "a\nX\nc");
    expect(rows).toEqual([
      { kind: "eq", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "X" },
      { kind: "eq", text: "c" },
    ]);
  });

  it("handles both empty inputs", () => {
    const rows = diffLines("", "");
    expect(rows).toEqual([{ kind: "eq", text: "" }]);
  });

  it("returns sentinel row when input exceeds cap", () => {
    const big = new Array(MAX_DIFF_LINES + 2).fill("x").join("\n");
    const rows = diffLines(big, "x");
    expect(rows).toEqual([{ kind: "eq", text: "(diff too large to display)" }]);
  });
});

describe("BriefHistoryDiffView component", () => {
  it("renders two columns marked with their snapshot labels", () => {
    render(
      <BriefHistoryDiffView
        leftMarkdown="a\nb"
        rightMarkdown="a\nb"
        leftTimestamp="ts-left"
        rightTimestamp="ts-right"
        onClose={() => {}}
      />,
    );
    expect(screen.getByTestId("briefs-diff-view")).toBeInTheDocument();
    expect(
      screen.getByLabelText(/snapshot ts-left/i),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText(/snapshot ts-right/i),
    ).toBeInTheDocument();
  });

  it("applies add class on the right and pad on the left for additions", () => {
    const { container } = render(
      <BriefHistoryDiffView
        leftMarkdown={"a\nb"}
        rightMarkdown={"a\nb\nc"}
        leftTimestamp="L"
        rightTimestamp="R"
        onClose={() => {}}
      />,
    );
    const cols = container.querySelectorAll(".briefs-diff-col");
    expect(cols.length).toBe(2);
    const leftCol = cols[0];
    const rightCol = cols[1];
    // Both columns have same row count (3).
    expect(leftCol.children.length).toBe(3);
    expect(rightCol.children.length).toBe(3);
    // Row index 2 = the added "c": left should be a pad, right should be add.
    expect(leftCol.children[2].className).toContain("briefs-diff-line-pad");
    expect(rightCol.children[2].className).toContain("briefs-diff-line-add");
  });

  it("applies del class on the left and pad on the right for deletions", () => {
    const { container } = render(
      <BriefHistoryDiffView
        leftMarkdown={"a\nb\nc"}
        rightMarkdown={"a\nb"}
        leftTimestamp="L"
        rightTimestamp="R"
        onClose={() => {}}
      />,
    );
    const cols = container.querySelectorAll(".briefs-diff-col");
    const leftCol = cols[0];
    const rightCol = cols[1];
    expect(leftCol.children[2].className).toContain("briefs-diff-line-del");
    expect(rightCol.children[2].className).toContain("briefs-diff-line-pad");
  });

  it("renders the over-cap fallback message", () => {
    const big = new Array(MAX_DIFF_LINES + 2).fill("x").join("\n");
    render(
      <BriefHistoryDiffView
        leftMarkdown={big}
        rightMarkdown={"x"}
        leftTimestamp="L"
        rightTimestamp="R"
        onClose={() => {}}
      />,
    );
    expect(
      screen.getAllByText(/diff too large to display/i).length,
    ).toBeGreaterThan(0);
  });

  it("calls onClose when the Close button is clicked", () => {
    const onClose = vi.fn();
    render(
      <BriefHistoryDiffView
        leftMarkdown="a"
        rightMarkdown="a"
        leftTimestamp="L"
        rightTimestamp="R"
        onClose={onClose}
      />,
    );
    screen.getByRole("button", { name: /close/i }).click();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
