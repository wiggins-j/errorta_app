// F031-DEMO-CORPUS Task 3 — seed-flow unit tests.
//
// Locks:
//   - Happy path posts /welcome/install THEN /council/rooms with
//     `corpus_ids=["welcome"]` + `metadata.demo_marker`.
//   - Reused path still posts /council/rooms with the same body shape.
//   - Failed corpus seed throws DemoSeedError and does NOT POST the room.
//   - skipCorpus override posts an empty-corpus room without calling
//     /welcome/install.
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEMO_PROMPT,
  DEMO_ROOM_MARKER,
  DemoSeedError,
  seedDemoRoom,
} from "./CouncilDemoRoomSeed";

interface CapturedCall {
  url: string;
  method: string;
  body: unknown;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetchMock(impls: Array<(call: CapturedCall) => Response>) {
  const calls: CapturedCall[] = [];
  let idx = 0;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const call: CapturedCall = {
      url,
      method: init?.method ?? "GET",
      body:
        typeof init?.body === "string"
          ? JSON.parse(init.body as string)
          : init?.body,
    };
    calls.push(call);
    const impl = impls[Math.min(idx, impls.length - 1)];
    idx += 1;
    return impl(call);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("seedDemoRoom — happy path posts ensureCorpus then room with welcome corpus_id", () => {
  it("posts /welcome/install then /council/rooms with corpus_ids + demo_marker", async () => {
    const { calls } = installFetchMock([
      () =>
        jsonResponse({
          corpus_name: "welcome",
          suggested_prompt: "What does Errorta do?",
          files_ingested: 6,
          bytes_downloaded: 12345,
          sha256: "deadbeef",
          f004_invoked: true,
          f004_error: null,
        }),
      () => jsonResponse({ room: { id: "demo-1" } }),
    ]);

    await seedDemoRoom();

    expect(calls.length).toBe(2);
    expect(calls[0].method).toBe("POST");
    expect(calls[0].url).toContain("/welcome/install");
    expect(calls[1].method).toBe("POST");
    expect(calls[1].url).toContain("/council/rooms");
    const roomBody = calls[1].body as Record<string, unknown>;
    expect(roomBody.corpus_ids).toEqual(["welcome"]);
    expect(roomBody.metadata).toEqual({ demo_marker: DEMO_ROOM_MARKER });
  });
});

describe("seedDemoRoom — reused path", () => {
  it("still posts a room with corpus_ids=['welcome'] + demo_marker when /welcome/install returns success", async () => {
    // The route always returns a fresh install response (no separate "reused"
    // status surface). The client treats f004_error=null as success in both
    // first and second seed cycles; the underlying F007 install handles the
    // idempotence on disk.
    const { calls } = installFetchMock([
      () =>
        jsonResponse({
          corpus_name: "welcome",
          f004_invoked: true,
          f004_error: null,
        }),
      () => jsonResponse({ room: { id: "demo-1" } }),
    ]);

    await seedDemoRoom();

    expect(calls.length).toBe(2);
    const roomBody = calls[1].body as Record<string, unknown>;
    expect(roomBody.corpus_ids).toEqual(["welcome"]);
    expect(roomBody.metadata).toEqual({ demo_marker: DEMO_ROOM_MARKER });
  });
});

describe("seedDemoRoom — failed path", () => {
  it("throws DemoSeedError and does NOT POST /council/rooms when corpus seed fails (HTTP error)", async () => {
    const { calls } = installFetchMock([
      () => jsonResponse({ detail: "sha256 mismatch" }, 409),
    ]);

    await expect(seedDemoRoom()).rejects.toBeInstanceOf(DemoSeedError);

    // Only the corpus call landed. No /council/rooms POST.
    expect(calls.length).toBe(1);
    expect(calls[0].url).toContain("/welcome/install");
    expect(
      calls.some((c) => c.url.includes("/council/rooms")),
    ).toBe(false);
  });

  it("throws DemoSeedError carrying the f004_error string when ingest fails", async () => {
    installFetchMock([
      () =>
        jsonResponse({
          corpus_name: "welcome",
          f004_invoked: true,
          f004_error: "ingest exploded: file too large",
        }),
    ]);

    try {
      await seedDemoRoom();
      throw new Error("should have thrown");
    } catch (err) {
      expect(err).toBeInstanceOf(DemoSeedError);
      expect((err as DemoSeedError).structuredReason).toContain(
        "ingest exploded",
      );
    }
  });
});

describe("seedDemoRoom — skipCorpus override", () => {
  it("posts empty-corpus room without calling /welcome/install when skipCorpus=true", async () => {
    const { calls } = installFetchMock([
      () => jsonResponse({ room: { id: "demo-1" } }),
    ]);

    await seedDemoRoom({ skipCorpus: true });

    // Single call straight to /council/rooms, no /welcome/install.
    expect(calls.length).toBe(1);
    expect(calls[0].url).toContain("/council/rooms");
    expect(calls[0].url).not.toContain("/welcome/install");
    const roomBody = calls[0].body as Record<string, unknown>;
    expect(roomBody.corpus_ids).toBeUndefined();
    expect(roomBody.metadata).toBeUndefined();
  });
});

describe("DEMO_PROMPT / DEMO_ROOM_MARKER constants", () => {
  it("DEMO_ROOM_MARKER is a stable string", () => {
    expect(DEMO_ROOM_MARKER).toBe("council-demo-room");
  });
  it("DEMO_PROMPT is non-empty and not a generic greeting", () => {
    expect(DEMO_PROMPT.length).toBeGreaterThan(20);
    expect(DEMO_PROMPT).not.toMatch(/^hello/i);
    // Should contain at least one sentence terminator.
    expect(DEMO_PROMPT).toMatch(/[.?!]/);
  });
});
