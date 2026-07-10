// F031 Phase 2 — API adapter tests. Locks invariant 11 (snake_case
// backend names are normalized to camelCase by the api/council.ts client).
import { describe, expect, it, vi, afterEach } from "vitest";

import { mapBackendRunState } from "./types";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("mapBackendRunState", () => {
  it.each([
    ["created", "idle"],
    ["running", "running"],
    ["paused", "paused"],
    ["finalizing", "finalizing"],
    ["awaiting_user_decision", "awaiting_decision"],
    ["completed", "done"],
    ["failed", "failed"],
    ["cancelled", "cancelled"],
  ])("backend %s → ui %s", (input, expected) => {
    expect(mapBackendRunState(input)).toBe(expected);
  });

  it("unknown backend status → 'unknown' (invariant 4 fail-closed)", () => {
    expect(mapBackendRunState("teleporting")).toBe("unknown");
  });
});

describe("council api adapter", () => {
  it("normalizes snake_case backend fields into camelCase view models", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          rooms: [
            {
              id: "rm-1",
              name: "Room 1",
              updated_at: "2026-06-11T00:00:00Z",
              revision: 3,
              status_hint: "ready",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const mod = await import("../../lib/api/council");
    const rooms = await mod.listRooms();
    expect(rooms).toEqual([
      {
        id: "rm-1",
        name: "Room 1",
        updatedAt: "2026-06-11T00:00:00Z",
        revision: 3,
        statusHint: "ready",
      },
    ]);
  });
});

describe("F037 expert callout client", () => {
  it("requestCallout posts snake_case body with the UI origin header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ callout_id: "co_1", status: "queued" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("../../lib/api/council");
    const res = await mod.requestCallout("r-1", {
      targetId: "deep-reviewer",
      question: "review this",
    });
    expect(res).toEqual({ calloutId: "co_1", status: "queued" });
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe("POST");
    // sidecarFetch builds headers via a `Headers` object (F-DIST-01 WKWebView
    // fix), so read the origin header through the Headers API, not plain-object
    // indexing.
    expect(new Headers(init.headers).get("x-errorta-origin")).toBe("tauri-ui");
    expect(JSON.parse(init.body)).toEqual({
      target_id: "deep-reviewer",
      question: "review this",
      reason_code: "user_requested",
    });
  });

  it("listCallouts normalizes snake_case records to camelCase", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: "r-1",
          callouts: [
            {
              callout_id: "co_1",
              target_id: "deep-reviewer",
              reason_code: "user_requested",
              question: "q",
              requested_by: { type: "user" },
              state: "completed",
              advisory: true,
              approval: null,
              reject_reason: null,
              answer_event_id: "evt_9",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("../../lib/api/council");
    const callouts = await mod.listCallouts("r-1");
    expect(callouts).toEqual([
      {
        calloutId: "co_1",
        targetId: "deep-reviewer",
        reasonCode: "user_requested",
        question: "q",
        requestedBy: { type: "user" },
        state: "completed",
        advisory: true,
        approval: null,
        rejectReason: null,
        answerEventId: "evt_9",
      },
    ]);
  });

  it("approveCallout sends the UI origin header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ callout: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("../../lib/api/council");
    await mod.approveCallout("r-1", "co_1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/council/runs/r-1/callouts/co_1/approve");
    // sidecarFetch builds headers via a `Headers` object (F-DIST-01 WKWebView
    // fix), so read the origin header through the Headers API, not plain-object
    // indexing.
    expect(new Headers(init.headers).get("x-errorta-origin")).toBe("tauri-ui");
  });
});

describe("getTurnInspection (F031-08, Phase 5 Task 1)", () => {
  it("normalizes manifest snake_case → camelCase + nested source_refs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: "r-1",
          turn_id: "m-full-r1",
          manifest_count: 1,
          manifests: [
            {
              manifest_id: "cm-aa",
              format_version: 1,
              context_id: "ctx-zz",
              run_id: "r-1",
              turn_id: "m-full-r1",
              member_id: "m-full",
              payload_sha256: "deadbeef".repeat(8),
              requested_context_access: "full_context",
              effective_context_access: "full_context",
              requested_transcript_access: "all_messages",
              effective_transcript_access: "all_messages",
              destination_scope: "local",
              egress_class: "local",
              source_counts: { task_instructions: 1, user_prompt: 1, retrieved_snippet: 2 },
              source_refs: [
                {
                  class_: "retrieved_snippet",
                  corpus_id: "aerospace",
                  chunk_id: "ch-001",
                  citation_id: "ct-001",
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
              omitted: [{ reason: "token_budget", class_: "retrieved_snippet" }],
              token_estimate: { input: 220, output: 0 },
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
              visibility_plan_id: "vp-xy",
              f030_audit_id: null,
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("../../lib/api/council");
    const r = await mod.getTurnInspection("r-1", "m-full-r1");
    expect(r).not.toBeNull();
    expect(r!.runId).toBe("r-1");
    expect(r!.manifestCount).toBe(1);
    expect(r!.manifests[0].effectiveContextAccess).toBe("full_context");
    expect(r!.manifests[0].egressClass).toBe("local");
    expect(r!.manifests[0].sourceRefs[0].class_).toBe("retrieved_snippet");
    expect(r!.manifests[0].sourceRefs[0].corpusId).toBe("aerospace");
    expect(r!.manifests[0].sourceRefs[1].class_).toBe("tool_result");
    expect(r!.manifests[0].sourceRefs[1].toolId).toBe("web_fetch");
    expect(r!.manifests[0].sourceRefs[1].toolCallId).toBe("tc-abc123");
    expect(r!.manifests[0].sourceRefs[1].argsSha256).toBe("d".repeat(64));
    expect(r!.manifests[0].sourceRefs[1].toolEgressClass).toBe("remote_eligible");
    expect(r!.manifests[0].visibilityPlanId).toBe("vp-xy");
    expect(r!.manifests[0].f030AuditId).toBeNull();
    expect(r!.manifests[0].steward?.packetId).toBe("sp_abc123");
    expect(r!.manifests[0].steward?.contentSha256).toBe("c".repeat(64));
    expect(r!.manifests[0].steward?.coverage?.toSequence).toBe(12);
    expect(r!.manifests[0].steward?.recentFullMessageCount).toBe(1);
  });

  it("returns null on 404 (drawer renders empty state)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("{}", {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("../../lib/api/council");
    const r = await mod.getTurnInspection("r-x", "t-x");
    expect(r).toBeNull();
  });

  it("re-throws on non-404 errors", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("oops", { status: 500 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("../../lib/api/council");
    await expect(mod.getTurnInspection("r-x", "t-x")).rejects.toThrow(/HTTP 500/);
  });
});

describe("getStewardPacket", () => {
  it("fetches packet audit bodies by run + packet id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          packet_id: "sp_abc123",
          user_goal: { text: "Goal", source_event_ids: ["ev-1"] },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("../../lib/api/council");
    const packet = await mod.getStewardPacket("run/needs encoding", "sp_abc123");
    expect(packet?.packet_id).toBe("sp_abc123");
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      "/council/runs/run%2Fneeds%20encoding/steward-packets/sp_abc123",
    );
  });
});
