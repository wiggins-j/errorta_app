// F031 Phase 5 Task 2 — ContextInspectionDrawer coverage.
//
// Locks invariant 5 (sealed): the drawer must render without raw payload
// text, only sha256s + counts + classes. Also covers the blocked-reason
// banner (invariant 4 fail-closed surface), 404 → empty-state, and Esc
// close.
import { useState } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ContextInspectionDrawer from "./ContextInspectionDrawer";
import type { CouncilContextManifest } from "./types";

function sampleManifest(
  overrides: Partial<CouncilContextManifest> = {},
): CouncilContextManifest {
  return {
    manifestId: "cm-aaaa1111bbbb2222",
    formatVersion: 1,
    contextId: "ctx-zz",
    runId: "r-1",
    turnId: "m-full-r1",
    memberId: "m-full",
    payloadSha256: "deadbeef".repeat(8),
    requestedContextAccess: "full_context",
    effectiveContextAccess: "full_context",
    requestedTranscriptAccess: "all_messages",
    effectiveTranscriptAccess: "all_messages",
    destinationScope: "local",
    egressClass: "local",
    sourceCounts: {
      task_instructions: 1,
      user_prompt: 1,
      retrieved_snippet: 2,
    },
    sourceRefs: [
      {
        class_: "retrieved_snippet",
        corpusId: "aerospace",
        chunkId: "ch-1",
        citationId: "ct-1",
        contentSha256: "a".repeat(64),
        tokens: 12,
      },
    ],
    omitted: [{ reason: "token_budget", class_: "retrieved_snippet" }],
    tokenEstimate: { input: 220, output: 0 },
    blockedReason: null,
    transformManifestId: null,
    visibilityPlanId: "vp-xy",
    f030AuditId: null,
    ...overrides,
  };
}

function mockResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("ContextInspectionDrawer", () => {
  it("renders policy + counts + refs + omitted sections from a manifest", async () => {
    const m = sampleManifest();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        mockResponse({
          run_id: m.runId,
          turn_id: m.turnId,
          manifest_count: 1,
          manifests: [
            {
              manifest_id: m.manifestId,
              format_version: m.formatVersion,
              context_id: m.contextId,
              run_id: m.runId,
              turn_id: m.turnId,
              member_id: m.memberId,
              payload_sha256: m.payloadSha256,
              requested_context_access: m.requestedContextAccess,
              effective_context_access: m.effectiveContextAccess,
              requested_transcript_access: m.requestedTranscriptAccess,
              effective_transcript_access: m.effectiveTranscriptAccess,
              destination_scope: m.destinationScope,
              egress_class: m.egressClass,
              source_counts: { ...m.sourceCounts, tool_result: 1 },
              source_refs: [
                {
                  class_: "retrieved_snippet",
                  corpus_id: "aerospace",
                  chunk_id: "ch-1",
                  citation_id: "ct-1",
                  content_sha256: "a".repeat(64),
                  tokens: 12,
                },
                {
                  class_: "tool_result",
                  content_sha256: "b".repeat(64),
                  tokens: 9,
                  tool_call_id: "tc-abc123",
                  tool_id: "web_fetch",
                  args_sha256: "d".repeat(64),
                  produced_at: "2026-06-13T00:00:00Z",
                  tool_egress_class: "remote_eligible",
                },
              ],
              omitted: m.omitted,
              token_estimate: m.tokenEstimate,
              steward: {
                enabled: true,
                fallback: false,
                packet_id: "sp_abc123",
                content_sha256: "c".repeat(64),
                mode: "hybrid",
                coverage: {
                  from_sequence: 1,
                  to_sequence: 12,
                  source_event_ids: ["ev-1", "ev-2"],
                },
                recent_full_message_count: 1,
                omitted_transcript_event_count: 4,
                effective_transcript_access: "steward_packet",
              },
              blocked_reason: null,
              transform_manifest_id: null,
              visibility_plan_id: m.visibilityPlanId,
              f030_audit_id: null,
            },
          ],
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          packet_id: "sp_abc123",
          user_goal: { text: "Answer safely.", source_event_ids: ["ev-1"] },
          current_consensus: {
            text: "Prefer the conservative implementation.",
            source_event_ids: ["ev-2"],
          },
          member_positions: [
            {
              member_id: "m-full",
              stance: "Use the packet.",
              confidence: "high",
              source_event_ids: ["ev-2"],
            },
          ],
          open_disagreements: [],
          open_questions: [],
        }),
      );
    vi.stubGlobal(
      "fetch",
      fetchMock,
    );

    render(
      <ContextInspectionDrawer
        runId="r-1"
        turnId="m-full-r1"
        memberId="m-full"
        onClose={() => {}}
      />,
    );

    await waitFor(() => screen.getByText("Effective policy"));
    expect(screen.getByText("Source counts")).toBeInTheDocument();
    expect(screen.getByText("Tool results")).toBeInTheDocument();
    expect(screen.getByText("web_fetch")).toBeInTheDocument();
    expect(screen.getByText("remote_eligible")).toBeInTheDocument();
    expect(screen.getByText("Source refs")).toBeInTheDocument();
    expect(screen.getByText("Council Steward")).toBeInTheDocument();
    expect(screen.getByText("sp_abc123")).toBeInTheDocument();
    await waitFor(() => screen.getByText("Steward packet audit"));
    await waitFor(() => expect(screen.getByText("Packet body")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Packet body"));
    expect(screen.getByText("Answer safely.")).toBeInTheDocument();
    expect(screen.getByText(/Use the packet/)).toBeInTheDocument();
    expect(screen.getByText("Omitted")).toBeInTheDocument();
    // Payload sha is rendered short-form.
    expect(screen.getByTitle(m.payloadSha256)).toBeInTheDocument();
    // Aria label on the drawer.
    expect(screen.getByRole("dialog", { name: "Inspection drawer" })).toBeInTheDocument();
  });

  it("surfaces blocked_reason in a red banner (invariant 4)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r-1",
          turn_id: "t-1",
          manifest_count: 1,
          manifests: [
            {
              manifest_id: "cm-blocked",
              format_version: 1,
              context_id: "ctx-x",
              run_id: "r-1",
              turn_id: "t-1",
              member_id: "m-x",
              payload_sha256: "",
              requested_context_access: "weird_value",
              effective_context_access: "blocked",
              requested_transcript_access: "none",
              effective_transcript_access: "none",
              destination_scope: "local",
              egress_class: "blocked",
              source_counts: {},
              source_refs: [],
              omitted: [],
              token_estimate: { input: 0, output: 0 },
              blocked_reason: "unknown_context_access",
              transform_manifest_id: null,
              visibility_plan_id: null,
              f030_audit_id: null,
            },
          ],
        }),
      ),
    );

    render(
      <ContextInspectionDrawer
        runId="r-1"
        turnId="t-1"
        onClose={() => {}}
      />,
    );
    await waitFor(() => screen.getByRole("alert"));
    expect(screen.getByText("unknown_context_access")).toBeInTheDocument();
  });

  it("renders empty state on 404 (no manifest yet)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(mockResponse({}, 404)),
    );
    render(
      <ContextInspectionDrawer
        runId="r-x"
        turnId="t-x"
        onClose={() => {}}
      />,
    );
    await waitFor(() =>
      screen.getByText(/No manifest yet for this turn/),
    );
  });

  it("Esc key closes the drawer", async () => {
    const onClose = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(mockResponse({}, 404)),
    );
    render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={onClose} />,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("close button click calls onClose", async () => {
    const onClose = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(mockResponse({}, 404)),
    );
    render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={onClose} />,
    );
    await waitFor(() => screen.getByText(/No manifest yet for this turn/));
    fireEvent.click(
      screen.getByRole("button", { name: "Close inspection drawer" }),
    );
    expect(onClose).toHaveBeenCalled();
  });

  // ─── F031-PROVENANCE-VIZ (Task 4) — multi-manifest compare dispatch. ───

  function backendManifest(
    overrides: Record<string, unknown> = {},
  ): Record<string, unknown> {
    return {
      manifest_id: "cm-x",
      format_version: 1,
      context_id: "ctx",
      run_id: "r",
      turn_id: "t",
      member_id: "m",
      payload_sha256: "deadbeef".repeat(8),
      requested_context_access: "full_context",
      effective_context_access: "full_context",
      requested_transcript_access: "all_messages",
      effective_transcript_access: "all_messages",
      destination_scope: "local",
      egress_class: "local",
      source_counts: {},
      source_refs: [],
      omitted: [],
      token_estimate: {},
      blocked_reason: null,
      transform_manifest_id: null,
      visibility_plan_id: null,
      f030_audit_id: null,
      ...overrides,
    };
  }

  it("two-manifest turn renders compare strip, not single card", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r",
          turn_id: "t",
          manifest_count: 2,
          manifests: [
            backendManifest({
              manifest_id: "cm-full",
              member_id: "m-full",
              effective_context_access: "full_context",
            }),
            backendManifest({
              manifest_id: "cm-red",
              member_id: "m-redacted",
              effective_context_access: "redacted_summary",
            }),
          ],
        }),
      ),
    );
    render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() =>
      screen.getByRole("region", {
        name: "Per-member context comparison",
      }),
    );
    // Single-card "Manifest …" labels should NOT appear (drawer should
    // only mount one layout).
    expect(
      screen.queryByLabelText(/^Manifest cm-full$/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText(/^Manifest cm-red$/),
    ).not.toBeInTheDocument();
  });

  it("single-manifest turn renders the existing single-card layout unchanged", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r",
          turn_id: "t",
          manifest_count: 1,
          manifests: [
            backendManifest({
              manifest_id: "cm-only",
              member_id: "m-only",
            }),
          ],
        }),
      ),
    );
    render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() => screen.getByText("Effective policy"));
    expect(
      screen.queryByRole("region", {
        name: "Per-member context comparison",
      }),
    ).not.toBeInTheDocument();
    // The single-card "Manifest …" article still renders.
    expect(screen.getByLabelText(/^Manifest cm-only$/)).toBeInTheDocument();
  });

  it("all-blocked two-manifest turn renders banners + caption", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r",
          turn_id: "t",
          manifest_count: 2,
          manifests: [
            backendManifest({
              manifest_id: "cm-A",
              member_id: "m-A",
              blocked_reason: "unknown_context_access",
            }),
            backendManifest({
              manifest_id: "cm-B",
              member_id: "m-B",
              blocked_reason: "egress_violation",
            }),
          ],
        }),
      ),
    );
    const { container } = render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() => screen.getByText(/All members blocked/));
    const banners = container.querySelectorAll(".cid-blocked-banner");
    expect(banners.length).toBe(2);
  });

  it("identical-policy two-manifest turn renders no-diff caption", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r",
          turn_id: "t",
          manifest_count: 2,
          manifests: [
            backendManifest({ manifest_id: "cm-A", member_id: "m-A" }),
            backendManifest({ manifest_id: "cm-B", member_id: "m-B" }),
          ],
        }),
      ),
    );
    render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() =>
      screen.getByText(/No policy differences across members/),
    );
  });

  it("defensive: stray content field at drawer level never leaks in compare path", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r",
          turn_id: "t",
          manifest_count: 2,
          manifests: [
            backendManifest({ manifest_id: "cm-A", member_id: "m-A" }),
            backendManifest({
              manifest_id: "cm-B",
              member_id: "m-B",
              // Stray field — must not surface in the compare DOM.
              content: "RAW_PAYLOAD_TEXT_DO_NOT_RENDER_ALPHA42",
            }),
          ],
        }),
      ),
    );
    const { container } = render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() =>
      screen.getByRole("region", {
        name: "Per-member context comparison",
      }),
    );
    expect(container.innerHTML).not.toContain(
      "RAW_PAYLOAD_TEXT_DO_NOT_RENDER_ALPHA42",
    );
  });

  it("focused column class applied when memberId prop is set", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r",
          turn_id: "t",
          manifest_count: 2,
          manifests: [
            backendManifest({ manifest_id: "cm-full", member_id: "m-full" }),
            backendManifest({
              manifest_id: "cm-red",
              member_id: "m-redacted",
            }),
          ],
        }),
      ),
    );
    const { container } = render(
      <ContextInspectionDrawer
        runId="r"
        turnId="t"
        memberId="m-redacted"
        onClose={() => {}}
      />,
    );
    await waitFor(() =>
      screen.getByRole("region", {
        name: "Per-member context comparison",
      }),
    );
    const focusedCol = container.querySelector(
      '.cid-compare-col[data-member-id="m-redacted"]',
    );
    expect(focusedCol).not.toBeNull();
    expect(focusedCol!.classList.contains("cid-compare-col-focused")).toBe(
      true,
    );
  });

  it("drawer body never contains a raw 'content' field from a manifest", async () => {
    // Defensive: even if a future manifest accidentally contains a `content`
    // string, the drawer renders only the typed fields, never the raw blob.
    const evilManifest = {
      manifest_id: "cm-evil",
      format_version: 1,
      context_id: "ctx",
      run_id: "r",
      turn_id: "t",
      member_id: "m",
      payload_sha256: "sha",
      requested_context_access: "full_context",
      effective_context_access: "full_context",
      requested_transcript_access: "none",
      effective_transcript_access: "none",
      destination_scope: "local",
      egress_class: "local",
      source_counts: {},
      source_refs: [],
      omitted: [],
      token_estimate: {},
      blocked_reason: null,
      transform_manifest_id: null,
      visibility_plan_id: null,
      f030_audit_id: null,
      // ↓ this field is NOT in our type and must not appear in DOM:
      content: "RAW_PAYLOAD_TEXT_DO_NOT_RENDER_ALPHA42",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({
          run_id: "r",
          turn_id: "t",
          manifest_count: 1,
          manifests: [evilManifest],
        }),
      ),
    );
    const { container } = render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() => screen.getByText("Effective policy"));
    expect(container.innerHTML).not.toContain(
      "RAW_PAYLOAD_TEXT_DO_NOT_RENDER_ALPHA42",
    );
  });
});

// QA P1 #1 — round-level fetch (the demo path uses this; without it the
// compare strip is structurally unreachable from a normal Inspect click).
describe("ContextInspectionDrawer — round-level fetch", () => {
  it("calls /rounds/{N}/inspection and renders compare strip when round prop is set", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({
        run_id: "r-1",
        round: 1,
        manifest_count: 2,
        manifests: [
          {
            manifest_id: "cm-full",
            format_version: 1,
            context_id: "ctx-A",
            run_id: "r-1",
            turn_id: "m-1-r1",
            member_id: "m-1",
            payload_sha256: "deadbeef".repeat(8),
            requested_context_access: "full_context",
            effective_context_access: "full_context",
            requested_transcript_access: "all_messages",
            effective_transcript_access: "all_messages",
            destination_scope: "local",
            egress_class: "local",
            source_counts: { retrieved_snippet: 2 },
            source_refs: [],
            omitted: [],
            token_estimate: { input: 0, output: 0 },
            blocked_reason: null,
            transform_manifest_id: null,
            visibility_plan_id: null,
            f030_audit_id: null,
          },
          {
            manifest_id: "cm-redacted",
            format_version: 1,
            context_id: "ctx-B",
            run_id: "r-1",
            turn_id: "m-2-r1",
            member_id: "m-2",
            payload_sha256: "beefdead".repeat(8),
            requested_context_access: "redacted_summary",
            effective_context_access: "redacted_summary",
            requested_transcript_access: "none",
            effective_transcript_access: "none",
            destination_scope: "local",
            egress_class: "local",
            source_counts: { summary_only: 1 },
            source_refs: [],
            omitted: [],
            token_estimate: { input: 0, output: 0 },
            blocked_reason: null,
            transform_manifest_id: null,
            visibility_plan_id: null,
            f030_audit_id: null,
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ContextInspectionDrawer
        runId="r-1"
        round={1}
        memberId="m-1"
        turnId="m-1-r1"
        onClose={() => {}}
      />,
    );

    // Wait for the compare region to mount.
    await waitFor(() =>
      screen.getByRole("region", { name: "Per-member context comparison" }),
    );
    // Confirm the round endpoint (not the turn endpoint) was hit.
    const url = (fetchMock.mock.calls[0][0] as string) ?? "";
    expect(url).toContain("/rounds/1/inspection");
    expect(url).not.toContain("/turns/");
  });

  it("falls back to single-card view when round endpoint returns one manifest", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({
        run_id: "r-1",
        round: 1,
        manifest_count: 1,
        manifests: [
          {
            manifest_id: "cm-solo",
            format_version: 1,
            context_id: "ctx",
            run_id: "r-1",
            turn_id: "m-1-r1",
            member_id: "m-1",
            payload_sha256: "abc",
            requested_context_access: "prompt_only",
            effective_context_access: "prompt_only",
            requested_transcript_access: "own_messages",
            effective_transcript_access: "own_messages",
            destination_scope: "local",
            egress_class: "local",
            source_counts: {},
            source_refs: [],
            omitted: [],
            token_estimate: { input: 0, output: 0 },
            blocked_reason: null,
            transform_manifest_id: null,
            visibility_plan_id: null,
            f030_audit_id: null,
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ContextInspectionDrawer
        runId="r-1"
        round={1}
        memberId="m-1"
        turnId="m-1-r1"
        onClose={() => {}}
      />,
    );
    await waitFor(() => screen.getByText("Effective policy"));
    // Compare region must NOT render for a single manifest.
    expect(
      screen.queryByRole("region", { name: "Per-member context comparison" }),
    ).toBeNull();
  });
});

// F031-DEMO-A11Y-SWEEP Task 3 — focus-trap + focus-restoration coverage.
// A keyboard-only operator must be able to (a) cycle Tab/Shift+Tab
// within the drawer without falling off into the page beneath, and (b)
// land back on the originating Inspect button after every close path.
describe("ContextInspectionDrawer — focus trap + restoration", () => {
  function setupSingleManifestFetch() {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            run_id: "r",
            turn_id: "t",
            manifest_count: 1,
            manifests: [
              {
                manifest_id: "cm-only",
                format_version: 1,
                context_id: "ctx",
                run_id: "r",
                turn_id: "t",
                member_id: "m-only",
                payload_sha256: "deadbeef".repeat(8),
                requested_context_access: "full_context",
                effective_context_access: "full_context",
                requested_transcript_access: "all_messages",
                effective_transcript_access: "all_messages",
                destination_scope: "local",
                egress_class: "local",
                source_counts: {},
                source_refs: [],
                omitted: [],
                token_estimate: {},
                blocked_reason: null,
                transform_manifest_id: null,
                visibility_plan_id: null,
                f030_audit_id: null,
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
  }

  it("focus_trap_wraps_forward", async () => {
    setupSingleManifestFetch();
    render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() => screen.getByText("Effective policy"));

    const dialog = screen.getByRole("dialog", { name: "Inspection drawer" });
    const focusables = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => !el.hasAttribute("disabled"));
    expect(focusables.length).toBeGreaterThan(0);
    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    last.focus();
    expect(document.activeElement).toBe(last);
    const tabEvt = new KeyboardEvent("keydown", {
      key: "Tab",
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      document.dispatchEvent(tabEvt);
    });
    expect(document.activeElement).toBe(first);
  });

  it("focus_trap_wraps_backward", async () => {
    setupSingleManifestFetch();
    render(
      <ContextInspectionDrawer runId="r" turnId="t" onClose={() => {}} />,
    );
    await waitFor(() => screen.getByText("Effective policy"));

    const dialog = screen.getByRole("dialog", { name: "Inspection drawer" });
    const focusables = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => !el.hasAttribute("disabled"));
    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    first.focus();
    expect(document.activeElement).toBe(first);
    const shiftTabEvt = new KeyboardEvent("keydown", {
      key: "Tab",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      document.dispatchEvent(shiftTabEvt);
    });
    expect(document.activeElement).toBe(last);
  });

  it("focus_returns_to_inspect_button_on_esc", async () => {
    setupSingleManifestFetch();
    const opener = document.createElement("button");
    opener.textContent = "Inspect";
    document.body.appendChild(opener);
    opener.focus();
    expect(document.activeElement).toBe(opener);

    function Host() {
      const [open, setOpen] = useState(true);
      if (!open) return null;
      return (
        <ContextInspectionDrawer
          runId="r"
          turnId="t"
          onClose={() => setOpen(false)}
        />
      );
    }
    render(<Host />);
    await waitFor(() => screen.getByText("Effective policy"));

    // Sanity: drawer focused away from opener while open.
    expect(document.activeElement).not.toBe(opener);

    act(() => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(opener);
    });
    document.body.removeChild(opener);
  });

  it("focus_returns_to_inspect_button_on_backdrop_click", async () => {
    setupSingleManifestFetch();
    const opener = document.createElement("button");
    opener.textContent = "Inspect";
    document.body.appendChild(opener);
    opener.focus();

    function Host() {
      const [open, setOpen] = useState(true);
      if (!open) return null;
      return (
        <ContextInspectionDrawer
          runId="r"
          turnId="t"
          onClose={() => setOpen(false)}
        />
      );
    }
    const { container } = render(<Host />);
    await waitFor(() => screen.getByText("Effective policy"));

    const backdrop = container.querySelector(".cid-backdrop");
    expect(backdrop).not.toBeNull();
    act(() => {
      fireEvent.click(backdrop!);
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(opener);
    });
    document.body.removeChild(opener);
  });

  it("focus_returns_to_inspect_button_on_close_button", async () => {
    setupSingleManifestFetch();
    const opener = document.createElement("button");
    opener.textContent = "Inspect";
    document.body.appendChild(opener);
    opener.focus();

    function Host() {
      const [open, setOpen] = useState(true);
      if (!open) return null;
      return (
        <ContextInspectionDrawer
          runId="r"
          turnId="t"
          onClose={() => setOpen(false)}
        />
      );
    }
    render(<Host />);
    await waitFor(() => screen.getByText("Effective policy"));

    const closeBtn = screen.getByRole("button", {
      name: "Close inspection drawer",
    });
    act(() => {
      fireEvent.click(closeBtn);
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(opener);
    });
    document.body.removeChild(opener);
  });
});

// ──────────────────────────────────────────────────────────────────────
// QA P2 #6 (2026-06-12): parent re-render must not reset focus.
//
// CouncilShell passes an inline `() => closeDrawer()` callback to the
// drawer. The shell re-renders on every poll tick (~350ms during a
// live run) so `onClose` identity changes constantly. Pre-fix, the
// keydown effect had `[onClose]` in its dep array, which re-ran on
// every change — re-installing the listener AND re-focusing Close,
// yanking the user out of mid-Tab navigation.
// ──────────────────────────────────────────────────────────────────────

describe("ContextInspectionDrawer — focus stability under parent re-render", () => {
  it("Tab focus is NOT reset when onClose prop identity changes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            run_id: "r",
            turn_id: "t",
            manifest_count: 1,
            manifests: [
              {
                manifest_id: "cm-stable",
                format_version: 1,
                context_id: "ctx",
                run_id: "r",
                turn_id: "t",
                member_id: "m",
                payload_sha256: "deadbeef".repeat(8),
                requested_context_access: "full_context",
                effective_context_access: "full_context",
                requested_transcript_access: "all_messages",
                effective_transcript_access: "all_messages",
                destination_scope: "local",
                egress_class: "local",
                source_counts: {},
                source_refs: [],
                omitted: [],
                token_estimate: { input: 0, output: 0 },
                blocked_reason: null,
                transform_manifest_id: null,
                visibility_plan_id: null,
                f030_audit_id: null,
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    // Host mimics CouncilShell: re-renders on each "tick" with a fresh
    // inline `onClose` lambda identity. The drawer should hold its
    // focus across these re-renders.
    function Host() {
      const [tick, setTick] = useState(0);
      return (
        <>
          <button
            type="button"
            data-testid="tick-button"
            onClick={() => setTick((t) => t + 1)}
          >
            tick={tick}
          </button>
          <ContextInspectionDrawer
            runId="r"
            turnId="t"
            onClose={() => {
              /* fresh identity every render */
            }}
          />
        </>
      );
    }

    render(<Host />);
    await waitFor(() => screen.getByText("Effective policy"));

    const closeBtn = screen.getByRole("button", {
      name: "Close inspection drawer",
    });
    // Initial focus lands on Close (mount-only effect).
    await waitFor(() => {
      expect(document.activeElement).toBe(closeBtn);
    });

    // User Tabs forward → focus moves off Close to the next focusable.
    // happy-dom's default Tab semantics put focus on the tick button
    // (next focusable in document order). We don't care exactly which
    // element gains focus — only that it ISN'T Close anymore.
    const tickBtn = screen.getByTestId("tick-button");
    act(() => {
      tickBtn.focus();
    });
    expect(document.activeElement).toBe(tickBtn);

    // Parent re-renders with a fresh inline onClose lambda.
    act(() => {
      fireEvent.click(tickBtn);
    });

    // Focus must NOT have been yanked back to Close. The pre-fix
    // behavior would have re-fired the focus effect when [onClose]
    // changed, putting activeElement back on closeBtn.
    expect(document.activeElement).not.toBe(closeBtn);
    expect(document.activeElement).toBe(tickBtn);
  });

  it("Esc still calls the LATEST onClose after the prop identity changes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({}), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    // onClose prop is swapped between two distinct vi.fn() mocks. After
    // the swap, Esc must call the NEW one — not the stale captured one
    // from the original keydown listener install.
    const first = vi.fn();
    const second = vi.fn();

    function Host() {
      const [use, setUse] = useState<"first" | "second">("first");
      return (
        <>
          <button
            type="button"
            data-testid="swap"
            onClick={() => setUse("second")}
          >
            swap
          </button>
          <ContextInspectionDrawer
            runId="r"
            turnId="t"
            onClose={use === "first" ? first : second}
          />
        </>
      );
    }

    render(<Host />);
    await waitFor(() =>
      screen.getByText(/No manifest yet for this turn/),
    );

    // Swap to the new onClose.
    act(() => {
      fireEvent.click(screen.getByTestId("swap"));
    });

    // Esc routes through the ref to the latest callback.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalled();
  });
});
