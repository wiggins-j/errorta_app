// F080 — the neutral judge's verdict renders in the simple transcript view.
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import CouncilTranscript from "./CouncilTranscript";
import type { CouncilTranscriptEvent } from "./types";

function ev(
  type: string,
  overrides: Partial<CouncilTranscriptEvent> & { payload?: Record<string, unknown> } = {},
): CouncilTranscriptEvent {
  return {
    id: `ev-${Math.random()}`,
    type,
    sequence: overrides.sequence ?? 0,
    status: "completed",
    createdAt: "2026-06-15T00:00:00Z",
    memberId: overrides.memberId,
    round: overrides.round,
    payload: overrides.payload ?? {},
    raw: undefined,
  } as CouncilTranscriptEvent;
}

describe("CouncilTranscript neutral judge", () => {
  beforeEach(() => cleanup());

  it("renders a judge verdict line in the simple view", () => {
    const events = [
      ev("judge_verdict", {
        sequence: 5, memberId: "m-judge", round: 1,
        payload: {
          verdict: "reached",
          reason: "both members converged on LRU",
        },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="cache?" memberLabels={{}} />);
    const verdict = screen.getByTestId("simple-judge-verdict");
    expect(verdict).toHaveTextContent("Judge");
    expect(verdict).toHaveTextContent("members reached a verdict");
    expect(verdict).toHaveTextContent("both members converged on LRU");
  });

  it("labels a judge-synthesized final answer as a Judge verdict", () => {
    const events = [
      ev("final_answer", {
        sequence: 9, memberId: "m-judge", round: 1,
        payload: { content: "Use an LRU cache.", synthesis_mode: "judge",
                   judge: { verdict: "reached" } },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="cache?" memberLabels={{}} />);
    expect(screen.getByText(/Judge verdict:/)).toBeInTheDocument();
    expect(screen.getByText(/Use an LRU cache\./)).toBeInTheDocument();
  });
});
