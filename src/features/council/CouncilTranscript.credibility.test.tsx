// F078 — credibility report renders in the simple transcript view.
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

const REPORT = {
  mode: "credibility_report",
  claims_used: ["c1", "c2"],
  source_map: [
    { source_id: "src_0001", url: "https://standards.gov/spec", title: "Spec", source_type: "government" },
  ],
  caveats: ["Claim c2 admitted with caveat (verified_indirect)."],
  excluded_claims: [{ claim_id: "c3", reason: "contradicted" }],
  confidence: "high",
  verification_incomplete: false,
};

describe("CouncilTranscript credibility report", () => {
  beforeEach(() => cleanup());

  it("labels the final answer as a Credibility report and renders sources/caveats", () => {
    const events = [
      ev("final_answer", {
        sequence: 9, memberId: "leader", round: 2,
        payload: { content: "2 verified claims.", synthesis_mode: "credibility",
                   credibility_report: REPORT },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="Compare caches" memberLabels={{}} />);

    expect(screen.getByText(/Credibility report:/)).toBeInTheDocument();
    expect(screen.getByTestId("credibility-report")).toBeInTheDocument();
    expect(screen.getByTestId("credibility-sources")).toHaveTextContent("standards.gov/spec");
    expect(screen.getByTestId("credibility-sources")).toHaveTextContent("[government]");
    expect(screen.getByTestId("credibility-caveats")).toHaveTextContent("caveat");
    expect(screen.getByTestId("credibility-excluded")).toHaveTextContent("1 claim excluded");
    expect(screen.getByText(/confidence high/)).toBeInTheDocument();
  });

  it("renders the F081 quality flag + entailment exclusions", () => {
    const events = [
      ev("final_answer", {
        sequence: 9, memberId: "leader", round: 2,
        payload: { content: "x", synthesis_mode: "credibility",
                   credibility_report: {
                     ...REPORT, quality_flag: "unchallenged_consensus",
                     excluded_claims: [{ claim_id: "Ada:c1", reason: "entailment_contradicted" }],
                   } },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{}} />);
    expect(screen.getByTestId("credibility-quality-flag")).toHaveTextContent(
      /Unchallenged consensus/);
    expect(screen.getByTestId("credibility-excluded")).toHaveTextContent(
      /cited source argued the opposite/);
  });

  it("humanizes a credibility JSON claim packet in the simple view (no raw JSON)", () => {
    const events = [
      ev("member_call_started", { sequence: 1, memberId: "GPT", round: 1 }),
      ev("member_message", {
        sequence: 2, memberId: "GPT", round: 1,
        payload: { content: JSON.stringify({
          answer_fragment: "Montgomery",
          claims: [{ claim_id: "c1", text: "Montgomery is the capital of Alabama.",
                     source_ids: ["https://example.gov/al"] }] }) },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="capital of Alabama?"
                             memberLabels={{ GPT: "GPT" }} />);
    expect(screen.getByText(/Montgomery is the capital of Alabama\./)).toBeInTheDocument();
    // The cited website is surfaced inline (host only).
    expect(screen.getByText(/\(example\.gov\)/)).toBeInTheDocument();
    // The raw JSON envelope must not be shown verbatim.
    expect(screen.queryByText(/"answer_fragment"/)).toBeNull();
  });

  it("shows the member's own words (comment) for a discussion turn", () => {
    const events = [
      ev("member_call_started", { sequence: 1, memberId: "Qwen", round: 2 }),
      ev("member_message", {
        sequence: 2, memberId: "Qwen", round: 2,
        payload: { content: '```json\n' + JSON.stringify({
          comment: "I think Claude overstates the materialist case; the andrewmbailey.com PDF rebuts it.",
          reviews: [{ claim_id: "Claude:c1", status: "partially_supported" }],
        }) + '\n```' },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{ Qwen: "Qwen" }} />);
    expect(screen.getByText(/Claude overstates the materialist case/)).toBeInTheDocument();
    expect(screen.queryByText(/"reviews"/)).toBeNull();
  });

  it("falls back to a plain-English review summary when there is no comment", () => {
    const events = [
      ev("member_call_started", { sequence: 1, memberId: "Qwen", round: 2 }),
      ev("member_message", {
        sequence: 2, memberId: "Qwen", round: 2,
        payload: { content: JSON.stringify({ reviews: [
          { claim_id: "GPT:c1", status: "verified" },
          { claim_id: "GPT:c2", status: "contradicted" },
        ] }) },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{ Qwen: "Qwen" }} />);
    expect(screen.getByText(/I agree with GPT:c1/)).toBeInTheDocument();
    expect(screen.getByText(/I disagree with GPT:c2/)).toBeInTheDocument();
    // No robotic "verified — GPT:c1" any more.
    expect(screen.queryByText(/verified — GPT:c1/)).toBeNull();
  });

  it("labels the final answer with the Council Leader's name", () => {
    const events = [
      ev("final_answer", {
        sequence: 9, memberId: "GPT", round: 2,
        payload: { content: "answer", synthesis_mode: "credibility", credibility_report: REPORT },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{ GPT: "GPT" }} />);
    expect(screen.getByTestId("council-leader")).toHaveTextContent("Council Leader: GPT");
  });

  it("renders F082 dispositions (revised / inference) + finalizer failure", () => {
    const events = [
      ev("final_answer", {
        sequence: 9, memberId: "GPT", round: 2,
        payload: { content: "x", synthesis_mode: "credibility",
                   credibility_report: { ...REPORT,
                     dispositions: [
                       { claim_id: "A:c1", disposition: "revised", text: "X is hard", revised_text: "X is hard" },
                       { claim_id: "A:c2", disposition: "inference", text: "Therefore Y", revised_text: "" },
                     ],
                     finalizer_citation_failures: [{ claim_id: "GPT:c5", reason: "contradicts" }],
                   } },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{}} />);
    expect(screen.getByTestId("credibility-dispositions")).toHaveTextContent(/Narrowed to what the source supports/);
    expect(screen.getByTestId("credibility-dispositions")).toHaveTextContent(/Inference \(not directly stated/);
    expect(screen.getByTestId("credibility-finalizer-failed")).toHaveTextContent(/council leader mis-cited 1/);
  });

  it("shows a verification-incomplete warning when flagged", () => {
    const events = [
      ev("final_answer", {
        sequence: 1, memberId: "leader", round: 1,
        payload: { content: "Incomplete.", synthesis_mode: "credibility",
                   credibility_report: { ...REPORT, verification_incomplete: true,
                                         claims_used: [], source_map: [], caveats: [],
                                         excluded_claims: [], confidence: "low" } },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{}} />);
    expect(screen.getByTestId("credibility-incomplete")).toHaveTextContent("verification incomplete");
  });

  it("F084: renders steelman claims as a distinct UNVERIFIED section with the topic", () => {
    const events = [
      ev("final_answer", {
        sequence: 1, memberId: "leader", round: 2,
        payload: { content: "answer", synthesis_mode: "credibility",
          credibility_report: { ...REPORT,
            steelman_claims: [
              { claim_id: "adv:c1", member_id: "adv", topic: "Existence of Santa",
                text: "The simulation admin instantiates gifts globally.",
                cited: ["https://made-up.invalid/x"] },
            ] } },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="Is Santa real?" memberLabels={{}} />);
    const sec = screen.getByTestId("credibility-steelman");
    expect(sec).toHaveTextContent("UNVERIFIED");
    expect(sec).toHaveTextContent("Existence of Santa");
    expect(sec).toHaveTextContent("simulation admin instantiates gifts");
    // The constructed URL is NOT rendered as a trustworthy source link.
    expect(screen.queryByTestId("credibility-sources")).toHaveTextContent("standards.gov/spec");
  });

  it("F084: badges a steelman member's spoken turn as unverified", () => {
    const events = [
      ev("member_message", {
        sequence: 1, memberId: "adv", round: 1,
        payload: { content: "Santa is plainly real.", member_id: "adv" },
      }),
    ];
    render(
      <CouncilTranscript
        events={events}
        userPrompt="Is Santa real?"
        memberLabels={{ adv: "NewGuy" }}
        steelmanMemberIds={["adv"]}
      />,
    );
    expect(screen.getByTestId("steelman-badge")).toHaveTextContent(/Steelman/i);
  });

  it("F085: tags an opinion source and shows the legend", () => {
    const events = [
      ev("final_answer", {
        sequence: 1, memberId: "leader", round: 2,
        payload: { content: "answer", synthesis_mode: "credibility",
          credibility_report: { ...REPORT,
            source_map: [
              { source_id: "s1", url: "https://veracalloway.com/x", title: "",
                source_type: "blog", tier: "opinion", tier_label: "opinion" },
            ] } },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{}} />);
    expect(screen.getByTestId("source-tier-0")).toHaveTextContent("opinion");
    expect(screen.getByTestId("credibility-tier-legend")).toHaveTextContent("individual viewpoint");
  });

  it("F085: no opinion legend when all sources are primary/reporting", () => {
    const events = [
      ev("final_answer", {
        sequence: 1, memberId: "leader", round: 2,
        payload: { content: "answer", synthesis_mode: "credibility",
          credibility_report: { ...REPORT,
            source_map: [
              { source_id: "s1", url: "https://standards.gov/spec", title: "Spec",
                source_type: "government", tier: "primary", tier_label: "primary" },
            ] } },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{}} />);
    expect(screen.getByTestId("source-tier-0")).toHaveTextContent("primary");
    expect(screen.queryByTestId("credibility-tier-legend")).toBeNull();
  });

  it("F085: falls back to local tier roll-up when report omits tier (old report)", () => {
    const events = [
      ev("final_answer", {
        sequence: 1, memberId: "leader", round: 2,
        payload: { content: "answer", synthesis_mode: "credibility",
          credibility_report: { ...REPORT,
            source_map: [
              { source_id: "s1", url: "https://forum.test/t", title: "", source_type: "forum" },
            ] } },
      }),
    ];
    render(<CouncilTranscript events={events} userPrompt="x" memberLabels={{}} />);
    expect(screen.getByTestId("source-tier-0")).toHaveTextContent("opinion");
  });
});
