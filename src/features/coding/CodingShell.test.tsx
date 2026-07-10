import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createProject: vi.fn().mockResolvedValue({}),
  deleteProject: vi.fn().mockResolvedValue(undefined),
  putNorthStar: vi.fn().mockResolvedValue({}),
  startRun: vi.fn().mockResolvedValue(true),
  getRunSetup: vi.fn(),
  confirmRunSetup: vi.fn().mockResolvedValue(true),
  runSetupPreflight: vi.fn().mockResolvedValue([]),
  getCliLoginCommand: vi.fn().mockResolvedValue({ loginArgv: [], installUrl: "", installCommand: "" }),
  listRooms: vi.fn().mockResolvedValue([]),
  getRoomFull: vi.fn().mockResolvedValue({ room: {}, validation: { status: "draft", errors: [] } }),
  pickPaths: vi.fn().mockResolvedValue([]),
  RunPreflightBlocked: class RunPreflightBlocked extends Error {
    unhealthy: unknown[];
    constructor(message: string, unhealthy: unknown[]) {
      super(message);
      this.name = "RunPreflightBlocked";
      this.unhealthy = unhealthy;
    }
  },
}));

// F105: the native directory picker (Tauri dialog). The real `pickPaths` routes
// to @tauri-apps/plugin-dialog via a runtime-string dynamic import that bypasses
// vi.mock, so we mock the wrapper itself.
vi.mock("../shell/FilePickerDialog", () => ({
  pickPaths: mocks.pickPaths,
}));

vi.mock("../../lib/api/coding", () => ({
  listProjects: vi.fn().mockResolvedValue([
    {
      id: "todo-app",
      northStar: "Build a todo CLI",
      status: "active",
      listStatus: "active",
      listStatusReason: "lifecycle",
    },
  ]),
  listGroundingCorpora: vi.fn().mockResolvedValue([
    { name: "alpha", fileCount: 2, readyCount: 2 },
  ]),
  createProject: mocks.createProject,
  deleteProject: mocks.deleteProject,
  putNorthStar: mocks.putNorthStar,
  // F135: OnboardingPanel + ImportProjectForm calls (getNorthStarProposal runs on
  // mount of a selected project). Stubbed so the shell renders in isolation.
  getNorthStarProposal: vi.fn().mockResolvedValue(null),
  startOrientationScan: vi.fn().mockResolvedValue({ jobId: "j", status: "scanning" }),
  orientationScanStatus: vi.fn().mockResolvedValue({ jobId: "j", status: "done" }),
  acceptNorthStarProposal: vi.fn().mockResolvedValue({}),
  setWorkRequest: vi.fn().mockResolvedValue({}),
  // F137: CurrentFocusPanel (embedded in OnboardingPanel) loads focuses on mount.
  listFocuses: vi.fn().mockResolvedValue([]),
  importLocalProject: vi.fn().mockResolvedValue({}),
  importGithubAuthStatus: vi.fn().mockResolvedValue({ ghPresent: false, login: null }),
  importGithubClone: vi.fn().mockResolvedValue({ jobId: "j", status: "cloning" }),
  importGithubCloneStatus: vi.fn().mockResolvedValue({ jobId: "j", status: "done" }),
  // F141 WS-C: branch discovery in the import form.
  importGithubBranches: vi.fn().mockResolvedValue({ ok: false, branches: [], defaultBranch: null }),
  // F141 WS-J: CodingProjectView loads the PM chat thread on mount.
  getPmChat: vi.fn().mockResolvedValue([]),
  pmAsk: vi.fn().mockResolvedValue({ reply: { role: "pm", kind: "chat", message: "", at: "" }, threadId: "main", answered: true }),
  getProject: vi.fn().mockResolvedValue({
    id: "todo-app",
    northStar: "Build a todo CLI",
    definitionOfDone: "tests pass",
    target: "new",
    status: "active",
    revision: 1,
    // F121: default to confirmed so existing run/start tests reach startRun; the
    // dedicated gate test overrides this to false.
    runSetupConfirmed: true,
  }),
  getBacklog: vi.fn().mockResolvedValue([]),
  getDecisions: vi.fn().mockResolvedValue([]),
  getArtifacts: vi.fn().mockResolvedValue([]),
  getToolEvents: vi.fn().mockResolvedValue([]),
  getTestCommands: vi.fn().mockResolvedValue({}),
  getTestRuns: vi.fn().mockResolvedValue([]),
  getTestSettings: vi.fn().mockResolvedValue({ requireSandbox: false }),
  getTurns: vi.fn().mockResolvedValue([]),
  getPrs: vi.fn().mockResolvedValue([]),
  getGovernance: vi.fn().mockResolvedValue({
    state: {
      mode: "off",
      phase: "idle",
      humanCodeApproval: "final_only",
      activeArtifactIds: {},
      updatedAt: "",
    },
    artifacts: [],
    reviews: [],
    approvals: [],
    planSlices: [],
  }),
  getGovernanceStatus: vi.fn().mockResolvedValue({
    mode: "off",
    stage: "idle",
    status: null,
    headline: "",
    actorMemberId: null,
    actorLabel: null,
    reviewPass: null,
    steps: [],
    buildProgress: null,
  }),
  getGovernanceFull: vi.fn().mockResolvedValue({
    summary: {
      state: { mode: "off", phase: "idle", humanCodeApproval: "final_only", activeArtifactIds: {}, updatedAt: "" },
      artifacts: [], reviews: [], approvals: [], planSlices: [],
    },
    status: { mode: "off", stage: "idle", status: null, headline: "", actorMemberId: null, actorLabel: null, reviewPass: null, steps: [], buildProgress: null },
  }),
  getGovernanceArtifact: vi.fn().mockResolvedValue({
    artifactId: "a-brainstorm-1",
    artifactKind: "brainstorm",
    version: 1,
    state: "under_review",
    title: "Brainstorm",
    bodyMarkdown: "# Brainstorm",
    sourceRefs: [],
    supersedesArtifactId: null,
    createdAt: "",
  }),
  acceptGovernanceArtifact: vi.fn(),
  startRun: mocks.startRun,
  RunPreflightBlocked: mocks.RunPreflightBlocked,
  RunSetupRequired: class RunSetupRequired extends Error {
    constructor(message: string) {
      super(message);
      this.name = "RunSetupRequired";
    }
  },
  getRunSetup: mocks.getRunSetup,
  confirmRunSetup: mocks.confirmRunSetup,
  runSetupPreflight: mocks.runSetupPreflight,
  interject: vi.fn().mockResolvedValue({ message: "", at: "", pmReply: null }),
  resumeRun: vi.fn().mockResolvedValue(true),
  getGuardrail: vi.fn().mockResolvedValue(true),
  getAutonomy: vi.fn().mockResolvedValue({
    maxIterations: 200,
    maxModelCalls: null,
    checkpointCadence: "per_milestone",
    checkpointN: 5,
  }),
  getRunStatus: vi.fn().mockResolvedValue({
    running: false,
    result: null,
    state: { status: "idle" },
    recoverable: false,
    canResume: false,
  }),
  listRuntimeProfiles: vi.fn().mockResolvedValue([]),
  detectRuntimeProfiles: vi.fn().mockResolvedValue([]),
  upsertRuntimeProfile: vi.fn(),
  setupRuntimeProfile: vi.fn(),
  startRuntimeProfile: vi.fn(),
  stopRuntimeProfile: vi.fn(),
  getRuntimeSession: vi.fn(),
  getRuntimeSessionLogs: vi.fn().mockResolvedValue({ lines: [], truncated: false }),
  runRuntimeHealthCheck: vi.fn(),
  runRuntimeTest: vi.fn(),
  runtimeProfileFrom: (raw: Record<string, unknown>) => raw,
  runtimeProfileToWire: (profile: Record<string, unknown>) => profile,
  // F093: pure helper used by CodingProjectView's summary panel.
  runStopReason: (s: { result?: Record<string, unknown> | null } | null | undefined) =>
    (s?.result?.["stop_reason"] as string | null) ?? null,
  // F121: pure run-state accessors used by the run-control phase derivation.
  runStateStatus: (s: { state?: Record<string, unknown> } | null | undefined) =>
    (typeof s?.state?.["status"] === "string" ? (s.state["status"] as string) : null),
  runCancelRequested: (s: { state?: Record<string, unknown> } | null | undefined) =>
    Boolean(s?.state?.["cancel_requested"]),
}));

vi.mock("../../lib/api/council", () => ({
  listRooms: mocks.listRooms,
}));

vi.mock("../../lib/api/councilRoom", () => ({
  getRoomFull: mocks.getRoomFull,
}));

vi.mock("../../lib/api/providerKeys", () => ({
  getCliLoginCommand: mocks.getCliLoginCommand,
}));

import CodingShell from "./index";
import { getProject, listProjects, putNorthStar } from "../../lib/api/coding";
import { SidecarUnreachableError } from "../../lib/api";

beforeEach(() => {
  vi.mocked(listProjects).mockReset();
  vi.mocked(getProject).mockReset();
  vi.mocked(listProjects).mockResolvedValue([
    {
      id: "todo-app",
      northStar: "Build a todo CLI",
      status: "active",
      listStatus: "active",
      listStatusReason: "lifecycle",
    },
  ]);
  vi.mocked(getProject).mockResolvedValue({
    id: "todo-app",
    northStar: "Build a todo CLI",
    definitionOfDone: "tests pass",
    target: "new",
    status: "active",
    revision: 1,
    runSetupConfirmed: true,
  });
  mocks.createProject.mockResolvedValue({});
  mocks.deleteProject.mockResolvedValue(undefined);
  mocks.startRun.mockResolvedValue(true);
  mocks.listRooms.mockResolvedValue([]);
  mocks.getRoomFull.mockResolvedValue({ room: {}, validation: { status: "draft", errors: [] } });
  mocks.pickPaths.mockResolvedValue([]);
  mocks.getRunSetup.mockResolvedValue({
    runSetupConfirmed: false,
    governance: { mode: "light" },
    autonomy: { checkpoint_cadence: "per_milestone", max_iterations: 50 },
    guardrailEnabled: true,
    memberHealthPreflight: true,
    defaults: {},
  });
  mocks.confirmRunSetup.mockResolvedValue(true);
  mocks.runSetupPreflight.mockResolvedValue([]);
  mocks.getCliLoginCommand.mockResolvedValue({ loginArgv: [], installUrl: "", installCommand: "" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CodingShell", () => {
  it("does not show the scary banner for a transient sidecar transport blip", async () => {
    // the cold-start / respawn case that used to latch "sidecar_unreachable" forever
    vi.mocked(listProjects).mockRejectedValueOnce(new SidecarUnreachableError());
    render(<CodingShell />);
    expect(screen.getByText("Coding Team")).toBeInTheDocument();
    // give the rejected mount fetch a tick; no alert banner should ever appear
    await waitFor(() => expect(vi.mocked(listProjects)).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders the create form and lists projects", async () => {
    render(<CodingShell />);
    expect(screen.getByText("Coding Team")).toBeInTheDocument();
    expect(screen.getByText("An autonomous coding team: give them a North Star, they will build.")).toBeInTheDocument();
    expect(screen.getByLabelText("Project id")).toBeInTheDocument();
    expect(screen.getByLabelText("Project corpus mode")).toBeInTheDocument();
    // the list shows just the project id (the North Star lives inside the project)
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Open project todo-app" })).toBeInTheDocument());
    expect(screen.queryByText(/Build a todo CLI/)).toBeNull();
  });

  it("blocks an invalid project id with an inline error and a disabled button", async () => {
    render(<CodingShell />);
    const idInput = screen.getByLabelText("Project id");
    const createBtn = screen.getByRole("button", { name: "Create project" });

    // A space is not allowed by the backend pattern — warn before submit.
    fireEvent.change(idInput, { target: { value: "my project" } });
    const err = await screen.findByRole("alert");
    expect(err.textContent).toMatch(/spaces/i);
    expect(idInput).toHaveAttribute("aria-invalid", "true");
    expect(createBtn).toBeDisabled();

    // Clicking the disabled button must not call the API.
    fireEvent.click(createBtn);
    expect(mocks.createProject).not.toHaveBeenCalled();

    // Fixing the id clears the error and enables the button.
    fireEvent.change(idInput, { target: { value: "my-project" } });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(idInput).toHaveAttribute("aria-invalid", "false");
    expect(createBtn).not.toBeDisabled();
  });

  it("renders derived all-projects statuses", async () => {
    vi.mocked(listProjects).mockResolvedValue([
      {
        id: "runner",
        northStar: "ship",
        status: "active",
        listStatus: "running",
        listStatusReason: "live_run",
      },
      {
        id: "blocked",
        northStar: "fix",
        status: "active",
        listStatus: "needs attention",
        listStatusReason: "blocking_attention",
      },
    ]);

    render(<CodingShell />);

    const runner = await screen.findByRole("button", { name: "Open project runner" });
    const blocked = await screen.findByRole("button", { name: "Open project blocked" });
    expect(within(runner).getByText("running")).toBeInTheDocument();
    expect(within(blocked).getByText("needs attention")).toBeInTheDocument();
    expect(within(blocked).queryByText("active")).toBeNull();
  });

  it("submits selected existing corpus in the create payload", async () => {
    render(<CodingShell />);
    fireEvent.change(screen.getByLabelText("Project id"), { target: { value: "p1" } });
    fireEvent.change(screen.getByLabelText("North Star"), { target: { value: "Build app" } });
    fireEvent.change(screen.getByLabelText("Project corpus mode"), { target: { value: "existing" } });
    await waitFor(() => expect(screen.getByLabelText("Existing corpus")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Existing corpus"), { target: { value: "alpha" } });
    fireEvent.click(screen.getByText("Create project"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalled());
    expect(mocks.createProject.mock.calls[0][0].grounding).toEqual({
      mode: "existing",
      corpusId: "alpha",
    });
  });

  it("deletes a project after confirmation and refreshes the list", async () => {
    const confirm = vi.fn(() => true);
    Object.defineProperty(window, "confirm", {
      configurable: true,
      value: confirm,
    });
    render(<CodingShell />);
    await screen.findByRole("button", { name: "Open project todo-app" });

    fireEvent.click(screen.getByLabelText("Delete project todo-app"));

    await waitFor(() => expect(mocks.deleteProject).toHaveBeenCalledWith("todo-app"));
    expect(vi.mocked(listProjects)).toHaveBeenCalledTimes(2);
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("todo-app"));
  });

  it("shows the assigned team room members with coding roles and models", async () => {
    mocks.listRooms.mockResolvedValue([
      { id: "coding-council", name: "Coding Council", updatedAt: "", revision: 1, statusHint: "ready" },
    ]);
    mocks.getRoomFull.mockResolvedValue({
      room: {
        members: [
          {
            id: "m-pm",
            name: "Avery",
            enabled: true,
            model_display: "claude-opus-4.8",
            metadata: { coding_role: "pm" },
          },
          {
            id: "m-dev",
            name: "Blake",
            enabled: true,
            gateway_route_id: "local.ollama.qwen3:14b",
            metadata: { coding_role: "dev" },
          },
          {
            id: "m-rev",
            name: "Casey",
            enabled: true,
            model_display: "gpt-4.1",
            metadata: { coding_role: "reviewer" },
          },
        ],
      },
      validation: { status: "ready", errors: [] },
    });

    render(<CodingShell />);
    // click the project-id button to open the project (North Star is in the header dialog)
    fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
    expect(await screen.findByRole("heading", { name: "todo-app" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "North Star" })).toBeInTheDocument();

    const teamPanel = screen.getByLabelText("Team") as HTMLDetailsElement;
    expect(teamPanel).not.toHaveAttribute("open");
    fireEvent.click(within(teamPanel).getByText("Team"));
    expect(await screen.findByLabelText("Team:")).toHaveValue("coding-council");
    expect(await screen.findByText("PM")).toBeInTheDocument();
    expect(screen.getByText("Avery")).toBeInTheDocument();
    expect(screen.getByText("claude-opus-4.8")).toBeInTheDocument();
    expect(screen.getByText("DEV")).toBeInTheDocument();
    expect(screen.getByText("Blake")).toBeInTheDocument();
    expect(screen.getByText("local.ollama.qwen3:14b")).toBeInTheDocument();
    expect(screen.getByText("REV")).toBeInTheDocument();
    expect(screen.getByText("Casey")).toBeInTheDocument();
    expect(screen.queryByText(/Assign each member a coding role/)).toBeNull();
  });

  it("opens and saves the North Star from the project header dialog", async () => {
    render(<CodingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
    const northStarButton = await screen.findByRole("button", { name: "North Star" });

    fireEvent.click(northStarButton);
    const dialog = await screen.findByRole("dialog", { name: "North Star for todo-app" });
    const editor = within(dialog).getByLabelText("North Star text");
    expect(editor).toHaveValue("Build a todo CLI");
    expect(within(dialog).getByRole("button", { name: "Save North Star" })).toBeDisabled();

    fireEvent.change(editor, { target: { value: "Build a better todo CLI" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save North Star" }));

    await waitFor(() =>
      expect(vi.mocked(putNorthStar)).toHaveBeenCalledWith(
        "todo-app",
        "Build a better todo CLI",
      ),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "North Star for todo-app" })).toBeNull(),
    );
    await waitFor(() => expect(northStarButton).toHaveFocus());
  });

  it("closes the North Star dialog with Escape and restores focus", async () => {
    render(<CodingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
    const northStarButton = await screen.findByRole("button", { name: "North Star" });

    fireEvent.click(northStarButton);
    const dialog = await screen.findByRole("dialog", { name: "North Star for todo-app" });
    fireEvent.keyDown(dialog, { key: "Escape" });

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "North Star for todo-app" })).toBeNull(),
    );
    await waitFor(() => expect(northStarButton).toHaveFocus());
    expect(vi.mocked(putNorthStar)).not.toHaveBeenCalled();
  });

  it("shows a blocked-start banner when member-health preflight refuses the run", async () => {
    mocks.listRooms.mockResolvedValue([
      { id: "coding-council", name: "Coding Council", updatedAt: "", revision: 1, statusHint: "ready" },
    ]);
    mocks.getRoomFull.mockResolvedValue({
      room: {
        members: [
          {
            id: "m-pm",
            name: "Avery",
            enabled: true,
            gateway_route_id: "claude_cli.opus",
            metadata: { coding_role: "pm" },
          },
          {
            id: "m-dev",
            name: "Blake",
            enabled: true,
            gateway_route_id: "claude_cli.opus",
            metadata: { coding_role: "dev" },
          },
        ],
      },
      validation: { status: "ready", errors: [] },
    });
    mocks.startRun.mockRejectedValueOnce(
      new mocks.RunPreflightBlocked("Can't start: providers are not ready.", [
        {
          provider: "claude_cli",
          route: "claude_cli.opus",
          reason: "auth_failed",
          detail: "not logged in",
          remediation: "Run the login command, then start again.",
          memberIds: ["m-pm", "m-dev"],
        },
      ]),
    );
    const navSpy = vi.fn();
    window.addEventListener("errorta:navigate", navSpy);
    try {
      render(<CodingShell />);
      fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
      await screen.findByText("Avery");

      fireEvent.click(screen.getByRole("button", { name: "Start run" }));

      const banner = await screen.findByRole("alert", {
        name: "Can't start: providers not ready",
      });
      expect(banner).toHaveTextContent(/claude_cli\.opus/);
      expect(banner).toHaveTextContent(/used by m-pm, m-dev/);
      expect(banner).toHaveTextContent(/Run the login command/);
      expect(mocks.startRun).toHaveBeenCalledWith(
        "todo-app",
        undefined,
        "coding-council",
      );

      fireEvent.click(within(banner).getByRole("button", { name: "Open provider settings" }));
      expect(navSpy).toHaveBeenCalledWith(
        expect.objectContaining({ detail: { view: "settings" } }),
      );

      fireEvent.click(within(banner).getByRole("button", { name: "Dismiss" }));
      expect(
        screen.queryByRole("alert", { name: "Can't start: providers not ready" }),
      ).toBeNull();
    } finally {
      window.removeEventListener("errorta:navigate", navSpy);
    }
  });

  // F121 Part B — the first Start on an unconfirmed project opens the readiness
  // gate instead of starting (no run thread).
  it("opens the Run setup gate instead of starting when setup is unconfirmed", async () => {
    vi.mocked(getProject).mockResolvedValue({
      id: "todo-app",
      northStar: "Build a todo CLI",
      definitionOfDone: "tests pass",
      target: "new",
      status: "active",
      revision: 1,
      runSetupConfirmed: false,
    });
    mocks.listRooms.mockResolvedValue([
      { id: "coding-council", name: "Coding Council", updatedAt: "", revision: 1, statusHint: "ready" },
    ]);
    mocks.getRoomFull.mockResolvedValue({
      room: { members: [{ id: "m-pm", name: "Avery", enabled: true, gateway_route_id: "fake.local.deterministic", metadata: { coding_role: "pm" } }] },
      validation: { status: "ready", errors: [] },
    });

    render(<CodingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
    await screen.findByText("Avery");

    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    // The gate opens (seeded via getRunSetup); no run was started.
    await screen.findByRole("dialog", { name: "Run setup" });
    expect(mocks.getRunSetup).toHaveBeenCalledWith("todo-app");
    expect(mocks.startRun).not.toHaveBeenCalled();
  });

  it("opens the Run setup gate before requiring a selected team", async () => {
    vi.mocked(getProject).mockResolvedValue({
      id: "todo-app",
      northStar: "Build a todo CLI",
      definitionOfDone: "tests pass",
      target: "new",
      status: "active",
      revision: 1,
      runSetupConfirmed: false,
    });
    mocks.listRooms.mockResolvedValue([]);

    render(<CodingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
    fireEvent.click(await screen.findByRole("button", { name: "Start run" }));

    await screen.findByRole("dialog", { name: "Run setup" });
    expect(screen.queryByText(/Pick a Council room as the team first/i)).toBeNull();
    expect(mocks.startRun).not.toHaveBeenCalled();
  });

  it("closes Run setup after confirm without auto-starting", async () => {
    vi.mocked(getProject).mockResolvedValue({
      id: "todo-app",
      northStar: "Build a todo CLI",
      definitionOfDone: "tests pass",
      target: "new",
      status: "active",
      revision: 1,
      runSetupConfirmed: false,
    });
    mocks.listRooms.mockResolvedValue([
      { id: "coding-council", name: "Coding Council", updatedAt: "", revision: 1, statusHint: "ready" },
    ]);
    mocks.getRoomFull.mockResolvedValue({
      room: { members: [{ id: "m-pm", name: "Avery", enabled: true, gateway_route_id: "fake.local.deterministic", metadata: { coding_role: "pm" } }] },
      validation: { status: "ready", errors: [] },
    });
    mocks.runSetupPreflight.mockResolvedValue([]);

    render(<CodingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
    await screen.findByText("Avery");
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await screen.findByRole("dialog", { name: "Run setup" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Ready to run/i })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Ready to run/i }));

    await waitFor(() => expect(mocks.confirmRunSetup).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Run setup" })).toBeNull());
    expect(mocks.startRun).not.toHaveBeenCalled();
  });

  it("opens Run setup by default after project creation", async () => {
    vi.mocked(listProjects)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          id: "new-app",
          northStar: "Build a new app",
          status: "active",
          listStatus: "active",
          listStatusReason: "lifecycle",
        },
      ]);
    vi.mocked(getProject).mockResolvedValue({
      id: "new-app",
      northStar: "Build a new app",
      definitionOfDone: "tests pass",
      target: "new",
      status: "active",
      revision: 1,
      runSetupConfirmed: false,
    });

    render(<CodingShell />);
    fireEvent.change(screen.getByLabelText("Project id"), { target: { value: "new-app" } });
    fireEvent.change(screen.getByLabelText("North Star"), {
      target: { value: "Build a new app" },
    });
    fireEvent.click(screen.getByText("Create project"));

    await screen.findByRole("dialog", { name: "Run setup" });
    expect(mocks.getRunSetup).toHaveBeenCalledWith("new-app");
    expect(mocks.startRun).not.toHaveBeenCalled();
  });

  it("keeps Run setup out of run controls and exposes it at the bottom", async () => {
    render(<CodingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
    await screen.findByRole("heading", { name: "todo-app" });

    const runControls = screen.getByLabelText("Run controls");
    expect(within(runControls).queryByRole("button", { name: "Run setup" })).toBeNull();
    expect(within(runControls).queryByRole("button", { name: "Set up run" })).toBeNull();

    const settings = screen.getByLabelText("Project settings");
    fireEvent.click(within(settings).getByRole("button", { name: "Run setup" }));

    await screen.findByRole("dialog", { name: "Run setup" });
    expect(mocks.getRunSetup).toHaveBeenCalledWith("todo-app");
  });

  it("renders the delivery location under project settings", async () => {
    vi.mocked(getProject).mockResolvedValue({
      id: "todo-app",
      northStar: "Build a todo CLI",
      definitionOfDone: "tests pass",
      target: "new",
      status: "active",
      revision: 1,
      runSetupConfirmed: true,
      plannedDeliveryDir: "/Users/example/Errorta Projects/ARK-Login-Sentinel",
    });

    render(<CodingShell />);
    fireEvent.click(await screen.findByRole("button", { name: "Open project todo-app" }));
    await screen.findByRole("heading", { name: "todo-app" });

    const settings = screen.getByLabelText("Project settings");
    expect(settings).toHaveTextContent(
      "Delivery: /Users/example/Errorta Projects/ARK-Login-Sentinel",
    );
    expect(within(screen.getByLabelText("Run controls")).queryByText(/Delivery:/)).toBeNull();
  });

  // F105 — Slice B: project location picker
  it("submits deliveryRoot for a new project", async () => {
    render(<CodingShell />);
    fireEvent.change(screen.getByLabelText("Project id"), { target: { value: "p-loc" } });
    fireEvent.change(screen.getByLabelText("Project location"), {
      target: { value: "/Users/example/Projects" },
    });
    fireEvent.click(screen.getByText("Create project"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalled());
    expect(mocks.createProject.mock.calls[0][0].deliveryRoot).toBe("/Users/example/Projects");
    expect(mocks.createProject.mock.calls[0][0].target).toBe("new");
  });

  it("submits deliveryRoot: null when the project location is blank", async () => {
    render(<CodingShell />);
    fireEvent.change(screen.getByLabelText("Project id"), { target: { value: "p-blank" } });
    fireEvent.click(screen.getByText("Create project"));

    await waitFor(() => expect(mocks.createProject).toHaveBeenCalled());
    expect(mocks.createProject.mock.calls[0][0].deliveryRoot).toBeNull();
  });

  it("Browse fills the project location with a POSIX path from the picker", async () => {
    mocks.pickPaths.mockResolvedValue(["/Users/example/Projects"]);
    render(<CodingShell />);
    fireEvent.click(screen.getByLabelText("Browse for project location"));
    await waitFor(() =>
      expect(screen.getByLabelText("Project location")).toHaveValue("/Users/example/Projects"),
    );
    expect(mocks.pickPaths).toHaveBeenCalledWith(
      expect.objectContaining({ directory: true, requireAbsolutePath: true }),
    );
  });

  it("Browse fills the project location with a Windows path from the picker", async () => {
    mocks.pickPaths.mockResolvedValue(["C:\\Users\\example\\Projects"]);
    render(<CodingShell />);
    fireEvent.click(screen.getByLabelText("Browse for project location"));
    await waitFor(() =>
      expect(screen.getByLabelText("Project location")).toHaveValue("C:\\Users\\example\\Projects"),
    );
  });

  it("Browse for an existing repo fills repoPath", async () => {
    mocks.pickPaths.mockResolvedValue(["/Users/example/code/myrepo"]);
    render(<CodingShell />);
    fireEvent.change(screen.getByLabelText("Target"), { target: { value: "existing" } });
    fireEvent.click(screen.getByLabelText("Browse for repo path"));
    await waitFor(() =>
      expect(screen.getByLabelText("Repo path")).toHaveValue("/Users/example/code/myrepo"),
    );
  });

  it("does not inject a fake name when the browser fallback returns nothing", async () => {
    // The browser <input type=file> fallback yields [] for directory picks, so
    // the field stays empty and an inline note appears — never a bare file name.
    mocks.pickPaths.mockResolvedValue([]);
    render(<CodingShell />);
    fireEvent.click(screen.getByLabelText("Browse for project location"));
    await waitFor(() => expect(mocks.pickPaths).toHaveBeenCalled());
    expect(screen.getByLabelText("Project location")).toHaveValue("");
    expect(
      screen.getByText(/paste an absolute path/i),
    ).toBeInTheDocument();
  });

  // F140 — the create/import forms live in collapsible panels: collapsed by
  // default once projects exist, open on the empty first-run state.
  it("collapses the create + import panels by default when projects exist", async () => {
    render(<CodingShell />);
    // wait for the settled load (default mock returns one project)
    await screen.findByRole("button", { name: "Open project todo-app" });
    const createPanel = screen.getByText("Create a project").closest("details")!;
    const importPanel = screen
      .getByText("Import a project")
      .closest("details")!;
    await waitFor(() => expect(createPanel).not.toHaveAttribute("open"));
    expect(importPanel).not.toHaveAttribute("open");
  });

  it("opens the create + import panels by default when there are no projects", async () => {
    vi.mocked(listProjects).mockResolvedValue([]);
    render(<CodingShell />);
    await screen.findByText("No coding projects yet.");
    const createPanel = screen.getByText("Create a project").closest("details")!;
    const importPanel = screen
      .getByText("Import a project")
      .closest("details")!;
    await waitFor(() => expect(createPanel).toHaveAttribute("open"));
    expect(importPanel).toHaveAttribute("open");
  });

  it("expands a collapsed panel when its summary is clicked", async () => {
    render(<CodingShell />);
    await screen.findByRole("button", { name: "Open project todo-app" });
    const createPanel = screen
      .getByText("Create a project")
      .closest("details") as HTMLDetailsElement;
    await waitFor(() => expect(createPanel).not.toHaveAttribute("open"));

    fireEvent.click(screen.getByText("Create a project"));

    await waitFor(() => expect(createPanel).toHaveAttribute("open"));
  });

  it("keeps a user-opened panel open across a background list refresh", async () => {
    const confirm = vi.fn(() => true);
    Object.defineProperty(window, "confirm", { configurable: true, value: confirm });
    render(<CodingShell />);
    await screen.findByRole("button", { name: "Open project todo-app" });
    const createPanel = screen
      .getByText("Create a project")
      .closest("details") as HTMLDetailsElement;
    await waitFor(() => expect(createPanel).not.toHaveAttribute("open"));

    // user opens the create panel
    fireEvent.click(screen.getByText("Create a project"));
    await waitFor(() => expect(createPanel).toHaveAttribute("open"));

    // a background refresh (delete → refreshList) must not stomp the user's toggle
    fireEvent.click(screen.getByLabelText("Delete project todo-app"));
    await waitFor(() => expect(mocks.deleteProject).toHaveBeenCalledWith("todo-app"));
    await waitFor(() => expect(vi.mocked(listProjects)).toHaveBeenCalledTimes(2));
    expect(createPanel).toHaveAttribute("open");
  });
});
