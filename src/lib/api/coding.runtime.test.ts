import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ sidecarFetch: vi.fn() }));
import { sidecarFetch } from "../api";
import {
  detectRuntimeProfiles,
  getRuntimeSession,
  getRuntimeSessionLogs,
  listRuntimeProfiles,
  runRuntimeHealthCheck,
  runRuntimeTest,
  runtimeProfileFrom,
  runtimeProfileToWire,
  requestRuntimeRepair,
  runCliTranscript,
  RuntimeCliArgsError,
  setupRuntimeProfile,
  startRuntimeProfile,
  stopRuntimeProfile,
  upsertRuntimeProfile,
  type RuntimeProfile,
} from "./coding";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;
const UI_ORIGIN = { "x-errorta-origin": "tauri-ui" };

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function wireProfile(over: Record<string, unknown> = {}) {
  return {
    schema_version: "coding_runtime_profile.v1",
    profile_id: "default",
    project_id: "proj",
    kind: "web",
    runtime_mode: "managed_local",
    working_dir: ".",
    setup: [["npm", "install"]],
    start: ["npm", "run", "dev"],
    stop: null,
    health: { type: "http", url: "http://127.0.0.1:{port}", timeout_seconds: 20 },
    demo: { type: "url", url: "http://127.0.0.1:{port}" },
    ports: [{ name: "web", container_port: null, preferred: 5173 }],
    env_required: ["OPENAI_API_KEY"],
    tests: ["unit"],
    sandbox: "auto",
    safety_warnings: ["network access"],
    created_by: "detector",
    updated_at: "2026-06-20T10:00:00Z",
    ...over,
  };
}

function wireSession(over: Record<string, unknown> = {}) {
  return {
    session_id: "s1",
    profile_id: "default",
    state: "healthy",
    pgid: 123,
    started_at: "2026-06-20T10:00:00Z",
    ended_at: null,
    allocated_ports: [5173],
    sandbox_backend: "seatbelt",
    health_status: { ok: true, detail: "HTTP 200" },
    log_ref: "logs/s1",
    exit_code: null,
    error: null,
    ...over,
  };
}

afterEach(() => vi.clearAllMocks());

describe("coding runtime API", () => {
  it("maps runtime profiles from the route contract", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ profiles: [wireProfile()] }));

    const profiles = await listRuntimeProfiles("proj");

    expect(mockFetch).toHaveBeenCalledWith("/coding/projects/proj/runtime/profiles");
    expect(profiles[0]).toMatchObject({
      schemaVersion: "coding_runtime_profile.v1",
      profileId: "default",
      projectId: "proj",
      kind: "web",
      runtimeMode: "managed_local",
      workingDir: ".",
      setup: [["npm", "install"]],
      start: ["npm", "run", "dev"],
      envRequired: ["OPENAI_API_KEY"],
      tests: ["unit"],
      sandbox: "auto",
      safetyWarnings: ["network access"],
      createdBy: "detector",
    });
    expect(profiles[0].health).toEqual({
      type: "http",
      url: "http://127.0.0.1:{port}",
      timeoutSeconds: 20,
    });
    expect(profiles[0].demo).toEqual({
      type: "url",
      url: "http://127.0.0.1:{port}",
      path: null,
      timeoutSeconds: null,
    });
    expect(profiles[0].ports[0]).toEqual({
      name: "web",
      containerPort: null,
      preferred: 5173,
    });
  });

  it("upserts profile JSON as snake_case and sends the UI origin header", async () => {
    const profile = runtimeProfileFrom(wireProfile()) as RuntimeProfile;
    mockFetch.mockResolvedValueOnce(jsonResponse({ profile: wireProfile({ sandbox: "seatbelt" }) }));

    const saved = await upsertRuntimeProfile("proj", profile);

    expect(mockFetch).toHaveBeenCalledWith(
      "/coding/projects/proj/runtime/profiles/default",
      {
        method: "PUT",
        headers: UI_ORIGIN,
        body: JSON.stringify(runtimeProfileToWire(profile)),
      },
    );
    expect(saved.sandbox).toBe("seatbelt");
  });

  it("calls mutation routes with the UI origin header", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse({ proposed: [wireProfile({ profile_id: "detected" })] }))
      .mockResolvedValueOnce(jsonResponse({ session: wireSession({ state: "starting" }) }))
      .mockResolvedValueOnce(jsonResponse({ session: wireSession({ state: "running" }) }))
      .mockResolvedValueOnce(jsonResponse({ stopped: true }))
      .mockResolvedValueOnce(jsonResponse({ health_status: { ok: false, detail: "timeout" } }))
      .mockResolvedValueOnce(jsonResponse({ result: { kind: "demo_smoke", passed: false, detail: "failed" } }));

    expect((await detectRuntimeProfiles("proj"))[0].profileId).toBe("detected");
    expect((await setupRuntimeProfile("proj", "default")).state).toBe("starting");
    expect((await startRuntimeProfile("proj", "default")).state).toBe("running");
    expect(await stopRuntimeProfile("proj", "default")).toBe(true);
    expect(await runRuntimeHealthCheck("proj", "default")).toEqual({ ok: false, detail: "timeout" });
    expect(await runRuntimeTest("proj", "default", "unit")).toMatchObject({
      kind: "demo_smoke",
      passed: false,
      detail: "failed",
    });

    expect(mockFetch.mock.calls).toEqual([
      ["/coding/projects/proj/runtime/detect", { method: "POST", headers: UI_ORIGIN, body: "{}" }],
      [
        "/coding/projects/proj/runtime/default/setup",
        { method: "POST", headers: UI_ORIGIN, body: JSON.stringify({ confirm: true }) },
      ],
      ["/coding/projects/proj/runtime/default/start", { method: "POST", headers: UI_ORIGIN, body: "{}" }],
      ["/coding/projects/proj/runtime/default/stop", { method: "POST", headers: UI_ORIGIN, body: "{}" }],
      ["/coding/projects/proj/runtime/default/health-check", { method: "POST", headers: UI_ORIGIN, body: "{}" }],
      [
        "/coding/projects/proj/runtime/default/test",
        { method: "POST", headers: UI_ORIGIN, body: JSON.stringify({ kind: "unit" }) },
      ],
    ]);
  });

  it("maps sessions and capped logs", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse({ session: wireSession({ sandbox_backend: "none" }) }))
      .mockResolvedValueOnce(jsonResponse({ lines: ["ready", "redacted"], truncated: true }));

    const session = await getRuntimeSession("proj", "s1");
    const logs = await getRuntimeSessionLogs("proj", "s1");

    expect(mockFetch.mock.calls[0][0]).toBe("/coding/projects/proj/runtime/sessions/s1");
    expect(session).toMatchObject({
      sessionId: "s1",
      profileId: "default",
      state: "healthy",
      sandboxBackend: "none",
      allocatedPorts: [5173],
      healthStatus: { ok: true, detail: "HTTP 200" },
    });
    expect(logs).toEqual({ lines: ["ready", "redacted"], truncated: true });
  });

  it("requests a runtime repair task (S5) with the bound session", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ task: { task_id: "t-9", title: "Fix runtime preview: default (crashed)" } }),
    );

    const out = await requestRuntimeRepair("proj", "default", "s-crash");

    expect(mockFetch).toHaveBeenCalledWith(
      "/coding/projects/proj/runtime/default/repair",
      expect.objectContaining({
        method: "POST",
        headers: UI_ORIGIN,
        body: JSON.stringify({ session_id: "s-crash" }),
      }),
    );
    expect(out).toEqual({ taskId: "t-9", title: "Fix runtime preview: default (crashed)" });
  });

  it("requests a runtime repair task with no session", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ task: { task_id: "t-1", title: "Fix" } }));
    await requestRuntimeRepair("proj", "default");
    expect(mockFetch.mock.calls[0][1]).toMatchObject({ body: JSON.stringify({}) });
  });

  // F101-01: the demo `file` path + per-profile CLI time-box must survive the
  // wire round-trip (previously `path` was dropped on read AND write).
  it("round-trips demo.path and demo.timeoutSeconds without dropping them", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        profiles: [
          wireProfile({
            demo: { type: "file", url: null, path: "index.html", timeout_seconds: 45 },
          }),
        ],
      }),
    );
    const profiles = await listRuntimeProfiles("proj");
    expect(profiles[0].demo).toEqual({
      type: "file",
      url: null,
      path: "index.html",
      timeoutSeconds: 45,
    });
    const wire = runtimeProfileToWire(profiles[0]);
    expect(wire.demo).toEqual({
      type: "file",
      url: null,
      path: "index.html",
      timeout_seconds: 45,
    });
  });

  // F101-02: run-cli posts to /run-cli with extra_args, maps the cli_transcript
  // session markers (kind/argv/passed), and raises a typed error on 422.
  it("runs a CLI transcript and maps the cli_transcript session markers", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        session: wireSession({
          state: "stopped",
          exit_code: 0,
          allocated_ports: [],
          kind: "cli_transcript",
          argv: ["python", "main.py", "--name", "world"],
          passed: true,
        }),
      }),
    );

    const out = await runCliTranscript("proj", "default", { extraArgs: "--name world" });

    expect(mockFetch).toHaveBeenCalledWith(
      "/coding/projects/proj/runtime/default/run-cli",
      {
        method: "POST",
        headers: { ...UI_ORIGIN, "content-type": "application/json" },
        body: JSON.stringify({ extra_args: "--name world" }),
      },
    );
    expect(out).toMatchObject({
      state: "stopped",
      exitCode: 0,
      kind: "cli_transcript",
      argv: ["python", "main.py", "--name", "world"],
      passed: true,
    });
  });

  it("forwards timeoutSeconds and omits a blank extraArgs", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ session: wireSession({ state: "starting", kind: "cli_transcript" }) }),
    );
    await runCliTranscript("proj", "default", { extraArgs: "", timeoutSeconds: 30 });
    expect(mockFetch.mock.calls[0][1]).toMatchObject({
      body: JSON.stringify({ timeout_seconds: 30 }),
    });
  });

  it("raises RuntimeCliArgsError on a 422 bad-args response", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ detail: "invalid extra_args: No closing quotation" }, 422),
    );
    await expect(
      runCliTranscript("proj", "default", { extraArgs: '--name "world' }),
    ).rejects.toBeInstanceOf(RuntimeCliArgsError);
  });
});
