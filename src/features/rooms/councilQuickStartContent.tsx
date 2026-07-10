// F141 WS-H — Council rooms Quick Start guide: static ship copy.
//
// SINGLE SOURCE OF TRUTH for the guide's copy. Plain structured JSX (no Markdown
// runtime, no external assets, NO fetches) so it renders offline / browser-dev /
// against remote AIAR. Every mode/label named here was taken from the room
// editor's own TIP help strings + docs/COUNCIL_ROOM_SETTINGS.md and should be
// re-verified against source if reworded. The list of starting points / topology
// kinds / finalizer modes is also exported (COUNCIL_STARTING_POINTS etc.) so a
// test can assert the guide names every currently-enabled option and can't
// silently drift.
import type { ReactNode } from "react";

export interface CouncilQuickStartSection {
  id: string;
  title: string;
  body: ReactNode;
}

// Enabled option keys the guide must document (asserted by the content test).
export const COUNCIL_STARTING_POINTS = [
  "Quick answers",
  "Debate to consensus",
  "Coding",
  "Token-saver",
  "Marathon",
  "Credibility",
] as const;

export const COUNCIL_TOPOLOGIES = [
  "Round robin",
  "Consensus deliberation",
  "Credibility",
] as const;

export const COUNCIL_FINAL_ANSWER_MODES = [
  "Transcript only",
  "Single finalizer",
  "Consensus report",
  "Summary",
  "Credibility report",
] as const;

export const COUNCIL_QUICK_START_SECTIONS: CouncilQuickStartSection[] = [
  {
    id: "what-is-a-room",
    title: "What is a Council room?",
    body: (
      <>
        <p>
          A Council room is a group of AI models you assemble to answer one
          prompt together. You choose who's in the room, how they talk to each
          other, how private each member is, and how the final answer is
          written. Everything runs locally through your configured providers.
        </p>
        <p>
          Start from a preset below, then adjust the flow, privacy, final
          answer, and limits. Advanced tuning stays collapsed with active-state
          summaries so you only open what you need.
        </p>
      </>
    ),
  },
  {
    id: "starting-points",
    title: "Starting points",
    body: (
      <>
        <p>Pick one, then tweak:</p>
        <ul>
          <li>
            <strong>Quick answers</strong> — one round, every member answers in
            plain prose. Fastest and cheapest.
          </li>
          <li>
            <strong>Debate to consensus</strong> — members deliberate over
            several rounds and converge, using a compact structured dialect.
          </li>
          <li>
            <strong>Coding</strong> — seeds a resilient Coding Team: a strong PM
            frames small tasks for mixed coding workers; bad turns retry,
            escalate, then return to the PM.
          </li>
          <li>
            <strong>Token-saver</strong> — telegraphic + digest + compaction +
            cache hints + steward. Minimizes tokens spent between members.
          </li>
          <li>
            <strong>Marathon</strong> — open-ended deliberation up to 100 rounds;
            a leader (steward) plus compaction keep it sustainable. Give a big
            task and let them run.
          </li>
          <li>
            <strong>Credibility</strong> — source-backed answers: members
            research the web, post claims, peer-review each other's citations,
            and the leader writes a report citing only verified, actually-fetched
            sources.
          </li>
        </ul>
      </>
    ),
  },
  {
    id: "flow",
    title: "Flow — how members take turns",
    body: (
      <ul>
        <li>
          <strong>Round robin</strong> — each member answers once per round, in
          order.
        </li>
        <li>
          <strong>Consensus deliberation</strong> — round 1 is blind, then
          members refine each round until they agree (or rounds run out).
        </li>
        <li>
          <strong>Credibility</strong> — members surface claims and verify each
          other's citations.
        </li>
      </ul>
    ),
  },
  {
    id: "final-answer",
    title: "Final Answer — how the answer is chosen",
    body: (
      <ul>
        <li>
          <strong>Transcript only</strong> — the last message is the answer.
        </li>
        <li>
          <strong>Single finalizer</strong> — a named member writes the final
          answer.
        </li>
        <li>
          <strong>Consensus report</strong> — a synthesizer writes the agreed
          answer.
        </li>
        <li>
          <strong>Summary</strong> — a rapporteur writes an abstractive summary
          that preserves disagreement (runs on any ending, not just consensus).
        </li>
        <li>
          <strong>Credibility report</strong> — a report that cites only verified
          sources.
        </li>
        <li>
          <em>Vote summary</em> and <em>Judge verdict</em> are planned and shown
          disabled.
        </li>
      </ul>
    ),
  },
  {
    id: "privacy",
    title: "Privacy — what each member sees",
    body: (
      <>
        <p>Each member can be limited in what it sees:</p>
        <ul>
          <li>
            <strong>Private prompt only</strong> — just the question; sees only
            its own messages.
          </li>
          <li>
            <strong>Sources + discussion</strong> — relevant corpus passages plus
            the full discussion.
          </li>
          <li>
            <strong>Full workspace context</strong> — everything.
          </li>
          <li>
            <strong>Custom</strong> — set context access and transcript access by
            hand.
          </li>
        </ul>
        <p>Less access = more private and cheaper.</p>
      </>
    ),
  },
  {
    id: "steward",
    title: "Steward — the council leader",
    body: (
      <p>
        Turn this on for long runs. The Steward keeps the full conversation
        visible to you while compacting older member-to-member chatter into a
        short, inspectable "packet" that members receive instead of the whole
        transcript. It can be a separate model or reuse an existing member; it
        can keep structured facts, short summaries, or both.
      </p>
    ),
  },
  {
    id: "efficiency",
    title: "Context efficiency — token savers",
    body: (
      <ul>
        <li>
          <strong>Telegraphic style</strong> — members are terse while
          deliberating; the final answer is never abbreviated.
        </li>
        <li>
          <strong>Digest dialect</strong> — members emit a small structured
          position (needed for consensus to detect agreement).
        </li>
        <li>
          <strong>Citation references</strong> — repeated source passages become
          short <code>[c:1]</code> markers plus a lookup table, so the same text
          isn't re-sent every turn.
        </li>
        <li>
          <strong>Compaction</strong> — older rounds get summarized while recent
          ones stay verbatim.
        </li>
        <li>
          <strong>Prompt-cache hints</strong> — marks the stable context so
          caching providers bill less.
        </li>
      </ul>
    ),
  },
  {
    id: "budget",
    title: "Budget & Limits",
    body: (
      <p>
        Caps for a single run — max model calls, max tokens, max rounds, and
        (with a Steward) max steward calls. When a cap is hit the run stops
        cleanly rather than running away. Set these to bound cost on open-ended
        rooms.
      </p>
    ),
  },
  {
    id: "tools",
    title: "Tools",
    body: (
      <p>
        Members can be granted internet and code tools. Everything is{" "}
        <strong>off by default and fail-closed</strong>; the first use of a
        granted tool asks for your approval. Tool output is treated as untrusted
        data, never as instructions.
      </p>
    ),
  },
];

export const COUNCIL_QUICK_START_TOC = COUNCIL_QUICK_START_SECTIONS.map((s) => ({
  id: s.id,
  title: s.title,
}));
