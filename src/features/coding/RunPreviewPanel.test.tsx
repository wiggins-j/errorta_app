import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const shellMocks = vi.hoisted(() => ({
  open: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@tauri-apps/plugin-shell", () => ({
  open: shellMocks.open,
}));

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    listRuntimeProfiles: vi.fn(),
    upsertRuntimeProfile: vi.fn(),
    detectRuntimeProfiles: vi.fn(),
    setupRuntimeProfile: vi.fn(),
    startRuntimeProfile: vi.fn(),
    runCliTranscript: vi.fn(),
    stopRuntimeProfile: vi.fn(),
    getRuntimeSession: vi.fn(),
    getRuntimeSessionLogs: vi.fn(),
    runRuntimeHealthCheck: vi.fn(),
    runRuntimeTest: vi.fn(),
    resolveRuntimeRun: vi.fn(),
  };
});

import * as api from "../../lib/api/coding";
import RunPreviewPanel from "./RunPreviewPanel";
import type { RuntimeProfile, RuntimeSession } from "../../lib/api/coding";

function profile(over: Partial<RuntimeProfile> = {}): RuntimeProfile {
  return {
    schemaVersion: "coding_runtime_profile.v1",
    profileId: "default",
    projectId: "proj",
    kind: "web",
    runtimeMode: "managed_local",
    workingDir: ".",
    setup: [["npm", "install"]],
    start: ["npm", "run", "dev"],
    stop: null,
    health: { type: "http", url: "http://127.0.0.1:{port}", timeoutSeconds: 20 },
    demo: { type: "url", url: "http://127.0.0.1:{port}", path: null, timeoutSeconds: null },
    ports: [{ name: "web", containerPort: null, preferred: 5173 }],
    envRequired: [],
    tests: ["unit"],
    sandbox: "auto",
    safetyWarnings: [],
    createdBy: "detector",
    updatedAt: "2026-06-20T10:00:00Z",
    ...over,
  };
}

function session(over: Partial<RuntimeSession> = {}): RuntimeSession {
  return {
    sessionId: "s1",
    profileId: "default",
    state: "running",
    pgid: 123,
    startedAt: "2026-06-20T10:00:00Z",
    endedAt: null,
    allocatedPorts: [5173],
    sandboxBackend: "seatbelt",
    healthStatus: null,
    logRef: "logs/s1",
    exitCode: null,
    error: null,
    kind: null,
    argv: null,
    passed: null,
    trustTier: null,
    screenshotRef: null,
    ...over,
  };
}

beforeEach(() => {
  vi.mocked(api.listRuntimeProfiles).mockResolvedValue([]);
  vi.mocked(api.detectRuntimeProfiles).mockResolvedValue([]);
  vi.mocked(api.upsertRuntimeProfile).mockImplementation(async (_projectId, p) => p);
  vi.mocked(api.setupRuntimeProfile).mockResolvedValue(session({ state: "starting" }));
  vi.mocked(api.startRuntimeProfile).mockResolvedValue(session());
  vi.mocked(api.runCliTranscript).mockResolvedValue(
    session({ state: "starting", kind: "cli_transcript" }),
  );
  vi.mocked(api.stopRuntimeProfile).mockResolvedValue(true);
  vi.mocked(api.getRuntimeSession).mockResolvedValue(session());
  vi.mocked(api.getRuntimeSessionLogs).mockResolvedValue({ lines: [], truncated: false });
  vi.mocked(api.runRuntimeHealthCheck).mockResolvedValue({ ok: true, detail: "HTTP 200" });
  vi.mocked(api.runRuntimeTest).mockResolvedValue({ kind: "demo_smoke", passed: true, detail: "ok", raw: {} });
  vi.mocked(api.resolveRuntimeRun).mockResolvedValue({
    resolved: false,
    runnable: false,
    reason: "unresolved",
    plan: null,
    session: null,
    lookedFor: [],
    requiresReducedIsolationConsent: false,
  });
  shellMocks.open.mockResolvedValue(undefined);
  Object.defineProperty(window, "confirm", {
    configurable: true,
    value: vi.fn(() => true),
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RunPreviewPanel", () => {
  it("is collapsed by default with the Preview title", async () => {
    render(<RunPreviewPanel projectId="proj" />);
    const panel = screen.getByText("Preview").closest("details") as HTMLDetailsElement;
    expect(panel).not.toHaveAttribute("open");
    expect(await screen.findByRole("heading", { name: "No runnable demo detected" })).toBeInTheDocument();
  });

  it("renders no runnable demo as an honest empty state", async () => {
    render(<RunPreviewPanel projectId="proj" />);
    fireEvent.click(screen.getByText("Preview"));

    expect(await screen.findByRole("heading", { name: "No runnable demo detected" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Detect runtime" }));

    await waitFor(() => expect(api.detectRuntimeProfiles).toHaveBeenCalledWith("proj"));
    expect(screen.getByText("No runnable demo detected.")).toBeInTheDocument();
  });

  it("treats a missing runtime route as unavailable instead of a project error", async () => {
    vi.mocked(api.listRuntimeProfiles).mockRejectedValueOnce(
      new Error("list runtime profiles failed (404)"),
    );

    render(<RunPreviewPanel projectId="proj" />);

    expect(await screen.findByText("Runtime unavailable")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Runtime preview unavailable" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Detect runtime" })).toBeDisabled();
  });

  it("shows exact argv and requires confirmation before dependency setup", async () => {
    const confirm = vi.fn(() => false);
    Object.defineProperty(window, "confirm", { configurable: true, value: confirm });
    vi.mocked(api.listRuntimeProfiles).mockResolvedValue([profile()]);

    render(<RunPreviewPanel projectId="proj" />);
    fireEvent.click(screen.getByText("Preview"));

    expect(await screen.findByText("npm install")).toBeInTheDocument();
    expect(screen.getByText("npm run dev")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run setup" }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("npm install"));
    expect(api.setupRuntimeProfile).not.toHaveBeenCalled();

    confirm.mockReturnValueOnce(true);
    fireEvent.click(screen.getByRole("button", { name: "Run setup" }));

    await waitFor(() =>
      expect(api.setupRuntimeProfile).toHaveBeenCalledWith("proj", "default"),
    );
  });

  it("opens the demo in the external browser through Tauri shell-open", async () => {
    vi.mocked(api.listRuntimeProfiles).mockResolvedValue([
      profile({ setup: [], ports: [{ name: "web", containerPort: null, preferred: 4173 }] }),
    ]);

    render(<RunPreviewPanel projectId="proj" />);
    fireEvent.click(screen.getByText("Preview"));

    await screen.findByText("npm run dev");
    fireEvent.click(screen.getByRole("button", { name: "Open demo" }));

    await waitFor(() =>
      expect(shellMocks.open).toHaveBeenCalledWith("http://127.0.0.1:4173"),
    );
  });

  // F144: the live demo address in the runtime summary is itself clickable.
  describe("clickable demo address (F144)", () => {
    it("renders the live demo address as a browser-open button once a run is active", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([profile({ setup: [] })]);
      vi.mocked(api.startRuntimeProfile).mockResolvedValue(
        session({ state: "running", allocatedPorts: [57276] }),
      );

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByText("npm run dev");
      fireEvent.click(screen.getByRole("button", { name: "Start" }));

      const summary = await screen.findByLabelText("Runtime summary");
      const link = await within(summary).findByRole("button", {
        name: /Open demo in browser: http:\/\/127\.0\.0\.1:57276/,
      });
      expect(link).toHaveTextContent("http://127.0.0.1:57276");

      fireEvent.click(link);
      await waitFor(() =>
        expect(shellMocks.open).toHaveBeenCalledWith("http://127.0.0.1:57276"),
      );
    });

    it("prevents duplicate browser opens while the first open action is pending", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([profile({ setup: [] })]);
      vi.mocked(api.startRuntimeProfile).mockResolvedValue(
        session({ state: "running", allocatedPorts: [57276] }),
      );
      let finishOpen!: () => void;
      shellMocks.open.mockImplementationOnce(
        () =>
          new Promise<undefined>((resolve) => {
            finishOpen = () => resolve(undefined);
          }),
      );

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByText("npm run dev");
      fireEvent.click(screen.getByRole("button", { name: "Start" }));

      const link = await screen.findByRole("button", {
        name: /Open demo in browser: http:\/\/127\.0\.0\.1:57276/,
      });
      fireEvent.click(link);
      await waitFor(() => expect(link).toBeDisabled());

      fireEvent.click(link);
      expect(shellMocks.open).toHaveBeenCalledTimes(1);

      finishOpen();
      await waitFor(() => expect(link).not.toBeDisabled());
    });

    it("keeps the intended address inert (plain text) before a run", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([profile({ setup: [] })]);

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      const summary = await screen.findByLabelText("Runtime summary");
      // The intended address is shown from the preferred port…
      expect(within(summary).getByText("http://127.0.0.1:5173")).toBeInTheDocument();
      // …but it is not clickable until a session binds the port.
      expect(
        within(summary).queryByRole("button", { name: /Open demo in browser/ }),
      ).toBeNull();
    });

    it("keeps the preferred address inert when an active session has no allocated port", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([profile({ setup: [] })]);
      vi.mocked(api.startRuntimeProfile).mockResolvedValue(
        session({ state: "running", allocatedPorts: [] }),
      );

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByText("npm run dev");
      fireEvent.click(screen.getByRole("button", { name: "Start" }));

      const summary = await screen.findByLabelText("Runtime summary");
      expect(within(summary).getByText("http://127.0.0.1:5173")).toBeInTheDocument();
      expect(
        within(summary).queryByRole("button", { name: /Open demo in browser/ }),
      ).toBeNull();
    });

    it("shows 'none' with no button when the profile has no demo URL", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([
        profile({
          setup: [],
          kind: "cli",
          demo: { type: "command", url: null, path: null, timeoutSeconds: null },
          ports: [],
          health: { type: "none", url: null, timeoutSeconds: null },
        }),
      ]);

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      const summary = await screen.findByLabelText("Runtime summary");
      expect(within(summary).getByText("none")).toBeInTheDocument();
      expect(
        within(summary).queryByRole("button", { name: /Open demo in browser/ }),
      ).toBeNull();
    });
  });

  it("surfaces reduced-isolation sessions when the backend reports sandbox none", async () => {
    vi.mocked(api.listRuntimeProfiles).mockResolvedValue([profile({ setup: [] })]);
    vi.mocked(api.startRuntimeProfile).mockResolvedValue(
      session({ sandboxBackend: "none", state: "running" }),
    );
    vi.mocked(api.getRuntimeSessionLogs).mockResolvedValue({
      lines: ["started without sandbox"],
      truncated: false,
    });

    render(<RunPreviewPanel projectId="proj" />);
    fireEvent.click(screen.getByText("Preview"));

    await screen.findByText("npm run dev");
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect(await screen.findByText(/Reduced isolation/)).toBeInTheDocument();
    expect(await screen.findByText("started without sandbox")).toBeInTheDocument();
  });

  it("does not show healthy until a real health pass is present", async () => {
    vi.mocked(api.listRuntimeProfiles).mockResolvedValue([profile({ setup: [] })]);
    vi.mocked(api.startRuntimeProfile).mockResolvedValue(
      session({ state: "healthy", healthStatus: null }),
    );

    render(<RunPreviewPanel projectId="proj" />);
    fireEvent.click(screen.getByText("Preview"));

    await screen.findByText("npm run dev");
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect(await screen.findAllByText("Running")).not.toHaveLength(0);
    expect(screen.queryByText("Healthy")).toBeNull();
  });

  it("clears profile-scoped health and test evidence when switching profiles", async () => {
    vi.mocked(api.listRuntimeProfiles).mockResolvedValue([
      profile({ profileId: "web", setup: [] }),
      profile({ profileId: "api", setup: [], start: ["uvicorn", "app:app"] }),
    ]);

    render(<RunPreviewPanel projectId="proj" />);

    await screen.findByText("npm run dev");
    fireEvent.click(screen.getByRole("button", { name: "Check health" }));
    await screen.findByText(/passed HTTP 200/);
    fireEvent.click(screen.getByRole("button", { name: "Run tests" }));
    await screen.findByText(/Runtime test recorded: ok/);

    fireEvent.change(screen.getByLabelText("Runtime profile"), { target: { value: "api" } });

    const healthBlock = screen.getByLabelText("Health-check detail");
    expect(within(healthBlock).getByText("not checked")).toBeInTheDocument();
    expect(screen.queryByText(/passed HTTP 200/)).toBeNull();
    expect(screen.queryByText(/Runtime test recorded: ok/)).toBeNull();
  });

  // F101-01: a served static project is now a normal managed_local runtime.
  describe("served static profile (F101-01)", () => {
    function staticProfile(): RuntimeProfile {
      return profile({
        kind: "static",
        runtimeMode: "managed_local",
        setup: [],
        start: ["python", "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
        health: { type: "http", url: "http://127.0.0.1:{port}", timeoutSeconds: 20 },
        demo: { type: "url", url: "http://127.0.0.1:{port}", path: null, timeoutSeconds: null },
        ports: [{ name: "web", containerPort: null, preferred: 8000 }],
      });
    }

    it("renders Start enabled and is not gated as a static dead-panel", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([staticProfile()]);

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByText('python -m http.server "{port}" --bind 127.0.0.1');
      expect(screen.queryByText("Static preview available")).toBeNull();
      expect(screen.getByRole("button", { name: "Start" })).not.toBeDisabled();
      // A served static profile keeps the server/health flavor, not CLI.
      expect(screen.queryByRole("button", { name: "Run (CLI)" })).toBeNull();
      expect(screen.getByLabelText("Health-check detail")).toBeInTheDocument();
    });

    it("opens the served loopback URL via Tauri shell-open", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([staticProfile()]);

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByText('python -m http.server "{port}" --bind 127.0.0.1');
      fireEvent.click(screen.getByRole("button", { name: "Open demo" }));

      await waitFor(() =>
        expect(shellMocks.open).toHaveBeenCalledWith("http://127.0.0.1:8000"),
      );
    });
  });

  // F101-02: CLI/script profiles get a transcript flavor instead of Open demo.
  describe("CLI transcript profile (F101-02)", () => {
    function cliProfile(): RuntimeProfile {
      return profile({
        kind: "cli",
        runtimeMode: "managed_local",
        setup: [],
        start: ["python", "main.py"],
        health: { type: "none", url: null, timeoutSeconds: null },
        demo: { type: "command", url: null, path: null, timeoutSeconds: null },
        ports: [],
      });
    }

    it("renders Run (CLI), the args input, and the effective command — no Open demo", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([cliProfile()]);

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      expect(await screen.findByRole("button", { name: "Run (CLI)" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Open demo" })).toBeNull();
      expect(screen.getByLabelText("Extra arguments")).toBeInTheDocument();
      // Effective command preview reflects the typed args.
      fireEvent.change(screen.getByLabelText("Extra arguments"), {
        target: { value: "--name world" },
      });
      const command = screen.getByLabelText("Effective command");
      expect(within(command).getByText("python main.py --name world")).toBeInTheDocument();
    });

    it("runs the CLI with the typed args and shows the exit-0 pass badge", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([cliProfile()]);
      vi.mocked(api.runCliTranscript).mockResolvedValue(
        session({
          state: "stopped",
          kind: "cli_transcript",
          exitCode: 0,
          passed: true,
          allocatedPorts: [],
        }),
      );
      vi.mocked(api.getRuntimeSessionLogs).mockResolvedValue({
        lines: ["hello world"],
        truncated: false,
      });

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByRole("button", { name: "Run (CLI)" });
      fireEvent.change(screen.getByLabelText("Extra arguments"), {
        target: { value: "--name world" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Run (CLI)" }));

      await waitFor(() =>
        expect(api.runCliTranscript).toHaveBeenCalledWith("proj", "default", {
          extraArgs: "--name world",
        }),
      );
      expect(await screen.findByText("passed · exit 0")).toBeInTheDocument();
      const transcript = await screen.findByLabelText("CLI transcript output");
      expect(transcript).toHaveTextContent("hello world");
    });

    it("shows a fail badge with the exit code on a non-zero exit", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([cliProfile()]);
      vi.mocked(api.runCliTranscript).mockResolvedValue(
        session({ state: "stopped", kind: "cli_transcript", exitCode: 3, passed: false }),
      );

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByRole("button", { name: "Run (CLI)" });
      fireEvent.click(screen.getByRole("button", { name: "Run (CLI)" }));

      expect(await screen.findByText("failed · exit 3")).toBeInTheDocument();
    });

    it("renders a timed-out fail when the run was killed by the time-box", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([
        profile({
          kind: "cli",
          runtimeMode: "managed_local",
          setup: [],
          start: ["python", "main.py"],
          health: { type: "none", url: null, timeoutSeconds: null },
          demo: { type: "command", url: null, path: null, timeoutSeconds: 30 },
          ports: [],
        }),
      ]);
      vi.mocked(api.runCliTranscript).mockResolvedValue(
        session({
          state: "stopped",
          kind: "cli_transcript",
          exitCode: null,
          passed: null,
          error: "timed_out",
        }),
      );

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByRole("button", { name: "Run (CLI)" });
      fireEvent.click(screen.getByRole("button", { name: "Run (CLI)" }));

      expect(
        await screen.findByText("Timed out after 30s (process killed)"),
      ).toBeInTheDocument();
    });

    it("disables Run (CLI) while a run is active", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([cliProfile()]);
      vi.mocked(api.runCliTranscript).mockResolvedValue(
        session({ state: "running", kind: "cli_transcript" }),
      );

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByRole("button", { name: "Run (CLI)" });
      fireEvent.click(screen.getByRole("button", { name: "Run (CLI)" }));

      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Run (CLI)" })).toBeDisabled(),
      );
    });

    it("surfaces a 422 bad-args error next to the input", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([cliProfile()]);
      vi.mocked(api.runCliTranscript).mockRejectedValue(
        new api.RuntimeCliArgsError("invalid extra_args: No closing quotation"),
      );

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));

      await screen.findByRole("button", { name: "Run (CLI)" });
      fireEvent.change(screen.getByLabelText("Extra arguments"), {
        target: { value: '--name "world' },
      });
      fireEvent.click(screen.getByRole("button", { name: "Run (CLI)" }));

      expect(
        await screen.findByText("invalid extra_args: No closing quotation"),
      ).toBeInTheDocument();
    });
  });

  describe("universal Run (F101-03)", () => {
    const groundedPlan = {
      modality: "cli",
      launchKind: "cli",
      profileId: "default",
      kind: "cli",
      start: ["python", "main.py"],
      setup: [],
      workingDir: ".",
      ports: [],
      groundedBy: "detector",
      verifiedPaths: ["main.py"],
      trustTier: 0,
      host: "local sidecar",
      warnings: [],
    };

    it("previews a grounded plan without executing it", async () => {
      vi.mocked(api.resolveRuntimeRun).mockResolvedValue({
        resolved: true,
        runnable: true,
        reason: null,
        plan: groundedPlan,
        session: null,
        lookedFor: [],
    requiresReducedIsolationConsent: false,
      });

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));
      fireEvent.click(await screen.findByRole("button", { name: "Run" }));

      expect(await screen.findByText("Launch plan")).toBeInTheDocument();
      expect(screen.getByText("python main.py")).toBeInTheDocument();
      expect(screen.getByText(/detector \(main\.py\)/)).toBeInTheDocument();
      expect(api.resolveRuntimeRun).toHaveBeenCalledWith("proj", { confirm: false });
      // A preview must not execute anything.
      expect(api.startRuntimeProfile).not.toHaveBeenCalled();
      expect(api.runCliTranscript).not.toHaveBeenCalled();
    });

    it("executes only on confirm and dismisses the preview", async () => {
      vi.mocked(api.listRuntimeProfiles).mockResolvedValue([
        profile({ kind: "cli", start: ["python", "main.py"] }),
      ]);
      vi.mocked(api.resolveRuntimeRun).mockImplementation(async (_pid, opts) => ({
        resolved: true,
        runnable: true,
        reason: null,
        plan: groundedPlan,
        session: opts?.confirm ? session({ state: "starting", kind: "cli_transcript" }) : null,
        lookedFor: [],
    requiresReducedIsolationConsent: false,
      }));

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));
      fireEvent.click(await screen.findByRole("button", { name: "Run" }));
      fireEvent.click(await screen.findByRole("button", { name: "Confirm & run" }));

      await waitFor(() =>
        expect(api.resolveRuntimeRun).toHaveBeenCalledWith("proj", {
          confirm: true,
          confirmReducedIsolation: false,
        }),
      );
      await waitFor(() => expect(screen.queryByText("Launch plan")).toBeNull());
    });

    it("gates a T2 (reduced isolation) desktop run behind a second consent", async () => {
      const desktopPlan = { ...groundedPlan, modality: "desktop", launchKind: "desktop", kind: "desktop", trustTier: 2 };
      vi.mocked(api.resolveRuntimeRun).mockImplementation(async (_pid, opts) => ({
        resolved: true,
        runnable: opts?.confirmReducedIsolation === true,
        reason: opts?.confirmReducedIsolation ? null : "reduced_isolation_consent_required",
        plan: desktopPlan,
        session: opts?.confirmReducedIsolation ? session({ state: "starting" }) : null,
        lookedFor: [],
        requiresReducedIsolationConsent: true,
      }));

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));
      fireEvent.click(await screen.findByRole("button", { name: "Run" }));

      // Preview flags the reduced-isolation consent; there is no plain confirm.
      const consentBtn = await screen.findByRole("button", {
        name: "Run with reduced isolation",
      });
      expect(screen.queryByRole("button", { name: "Confirm & run" })).toBeNull();

      fireEvent.click(consentBtn);
      await waitFor(() =>
        expect(api.resolveRuntimeRun).toHaveBeenCalledWith("proj", {
          confirm: true,
          confirmReducedIsolation: true,
        }),
      );
    });

    it("shows the how-to-run-unknown checklist when nothing grounds", async () => {
      vi.mocked(api.resolveRuntimeRun).mockResolvedValue({
        resolved: false,
        runnable: false,
        reason: "unresolved",
        plan: null,
        session: null,
        lookedFor: ["app.py / main.py / __main__.py (Python)"],
        requiresReducedIsolationConsent: false,
      });

      render(<RunPreviewPanel projectId="proj" />);
      fireEvent.click(screen.getByText("Preview"));
      fireEvent.click(await screen.findByRole("button", { name: "Run" }));

      expect(
        await screen.findByText(/doesn.t know how to run this yet/),
      ).toBeInTheDocument();
      expect(
        screen.getByText("app.py / main.py / __main__.py (Python)"),
      ).toBeInTheDocument();
    });
  });
});
