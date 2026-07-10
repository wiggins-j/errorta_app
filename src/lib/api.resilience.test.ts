// F063 A1 — sidecarFetch self-heal on transport failure.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./sidecarPort", () => ({
  getSidecarBase: vi.fn(),
  ensureSidecarBase: vi.fn(),
  resetSidecarBaseCache: vi.fn(),
}));

import { SidecarUnreachableError, sidecarFetch } from "./api";
import {
  ensureSidecarBase,
  getSidecarBase,
  resetSidecarBaseCache,
} from "./sidecarPort";

const _get = getSidecarBase as unknown as ReturnType<typeof vi.fn>;
const _ensure = ensureSidecarBase as unknown as ReturnType<typeof vi.fn>;
const _reset = resetSidecarBaseCache as unknown as ReturnType<typeof vi.fn>;

function okResponse(): Response {
  return new Response("{}", { status: 200 });
}

beforeEach(() => {
  _get.mockReset().mockResolvedValue("http://127.0.0.1:1111");
  _ensure.mockReset().mockResolvedValue("http://127.0.0.1:2222");
  _reset.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("sidecarFetch self-heal", () => {
  it("retries a GET once on the re-resolved base after a transport failure", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("Load failed"))
      .mockResolvedValueOnce(okResponse());

    const r = await sidecarFetch("/healthz", { method: "GET" });
    expect(r.status).toBe(200);
    expect(_reset).toHaveBeenCalledOnce();
    expect(_ensure).toHaveBeenCalledOnce();
    // Second attempt used the fresh base.
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:2222/healthz",
      expect.anything(),
    );
  });

  it("throws SidecarUnreachableError when the GET retry also fails transport", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Load failed"));
    await expect(sidecarFetch("/healthz")).rejects.toBeInstanceOf(
      SidecarUnreachableError,
    );
    // No third attempt (recursion guard): exactly two fetch calls.
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  });

  it("retries a PUT once on the re-resolved base (idempotent: heals a stale port)", async () => {
    // Regression: saving a room (PUT /rooms/{id}) after the sidecar respawned
    // on a new port used to surface "backend isn't running" instead of healing.
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("Load failed"))
      .mockResolvedValueOnce(okResponse());

    const r = await sidecarFetch("/council/rooms/demo", {
      method: "PUT",
      body: "{}",
    });
    expect(r.status).toBe(200);
    expect(_ensure).toHaveBeenCalledOnce();
    // Second attempt used the fresh base.
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:2222/council/rooms/demo",
      expect.anything(),
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries a DELETE once on the re-resolved base (idempotent)", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("Load failed"))
      .mockResolvedValueOnce(okResponse());

    const r = await sidecarFetch("/council/rooms/demo", { method: "DELETE" });
    expect(r.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("throws SidecarUnreachableError when the PUT retry also fails transport", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Load failed"));
    await expect(
      sidecarFetch("/council/rooms/demo", { method: "PUT", body: "{}" }),
    ).rejects.toBeInstanceOf(SidecarUnreachableError);
    // Exactly two attempts (original + one retry), no infinite loop.
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  });

  it("retries a POST once when the sidecar MOVED to a new port (safe: never received)", async () => {
    // Regression (Start Run): the sidecar respawned on a new ephemeral port, so
    // the POST hit a now-dead port and was never received — retrying against the
    // re-resolved live port is safe (no double-apply) and is what makes Start
    // Run work instead of throwing a spurious "sidecar unreachable".
    _get.mockResolvedValue("http://127.0.0.1:1111"); // cached (now-dead) base
    _ensure.mockResolvedValue("http://127.0.0.1:2222"); // re-resolved live base
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("Load failed"))
      .mockResolvedValueOnce(okResponse());

    const r = await sidecarFetch("/coding/projects/p/run", {
      method: "POST",
      body: "{}",
    });
    expect(r.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:2222/coding/projects/p/run",
      expect.anything(),
    );
  });

  it("does NOT retry a POST when the port is UNCHANGED (could have been received)", async () => {
    // Same port -> the write might have reached the server before the drop, so a
    // blind retry could double-apply. Re-resolve and surface a clear error.
    _get.mockResolvedValue("http://127.0.0.1:1111");
    _ensure.mockResolvedValue("http://127.0.0.1:1111"); // unchanged
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("Load failed"));
    await expect(
      sidecarFetch("/council/runs", { method: "POST", body: "{}" }),
    ).rejects.toBeInstanceOf(SidecarUnreachableError);
    expect(_ensure).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does NOT retry an HTTP error (4xx/5xx is not a transport failure)", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("nope", { status: 500 }));
    const r = await sidecarFetch("/healthz");
    expect(r.status).toBe(500);
    expect(_ensure).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledOnce();
  });
});

describe("sidecarFetch header hygiene", () => {
  it("does not duplicate content-type when the caller also sets it (WKWebView 422 regression)", async () => {
    // A caller's lowercase `content-type` used to coexist with sidecarFetch's
    // auto-added `Content-Type` as two record keys; WKWebView combined them into
    // `application/json, application/json`, which the sidecar rejected with 422.
    // Now the merge is case-insensitive: exactly one clean content-type.
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("{}", { status: 200 }));

    await sidecarFetch("/alpha/activate", {
      method: "POST",
      headers: { "x-errorta-origin": "tauri-ui", "content-type": "application/json" },
      body: JSON.stringify({ code: "ERRT-XXXX-XXXX" }),
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const sent = new Headers(init.headers as HeadersInit);
    expect(sent.get("content-type")).toBe("application/json");
    expect(sent.get("x-errorta-origin")).toBe("tauri-ui");
    expect(sent.get("accept")).toBe("application/json");
  });

  it("auto-adds content-type for a body-bearing request with no caller header", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("{}", { status: 200 }));

    await sidecarFetch("/council/runs", { method: "POST", body: "{}" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const sent = new Headers(init.headers as HeadersInit);
    expect(sent.get("content-type")).toBe("application/json");
  });

  it("does not set content-type for FormData bodies (browser owns the boundary)", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("{}", { status: 200 }));

    await sidecarFetch("/alpha/feedback/submit", {
      method: "POST",
      body: new FormData(),
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const sent = new Headers(init.headers as HeadersInit);
    expect(sent.get("content-type")).toBeNull();
  });
});
