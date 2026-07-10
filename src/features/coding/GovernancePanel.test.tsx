import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    putGovernanceSettings: vi.fn().mockResolvedValue({}),
    approveGovernanceApproval: vi.fn().mockResolvedValue({}),
    rejectGovernanceApproval: vi.fn().mockResolvedValue({}),
  };
});

import {
  approveGovernanceApproval,
  putGovernanceSettings,
  rejectGovernanceApproval,
  type GovernanceSummary,
} from "../../lib/api/coding";
import GovernancePanel from "./GovernancePanel";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function summary(): GovernanceSummary {
  return {
    state: {
      mode: "strict",
      phase: "awaiting_spec_approval",
      humanCodeApproval: "final_only",
      activeArtifactIds: {},
      blockOnProblems: true,
      monitor: {},
      updatedAt: "",
    },
    artifacts: [
      {
        artifactId: "ga_spec_1",
        artifactKind: "spec",
        version: 1,
        state: "awaiting_approval",
        title: "Governed spec",
        sourceRefs: [],
        supersedesArtifactId: null,
        createdAt: "",
      },
    ],
    reviews: [],
    approvals: [
      {
        approvalId: "gap_1",
        kind: "spec_approval",
        artifactId: "ga_spec_1",
        requiredActor: "user",
        state: "pending",
        requestedByMemberId: "m-reviewer",
        resolvedBy: null,
        feedback: "",
        createdAt: "",
        resolvedAt: null,
      },
    ],
    planSlices: [
      {
        sliceId: "S1",
        title: "First slice",
        detail: "",
        dependsOn: [],
        doneWhen: ["done"],
        tests: ["vitest"],
        reviewFocus: ["scope"],
      },
    ],
  };
}

describe("GovernancePanel", () => {
  it("is collapsed by default with the PM Governance title", () => {
    render(<GovernancePanel projectId="p1" governance={summary()} />);
    const panel = screen.getByText("PM Governance").closest("details") as HTMLDetailsElement;
    expect(panel).not.toHaveAttribute("open");
    expect(screen.getByText("PM Governance")).toBeInTheDocument();
  });

  it("changes governance mode through the API", () => {
    const onChanged = vi.fn();
    render(
      <GovernancePanel
        projectId="p1"
        governance={summary()}
        onChanged={onChanged}
      />,
    );
    fireEvent.click(screen.getByText("PM Governance"));

    fireEvent.change(screen.getByLabelText("Human in the loop"), {
      target: { value: "light" },
    });

    expect(putGovernanceSettings).toHaveBeenCalledWith("p1", { mode: "light" });
  });

  it("approves and rejects pending approvals", () => {
    Object.defineProperty(window, "prompt", {
      configurable: true,
      value: vi.fn(),
    });
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("revise criteria");
    render(<GovernancePanel projectId="p1" governance={summary()} />);
    fireEvent.click(screen.getByText("PM Governance"));

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(approveGovernanceApproval).toHaveBeenCalledWith("p1", "gap_1");

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(rejectGovernanceApproval).toHaveBeenCalledWith(
      "p1",
      "gap_1",
      "revise criteria",
    );
    prompt.mockRestore();
  });

  // Autonomy controls relocated here from the run-controls bar.
  const AUTONOMY = {
    maxIterations: 200,
    maxModelCalls: null,
    checkpointCadence: "per_milestone",
    checkpointN: 5,
  };

  it("guardrail defaults checked and toggles", () => {
    const onToggleGuardrail = vi.fn();
    render(
      <GovernancePanel
        projectId="p1"
        governance={summary()}
        guardrailEnabled
        autonomy={AUTONOMY}
        onToggleGuardrail={onToggleGuardrail}
      />,
    );
    fireEvent.click(screen.getByText("PM Governance"));
    const cb = screen.getByRole("checkbox", {
      name: /Superpowers Guardrail/,
    }) as HTMLInputElement;
    expect(cb.checked).toBe(true);
    fireEvent.click(cb);
    expect(onToggleGuardrail).toHaveBeenCalledWith(false);
  });

  it("changes checkpoint cadence and shows the budget", () => {
    const onChangeCadence = vi.fn();
    render(
      <GovernancePanel
        projectId="p1"
        governance={summary()}
        autonomy={AUTONOMY}
        onChangeCadence={onChangeCadence}
      />,
    );
    fireEvent.click(screen.getByText("PM Governance"));
    expect(screen.getByText("Budget: 200 iterations")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Checkpoint cadence"), {
      target: { value: "off" },
    });
    expect(onChangeCadence).toHaveBeenCalledWith("off");
  });
});
