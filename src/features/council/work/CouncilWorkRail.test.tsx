import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

vi.mock("../../../lib/api/council", () => ({
  listPendingDecisions: vi.fn(),
  approvePendingDecision: vi.fn(),
  rejectPendingDecision: vi.fn(),
  listChildRuns: vi.fn(),
  cancelRun: vi.fn(),
}));
vi.mock("../../../lib/api/tools", () => ({ getMcpHealth: vi.fn() }));
vi.mock("../../../lib/api/diagnostics", () => ({ getSidecarLifecycle: vi.fn() }));

import {
  approvePendingDecision,
  cancelRun,
  listChildRuns,
  listPendingDecisions,
  rejectPendingDecision,
} from "../../../lib/api/council";
import { getMcpHealth } from "../../../lib/api/tools";
import { getSidecarLifecycle } from "../../../lib/api/diagnostics";
import CouncilWorkRail from "./CouncilWorkRail";
import { roomUsesWorkRail } from "./roomUsesWorkRail";

const _pending = listPendingDecisions as unknown as ReturnType<typeof vi.fn>;
const _approve = approvePendingDecision as unknown as ReturnType<typeof vi.fn>;
const _reject = rejectPendingDecision as unknown as ReturnType<typeof vi.fn>;
const _children = listChildRuns as unknown as ReturnType<typeof vi.fn>;
const _cancel = cancelRun as unknown as ReturnType<typeof vi.fn>;
const _mcp = getMcpHealth as unknown as ReturnType<typeof vi.fn>;
const _sidecar = getSidecarLifecycle as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("roomUsesWorkRail", () => {
  it("is false for a plain room (no tools/children/escalation)", () => {
    expect(roomUsesWorkRail(null)).toBe(false);
    expect(roomUsesWorkRail({ name: "plain", members: [] })).toBe(false);
    expect(
      roomUsesWorkRail({ tool_policy: { web_fetch: { enabled: false } } }),
    ).toBe(false);
  });

  it("is true for a tool/child/escalation room", () => {
    expect(roomUsesWorkRail({ tool_policy: { code_read: { enabled: true } } })).toBe(true);
    expect(roomUsesWorkRail({ child_run_policy: { enabled: true } })).toBe(true);
    expect(roomUsesWorkRail({ escalation_policy: { enabled: true } })).toBe(true);
  });
});

function setupEmpty() {
  _pending.mockResolvedValue([]);
  _children.mockResolvedValue([]);
  _mcp.mockResolvedValue([]);
  _sidecar.mockResolvedValue({
    component: "sidecar", pid: 1, sidecar_version: "0.1", residency_mode: "local",
    config_signature: "cfg-x", signature_inputs: {},
  });
}

describe("CouncilWorkRail", () => {
  it("renders an ARIA tablist with all tabs and supports arrow-key nav", async () => {
    setupEmpty();
    render(<CouncilWorkRail runId="run-1" events={[]} />);
    const tablist = screen.getByRole("tablist");
    expect(tablist).toBeInTheDocument();
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(5);
    // Approvals selected by default.
    expect(screen.getByTestId("work-rail-tab-approvals")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    // ArrowRight moves selection to Tool results.
    fireEvent.keyDown(tablist, { key: "ArrowRight" });
    await waitFor(() =>
      expect(screen.getByTestId("work-rail-tab-tools")).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
  });

  it("approves a pending decision via the Approvals tab", async () => {
    _children.mockResolvedValue([]);
    _mcp.mockResolvedValue([]);
    _sidecar.mockResolvedValue({
      component: "sidecar", pid: 1, sidecar_version: "0.1",
      residency_mode: "local", config_signature: "cfg", signature_inputs: {},
    });
    _pending.mockResolvedValue([
      {
        decisionId: "pd-1", runId: "run-1", phase: "tool_call",
        state: "pending", reasonCode: "tool_consent_required",
        requester: { member_id: "m-1" }, safeRequest: { tool_id: "web_fetch" },
        riskClass: "internet", createdAt: "t",
        stateWritesOnApprove: [], appliedStateWrites: [], metadata: {},
      },
    ]);
    _approve.mockResolvedValue({});
    _reject.mockResolvedValue({});
    render(<CouncilWorkRail runId="run-1" events={[]} />);
    await waitFor(() => screen.getByTestId("approve-pd-1"));
    fireEvent.click(screen.getByTestId("reject-pd-1"));
    await waitFor(() => expect(_reject).toHaveBeenCalledWith("run-1", "pd-1"));
    fireEvent.click(screen.getByTestId("approve-pd-1"));
    await waitFor(() =>
      expect(_approve).toHaveBeenCalledWith("run-1", "pd-1"),
    );
  });

  it("shows a blocked tool call as blocked, not a success", () => {
    setupEmpty();
    render(
      <CouncilWorkRail
        runId="run-1"
        events={[
          {
            id: "e1", runId: "run-1", sequence: 1, type: "tool_call_blocked",
            status: "blocked", createdAt: "t",
            payload: { tool_id: "web_fetch", reason: "tool_not_granted" },
            raw: {},
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("work-rail-tab-tools"));
    expect(screen.getByText(/blocked: tool_not_granted/)).toBeInTheDocument();
    expect(screen.getByText(/untrusted data/i)).toBeInTheDocument();
  });

  it("cancels the run (cascading child runs) from the Child runs tab", async () => {
    _pending.mockResolvedValue([]);
    _mcp.mockResolvedValue([]);
    _sidecar.mockResolvedValue({
      component: "sidecar", pid: 1, sidecar_version: "0.1",
      residency_mode: "local", config_signature: "cfg", signature_inputs: {},
    });
    _children.mockResolvedValue([
      {
        parentRunId: "run-1", childRunId: "cr-1", memberId: "m-1",
        taskKind: "tester", status: "running", title: "Run tests",
        promptSha256: "x", workerKind: "scripted", createdAt: "t", updatedAt: "t",
        artifactRefs: [], metadata: {},
      },
    ]);
    _cancel.mockResolvedValue({ state: "cancelled" });
    render(<CouncilWorkRail runId="run-1" events={[]} />);
    fireEvent.click(screen.getByTestId("work-rail-tab-children"));
    await waitFor(() => screen.getByTestId("cancel-children"));
    fireEvent.click(screen.getByTestId("cancel-children"));
    await waitFor(() => expect(_cancel).toHaveBeenCalledWith("run-1"));
  });
});
