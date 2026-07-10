// Axe-core a11y sweep for CouncilTranscript, locking the Simple/Verbose
// toolbar and both view states against accessibility regressions.
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, it } from "vitest";

import CouncilTranscript from "./CouncilTranscript";
import { expectNoA11yViolations } from "./a11y-helpers";
import type { CouncilTranscriptEvent } from "./types";

function ev(
  type: string,
  overrides: Partial<CouncilTranscriptEvent> & { payload?: Record<string, unknown> } = {},
): CouncilTranscriptEvent {
  return {
    id: `ev-${type}`,
    type,
    sequence: overrides.sequence ?? 0,
    status: "completed",
    createdAt: "2026-06-12T00:00:00Z",
    memberId: overrides.memberId,
    round: overrides.round,
    payload: overrides.payload ?? {},
    raw: undefined,
  } as CouncilTranscriptEvent;
}

const EVENTS: CouncilTranscriptEvent[] = [
  ev("run_started", { sequence: 1 }),
  ev("member_call_started", { sequence: 2, memberId: "m-1", round: 1 }),
  ev("member_message", {
    sequence: 3, memberId: "m-1", round: 1,
    payload: { content: "Paris.", model: "gemma3:27b" },
  }),
  ev("final_answer", {
    sequence: 4,
    payload: { content: "The capital of France is Paris." },
  }),
];

afterEach(() => cleanup());

describe("CouncilTranscript a11y", () => {
  it("simple_view_no_violations", async () => {
    const { container } = render(
      <CouncilTranscript
        events={EVENTS}
        userPrompt="What is the capital of France?"
        memberLabels={{ "m-1": "Gemma3 27B" }}
      />,
    );
    await expectNoA11yViolations(container);
  });

  it("verbose_view_no_violations", async () => {
    const { container, getByRole } = render(
      <CouncilTranscript events={EVENTS} />,
    );
    fireEvent.click(getByRole("button", { name: /verbose/i }));
    await expectNoA11yViolations(container);
  });

  it("final_answer_no_violations", async () => {
    const { container } = render(
      <CouncilTranscript
        events={[ev("final_answer", { payload: { content: "Paris." } })]}
      />,
    );
    await expectNoA11yViolations(container);
  });
});
