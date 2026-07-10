// F120 — frontend: member-health ProblemCard (reason → remediation + actions)
// and the pre-run preflight blocked-start banner.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AttentionFeed, {
  PreflightBlockedBanner,
  type PreflightUnhealthy,
} from "./AttentionFeed";
import type { AttentionSignal } from "../../lib/api/coding";

vi.mock("../../lib/api/coding", () => ({
  getAttention: vi.fn(),
  resolveSignal: vi.fn(),
}));

// eslint-disable-next-line @typescript-eslint/no-var-requires
import * as api from "../../lib/api/coding";
const mockGet = api.getAttention as unknown as ReturnType<typeof vi.fn>;
const mockResolve = api.resolveSignal as unknown as ReturnType<typeof vi.fn>;

afterEach(cleanup);
beforeEach(() => {
  mockGet.mockReset();
  mockResolve.mockReset();
});

function memberHealthProblem(over: Partial<AttentionSignal> = {}): AttentionSignal {
  return {
    id: "sig-mh1",
    kind: "problem",
    blocking: true,
    source: "member_health",
    stage: "brainstorming",
    title: "Member unhealthy: m-1 (auth_failed)",
    summary: "m-1 (claude_cli.opus) failed 1×: auth_failed. Run the login command…",
    pmEvaluation: "Member m-1 (pm, claude_cli.opus) failed 1 time with 'auth_failed'.",
    suggestions: [
      { id: "open_provider_settings", label: "Open provider settings", detail: "Log in" },
      { id: "disable_member", label: "Disable this member & continue", detail: "Drop it" },
      { id: "stop", label: "Stop and let me look", detail: "Pause" },
    ],
    state: "open",
    resolution: null,
    context: {
      member_id: "m-1",
      coding_role: "pm",
      gateway_route_id: "claude_cli.opus",
      reason: "auth_failed",
      detail: "API Error: 401 … Please run /login",
      remediation: "Run the login command for this provider in Settings → Providers, then retry.",
      attempts: 1,
    },
    createdAt: "t1",
    ...over,
  };
}

function workerUnproductiveProblem(): AttentionSignal {
  return memberHealthProblem({
    id: "sig-worker",
    source: "worker_unproductive",
    title: "Task stuck: no member can produce a usable turn (t-1)",
    summary: "The coding workers returned invalid turns.",
    suggestions: [
      { id: "edit_room", label: "Edit room", detail: "Use a stronger model" },
      { id: "stop", label: "Stop and let me look", detail: "Pause" },
    ],
    context: {
      task_id: "t-1",
      member_id: "m-dev-1",
      gateway_route_id: "claude_cli.haiku",
      reason: "turn_tool_markup_only",
      remediation: "Switch this role to a stronger model in the room editor.",
    },
  });
}

describe("member-health ProblemCard", () => {
  it("renders the member + reason + remediation", async () => {
    mockGet.mockResolvedValue({ signals: [memberHealthProblem()], blocksStage: true });
    render(<AttentionFeed projectId="p" />);

    await screen.findByText("Member unhealthy: m-1 (auth_failed)");
    // member id + role + route surfaced
    expect(screen.getByText(/m-1 \(pm\) — claude_cli\.opus/)).toBeInTheDocument();
    // human-readable reason label
    expect(screen.getByText("Not logged in")).toBeInTheDocument();
    // remediation present
    expect(
      screen.getByText(/Run the login command for this provider/),
    ).toBeInTheDocument();
  });

  it("offers Open provider settings + the disable/stop suggestions", async () => {
    const onOpen = vi.fn();
    mockGet.mockResolvedValue({ signals: [memberHealthProblem()], blocksStage: true });
    render(<AttentionFeed projectId="p" onOpenProviderSettings={onOpen} />);

    const openBtn = await screen.findByRole("button", { name: "Open provider settings" });
    await userEvent.click(openBtn);
    expect(onOpen).toHaveBeenCalledTimes(1);

    // The disable-member + stop suggestions render as Accept actions.
    expect(
      screen.getByRole("button", { name: /Disable this member & continue/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Stop and let me look/ }),
    ).toBeInTheDocument();
  });

  it("accepting 'disable this member & continue' resolves the signal", async () => {
    mockGet
      .mockResolvedValueOnce({ signals: [memberHealthProblem()], blocksStage: true })
      .mockResolvedValueOnce({ signals: [], blocksStage: false });
    mockResolve.mockResolvedValue({});
    render(<AttentionFeed projectId="p" />);

    const btn = await screen.findByRole("button", {
      name: /Disable this member & continue/,
    });
    await userEvent.click(btn);
    await waitFor(() =>
      expect(mockResolve).toHaveBeenCalledWith("p", "sig-mh1", {
        action: "accept",
        suggestionId: "disable_member",
      }),
    );
  });

  it("does not render the member-health block for a non-member-health problem", async () => {
    const generic = memberHealthProblem({
      source: "monitor",
      title: "Stuck: no_progress",
      context: {},
    });
    mockGet.mockResolvedValue({ signals: [generic], blocksStage: true });
    render(<AttentionFeed projectId="p" />);
    await screen.findByText("Stuck: no_progress");
    expect(screen.queryByText("Not logged in")).not.toBeInTheDocument();
  });
});

describe("worker-unproductive ProblemCard", () => {
  it("shows the task remediation and opens the room editor", async () => {
    const onOpenRoom = vi.fn();
    mockGet.mockResolvedValue({
      signals: [workerUnproductiveProblem()],
      blocksStage: true,
    });
    render(<AttentionFeed projectId="p" onOpenRoomSettings={onOpenRoom} />);

    await screen.findByText("Task stuck: no member can produce a usable turn (t-1)");
    expect(screen.getByText("t-1")).toBeInTheDocument();
    expect(screen.getByText(/Switch this role to a stronger model/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit room" }));
    expect(onOpenRoom).toHaveBeenCalledTimes(1);
    expect(mockResolve).not.toHaveBeenCalled();
  });
});

describe("PreflightBlockedBanner", () => {
  const unhealthy: PreflightUnhealthy[] = [
    {
      provider: "claude_cli",
      route: "claude_cli.opus",
      reason: "auth_failed",
      detail: "not logged in",
      remediation: "Run the login command, then start again.",
      memberIds: ["m-1", "m-2", "m-4"],
    },
  ];

  it("lists the unhealthy provider + the members that use it", () => {
    render(<PreflightBlockedBanner unhealthy={unhealthy} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/Can't start/);
    expect(screen.getByText("claude_cli.opus")).toBeInTheDocument();
    expect(screen.getByText("Not logged in")).toBeInTheDocument();
    expect(screen.getByText(/used by m-1, m-2, m-4/)).toBeInTheDocument();
    expect(screen.getByText(/Run the login command/)).toBeInTheDocument();
  });

  it("renders nothing when there are no unhealthy providers", () => {
    const { container } = render(<PreflightBlockedBanner unhealthy={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("wires Open provider settings + Dismiss", async () => {
    const onOpen = vi.fn();
    const onDismiss = vi.fn();
    render(
      <PreflightBlockedBanner
        unhealthy={unhealthy}
        onOpenProviderSettings={onOpen}
        onDismiss={onDismiss}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Open provider settings" }));
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
