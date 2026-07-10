// QA 2026-06-12: lock the simple-view contract.
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
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
    createdAt: "2026-06-12T22:00:00Z",
    memberId: overrides.memberId,
    round: overrides.round,
    payload: overrides.payload ?? {},
    raw: undefined,
  } as CouncilTranscriptEvent;
}

describe("CouncilTranscript simple view", () => {
  beforeEach(() => cleanup());

  it("renders Simple by default with the user prompt and one turn per member", () => {
    const events = [
      ev("run_started", { sequence: 1 }),
      ev("member_call_started", { sequence: 2, memberId: "m-1", round: 1 }),
      ev("member_message", {
        sequence: 3, memberId: "m-1", round: 1,
        payload: { content: "Paris is the capital of France.", model: "gemma3:27b", duration_ms: 1200 },
      }),
      ev("member_call_started", { sequence: 4, memberId: "m-2", round: 1 }),
      ev("member_message", {
        sequence: 5, memberId: "m-2", round: 1,
        payload: { content: "Confirmed.", model: "mistral-small3.1:latest", duration_ms: 800 },
      }),
    ];

    render(
      <CouncilTranscript
        events={events}
        userPrompt="What is the capital of France?"
        memberLabels={{ "m-1": "Gemma3 27B", "m-2": "Mistral Small 3.1" }}
      />,
    );

    expect(screen.getByText(/User Prompt:/i)).toBeInTheDocument();
    expect(screen.getByText(/What is the capital of France\?/)).toBeInTheDocument();
    expect(screen.getByText(/Gemma3 27B/)).toBeInTheDocument();
    expect(screen.getByText(/Paris is the capital of France\./)).toBeInTheDocument();
    expect(screen.getByText(/Mistral Small 3\.1/)).toBeInTheDocument();
    expect(screen.getByText(/"Confirmed\."/)).toBeInTheDocument();
  });

  it("escapes XSS-shaped model output instead of executing it (F086-D)", () => {
    // Model/tool output is untrusted. It must render as inert text, never as
    // live HTML — defense-in-depth behind the bundled-app CSP. Guards against a
    // future dangerouslySetInnerHTML regression on this surface.
    const payload =
      '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>';
    (window as unknown as { __xss?: number }).__xss = undefined;
    const events = [
      ev("member_message", {
        sequence: 1, memberId: "m-1", round: 1,
        payload: { content: payload },
      }),
    ];
    const { container } = render(<CouncilTranscript events={events} />);
    // No live element was created from the payload...
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    // ...and the payload survives as escaped text.
    expect(container.textContent).toContain("onerror");
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
  });

  it("renders a live user interjection as a distinct 'You' turn in sequence", () => {
    const events = [
      ev("member_message", {
        sequence: 1, memberId: "m-1", round: 1,
        payload: { content: "Initial take." },
      }),
      ev("user_interjection", {
        sequence: 2, round: 1,
        payload: { content: "Optimize for cost, not speed.", author: "user" },
      }),
      ev("member_message", {
        sequence: 3, memberId: "m-2", round: 1,
        payload: { content: "Adjusting for cost." },
      }),
    ];
    render(<CouncilTranscript events={events} />);
    const bubble = screen.getByTestId("simple-interjection");
    expect(bubble).toHaveTextContent("You:");
    expect(bubble).toHaveTextContent("Optimize for cost, not speed.");
  });

  it("shows 'is thinking…' for a member who has started but not produced a message", () => {
    const events = [
      ev("member_call_started", { sequence: 1, memberId: "m-3", round: 1 }),
    ];
    render(
      <CouncilTranscript
        events={events}
        memberLabels={{ "m-3": "Qwen 3.5 9B" }}
      />,
    );
    expect(screen.getByText(/Qwen 3\.5 9B/)).toBeInTheDocument();
    expect(screen.getByText(/is thinking/)).toBeInTheDocument();
  });

  it("surfaces a 'reasoning budget exhausted' note instead of the raw trace", () => {
    const events = [
      ev("member_call_started", { sequence: 1, memberId: "m-3", round: 1 }),
      ev("member_message", {
        sequence: 2, memberId: "m-3", round: 1,
        payload: {
          content: "(reasoning trace, no visible answer) Thinking Process: ...",
          model: "qwen3.5:9b",
        },
      }),
    ];
    render(
      <CouncilTranscript events={events} memberLabels={{ "m-3": "Qwen 3.5 9B" }} />,
    );
    expect(screen.getByText(/reasoning budget exhausted/)).toBeInTheDocument();
    // The raw trace must NOT be rendered verbatim in simple view.
    expect(screen.queryByText(/Thinking Process/)).toBeNull();
  });

  it("notes a dialect downgrade inline on the affected member's turn", () => {
    const events = [
      ev("member_call_started", { sequence: 1, memberId: "m-1", round: 1 }),
      ev("dialect_downgraded", {
        sequence: 2, memberId: "m-1", round: 1,
        payload: { from: "digest_v1", to: "prose", reason: "digest_parse_failed" },
      }),
      ev("member_message", {
        sequence: 3, memberId: "m-1", round: 1,
        payload: { content: "Paris.", model: "gemma3:27b" },
      }),
    ];
    render(
      <CouncilTranscript events={events} memberLabels={{ "m-1": "Gemma3 27B" }} />,
    );
    expect(screen.getByText(/dialect downgraded to prose/)).toBeInTheDocument();
    expect(screen.getByText(/"Paris\."/)).toBeInTheDocument();
  });

  it("trims stray leading/trailing whitespace from a member's answer", () => {
    const events = [
      ev("member_call_started", { sequence: 1, memberId: "m-1", round: 1 }),
      ev("member_message", {
        sequence: 2, memberId: "m-1", round: 1,
        payload: {
          content: "\n\n  They're all pretty much the same.   \n\n\n\n",
          model: "gemma3:27b",
        },
      }),
    ];
    render(
      <CouncilTranscript events={events} memberLabels={{ "m-1": "Gem" }} />,
    );
    // Simple view wraps the content in quotes; the trimmed text sits flush
    // against them (no leading/trailing whitespace inside the quotes).
    expect(
      screen.getByText('"They\'re all pretty much the same."'),
    ).toBeInTheDocument();
  });

  it("warns when a run hit the round limit instead of reaching consensus", () => {
    const events = [
      ev("member_message", {
        sequence: 1, memberId: "m-1", round: 2,
        payload: { content: "Lay's, I guess.", model: "gemma3" },
      }),
      ev("final_answer", {
        sequence: 2, memberId: "m-1", round: 2,
        payload: { content: "Lay's, I guess." },
      }),
      ev("run_completed", { sequence: 3, payload: { reason: "limits_exhausted" } }),
    ];
    render(<CouncilTranscript events={events} />);
    expect(screen.getByText(/did not reach consensus/i)).toBeInTheDocument();
    // It must NOT claim consensus.
    expect(screen.queryByText(/consensus reached/i)).toBeNull();
  });

  it("labels a genuine consensus result as consensus reached", () => {
    const events = [
      ev("final_answer", {
        sequence: 1, memberId: "m-1", round: 3,
        payload: { content: "We agree: Kettle." },
      }),
      ev("run_completed", { sequence: 2, payload: { reason: "consensus_reached" } }),
    ];
    render(<CouncilTranscript events={events} />);
    expect(screen.getByText(/consensus reached/i)).toBeInTheDocument();
    expect(screen.queryByText(/did not reach consensus/i)).toBeNull();
  });

  it("renders a synthesized consensus answer as a distinct Consensus block", () => {
    const events = [
      ev("final_answer", {
        sequence: 1, memberId: "m-1", round: 3,
        payload: {
          content: "Drink water, rest, and see a doctor for red-flag symptoms.",
          synthesis_mode: "consensus",
        },
      }),
      ev("run_completed", { sequence: 2, payload: { reason: "consensus_reached" } }),
    ];
    render(<CouncilTranscript events={events} />);
    // "Consensus reached" banner + distinct "Consensus:" label + the
    // plain-English "how the council leader got here" note.
    expect(screen.getByTestId("consensus-banner")).toHaveTextContent(
      /reached consensus/i,
    );
    expect(screen.getByText(/^Consensus:$/)).toBeInTheDocument();
    expect(
      screen.getByText(/the council leader.*combined their positions/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Drink water, rest, and see a doctor/),
    ).toBeInTheDocument();
  });

  it("renders a synthesized summary as a distinct Summary block", () => {
    const events = [
      ev("final_answer", {
        sequence: 1, memberId: "m-2", round: 1,
        payload: {
          content: "Most members preferred A, while one argued B.",
          synthesis_mode: "summary",
        },
      }),
      ev("run_completed", { sequence: 2, payload: { reason: "max_rounds_reached" } }),
    ];
    render(<CouncilTranscript events={events} memberLabels={{ "m-2": "Rapporteur" }} />);
    expect(screen.getByText(/^Summary:$/)).toBeInTheDocument();
    expect(screen.getByTestId("council-leader")).toHaveTextContent(
      "Council Leader: Rapporteur",
    );
    expect(screen.getByText(/summary preserves disagreement/i)).toBeInTheDocument();
    expect(screen.queryByText(/^Consensus:$/)).toBeNull();
    expect(screen.queryByText(/last message stated/i)).toBeNull();
  });

  it("shows who agreed + how many, and badges agreeing members' final turns", () => {
    const events = [
      ev("member_message", {
        sequence: 1, memberId: "m-1", round: 2,
        payload: { content: "I hold my position." },
      }),
      ev("member_message", {
        sequence: 2, memberId: "m-2", round: 2,
        payload: { content: "Agreed, no changes." },
      }),
      ev("final_answer", {
        // Synthesis happens on a LATER round than the agreement round; the
        // badge must follow consensus.round (2), not the final-answer round (3).
        sequence: 3, memberId: "m-1", round: 3,
        payload: {
          content: "The shared conclusion.",
          synthesis_mode: "consensus",
          consensus: {
            agreed_member_ids: ["m-1", "m-2"],
            threshold: 2,
            round: 2,
            member_count: 2,
          },
        },
      }),
      ev("run_completed", { sequence: 4, payload: { reason: "consensus_reached" } }),
    ];
    render(
      <CouncilTranscript
        events={events}
        memberLabels={{ "m-1": "Qwen", "m-2": "Claude" }}
      />,
    );
    // "2 of 2 members held their position in round 2" + the agreeing names.
    expect(
      screen.getByText(/2 of 2 members held their position in round 2/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Qwen, Claude/)).toBeInTheDocument();
    // Each agreeing member's round-2 turn carries a "held position" badge.
    expect(screen.getAllByTestId("held-position-badge")).toHaveLength(2);
  });

  it("renders a digest_v1 answer as readable prose, not raw JSON", () => {
    const digest = JSON.stringify({
      v: "digest_v1",
      position: "2+2=4",
      claims: [{ id: "c1", text: "Addition of two and two yields four.", cites: [], confidence: "high" }],
      agree: [], dispute: [], delta: null, open: [],
      answer_fragment: "2 + 2 = 4",
    });
    const events = [
      ev("member_message", {
        sequence: 1, memberId: "m-1", round: 1,
        payload: { content: digest, model: "qwen2.5:3b" },
      }),
    ];
    render(<CouncilTranscript events={events} memberLabels={{ "m-1": "Gem" }} />);
    expect(screen.getByText(/2 \+ 2 = 4/)).toBeInTheDocument();
    expect(screen.getByText(/Addition of two and two yields four/)).toBeInTheDocument();
    // Raw JSON envelope must NOT appear in the simple view.
    expect(screen.queryByText(/"v":\s*"digest_v1"/)).toBeNull();
    expect(screen.queryByText(/digest_v1/)).toBeNull();
  });

  it("humanizes a digest wrapped in a ```json fence (Gemma's format)", () => {
    // Gemma emits the digest inside a markdown code fence; the humanizer must
    // strip the fence and still render prose, not raw JSON.
    const fenced =
      "```json\n" +
      JSON.stringify({
        v: "digest_v1",
        position: "Kettle Sea Salt is the best chip.",
        claims: [{ id: "c1", text: "Kettle-cooked for crunch.", cites: [], confidence: "high" }],
        agree: [], dispute: [], delta: null, open: [],
        answer_fragment: "Kettle Sea Salt is the best chip.",
      }) +
      "\n```";
    const events = [
      ev("member_message", {
        sequence: 1, memberId: "m-1", round: 1,
        payload: { content: fenced, model: "gemma3:27b" },
      }),
    ];
    render(<CouncilTranscript events={events} memberLabels={{ "m-1": "Gem" }} />);
    expect(screen.getByText(/Kettle Sea Salt is the best chip/)).toBeInTheDocument();
    expect(screen.getByText(/Kettle-cooked for crunch/)).toBeInTheDocument();
    expect(screen.queryByText(/digest_v1/)).toBeNull();
    expect(screen.queryByText(/```/)).toBeNull();
  });

  it("toggles to Verbose view when the Verbose button is clicked", () => {
    const events = [
      ev("context_built", {
        sequence: 1, memberId: "m-1", round: 1,
        payload: { context_id: "ctx-abc", manifest_id: "cm-xyz" },
      }),
    ];
    render(<CouncilTranscript events={events} />);
    // Verbose-only event type is hidden in Simple view
    expect(screen.queryByText(/context_built/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /verbose/i }));
    expect(screen.getByText(/context_built/)).toBeInTheDocument();
  });

  it("renders steward packet lifecycle events as compact verbose cards", () => {
    const events = [
      ev("steward_packet_created", {
        sequence: 1,
        round: 1,
        payload: {
          packet_id: "sp_abc123",
          content_sha256: "c".repeat(64),
          mode: "deterministic",
          estimated_tokens: 92,
          coverage: {
            from_sequence: 1,
            to_sequence: 8,
            source_event_ids: ["ev-1", "ev-2", "ev-3"],
          },
          source_event_ids: ["ev-1", "ev-2", "ev-3"],
        },
      }),
    ];
    render(<CouncilTranscript events={events} />);
    expect(screen.queryByText(/steward_packet_created/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /verbose/i }));
    expect(screen.getByText(/steward_packet_created/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Steward packet created"));
    expect(screen.getByText("sp_abc123")).toBeInTheDocument();
    expect(screen.getByText("deterministic")).toBeInTheDocument();
    expect(screen.getByText("92")).toBeInTheDocument();
  });
});
