// F087-14 WS-6 — merge-back modal is an aria-modal dialog that closes on Escape.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/council", () => ({ listRooms: vi.fn().mockResolvedValue([]) }));

vi.mock("../../lib/api/coding", () => ({
  listProjects: vi.fn().mockResolvedValue([
    {
      id: "p1",
      northStar: "n",
      status: "active",
      listStatus: "active",
      listStatusReason: "lifecycle",
    },
  ]),
  listGroundingCorpora: vi.fn().mockResolvedValue([]),
  createProject: vi.fn().mockResolvedValue({}),
  deleteProject: vi.fn().mockResolvedValue(undefined),
  getProject: vi.fn().mockResolvedValue({
    id: "p1", northStar: "n", definitionOfDone: "d", target: "new", status: "active", revision: 1,
  }),
  // F135: OnboardingPanel mounts inside the project view; stub its calls.
  getNorthStarProposal: vi.fn().mockResolvedValue(null),
  startOrientationScan: vi.fn().mockResolvedValue({ jobId: "j", status: "scanning" }),
  orientationScanStatus: vi.fn().mockResolvedValue({ jobId: "j", status: "done" }),
  acceptNorthStarProposal: vi.fn().mockResolvedValue({}),
  setWorkRequest: vi.fn().mockResolvedValue({}),
  listFocuses: vi.fn().mockResolvedValue([]),
  importLocalProject: vi.fn().mockResolvedValue({}),
  importGithubAuthStatus: vi.fn().mockResolvedValue({ ghPresent: false, login: null }),
  importGithubClone: vi.fn().mockResolvedValue({ jobId: "j", status: "cloning" }),
  importGithubCloneStatus: vi.fn().mockResolvedValue({ jobId: "j", status: "done" }),
  importGithubBranches: vi.fn().mockResolvedValue({ ok: false, branches: [], defaultBranch: null }),
  getPmChat: vi.fn().mockResolvedValue([]),
  pmAsk: vi.fn().mockResolvedValue({ reply: { role: "pm", kind: "chat", message: "", at: "" }, threadId: "main", answered: true }),
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
  getGuardrail: vi.fn().mockResolvedValue(true),
  getAutonomy: vi.fn().mockResolvedValue({
    maxIterations: 200, maxModelCalls: null, checkpointCadence: "per_milestone", checkpointN: 5,
  }),
  getRunStatus: vi.fn().mockResolvedValue({ running: false, result: null, recoverable: false, canResume: false }),
  runStopReason: (s: { result?: Record<string, unknown> | null } | null | undefined) =>
    (s?.result?.["stop_reason"] as string | null) ?? null,
  runStateStatus: (s: { state?: Record<string, unknown> } | null | undefined) =>
    (typeof s?.state?.["status"] === "string" ? (s.state["status"] as string) : null),
  runCancelRequested: (s: { state?: Record<string, unknown> } | null | undefined) =>
    Boolean(s?.state?.["cancel_requested"]),
  getWorktreePreview: vi.fn().mockResolvedValue({
    diff: "diff --git a/x b/x",
    conflicts: [],
    fileDiffs: [{ path: "x.py", oldPath: null, changeType: "added", addedLines: 1, removedLines: 0 }],
    gate: { allowed: false, allowOverride: true, blockers: [{ code: "open_tasks", detail: "1" }] },
  }),
}));

import CodingShell from "./index";

afterEach(() => cleanup());

async function openModal() {
  render(<CodingShell />);
  fireEvent.click(await screen.findByRole("button", { name: "Open project p1" }));
  fireEvent.click(await screen.findByRole("button", { name: /Review diff/ }));
  return screen.findByRole("dialog", { name: "Review diff" });
}

describe("merge-back modal a11y", () => {
  it("is an aria-modal dialog showing the gate blockers and per-file diff", async () => {
    const dialog = await openModal();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText("open_tasks")).toBeInTheDocument();
    expect(screen.getByText("x.py")).toBeInTheDocument();
    // blocked gate -> override button, not a plain accept
    expect(screen.getByRole("button", { name: /Override & merge anyway/ })).toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    const dialog = await openModal();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Review diff" })).not.toBeInTheDocument(),
    );
  });

  // F146 Slice A: the individual-vs-combined-diff explainer is scoped to
  // head-binding blockers (unreviewed_changes / tests_missing), NOT to
  // structural blockers like open_tasks.
  it("does NOT show the delivery explainer for an open_tasks-only gate", async () => {
    await openModal();
    expect(
      screen.queryByText(/re-reviewed as a whole/i),
    ).not.toBeInTheDocument();
  });
});

describe("merge-back modal — delivery-review explainer (F146 Slice A)", () => {
  it("explains the individual-vs-combined diff for a head-binding blocker", async () => {
    const coding = await import("../../lib/api/coding");
    vi.mocked(coding.getWorktreePreview).mockResolvedValueOnce({
      diff: "diff --git a/x b/x",
      conflicts: [],
      fileDiffs: [{ path: "x.py", oldPath: null, changeType: "added", addedLines: 1, removedLines: 0 }],
      gate: {
        allowed: false,
        allowOverride: true,
        blockers: [
          { code: "unreviewed_changes", detail: "no reviewer verdict yet" },
          { code: "tests_missing", detail: "no test verdict yet" },
        ],
      },
      grounding: null,
    });
    await openModal();
    expect(screen.getByText(/re-reviewed as a whole/i)).toBeInTheDocument();
    expect(screen.getByText("unreviewed_changes")).toBeInTheDocument();
    expect(screen.getByText("tests_missing")).toBeInTheDocument();
  });
});
