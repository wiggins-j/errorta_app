import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", () => ({
  getGovernanceArtifact: vi.fn(),
  interject: vi.fn(),
  resumeRun: vi.fn(),
  continueRun: vi.fn(),
  acceptGovernanceArtifact: vi.fn(),
}));

import BrainstormViewer from "./BrainstormViewer";
import * as api from "../../lib/api/coding";
import type { GovernanceArtifact, GovernanceSummary } from "../../lib/api/coding";

const getArtifact = vi.mocked(api.getGovernanceArtifact);
const interject = vi.mocked(api.interject);
const resumeRun = vi.mocked(api.resumeRun);
const continueRun = vi.mocked(api.continueRun);
const acceptArtifact = vi.mocked(api.acceptGovernanceArtifact);

function artifact(over: Partial<GovernanceArtifact> = {}): GovernanceArtifact {
  return {
    artifactId: "a-bs-2",
    artifactKind: "brainstorm",
    version: 2,
    state: "changes_requested",
    title: "Project brainstorm",
    bodyMarkdown: "# Direction\nBuild the thing.",
    sourceRefs: [],
    supersedesArtifactId: null,
    createdAt: "",
    ...over,
  };
}

function summary(over: Partial<GovernanceSummary> = {}): GovernanceSummary {
  return {
    state: {
      mode: "strict",
      phase: "reviewing_brainstorm",
      humanCodeApproval: "final_only",
      activeArtifactIds: {},
      blockOnProblems: true,
      monitor: {},
      updatedAt: "",
    },
    artifacts: [
      { ...artifact({ artifactId: "a-bs-1", version: 1, title: "Old" }) },
      { ...artifact() },
    ],
    reviews: [
      {
        reviewId: "r-1",
        artifactId: "a-bs-2",
        reviewerMemberId: "m-rev-secret",
        verdict: "changes_requested",
        findings: [
          { severity: "high", title: "Missing audience", body: "Who is this for?", blocking: true },
        ],
        createdAt: "",
      },
    ],
    approvals: [],
    planSlices: [],
    ...over,
  };
}

beforeEach(() => {
  getArtifact.mockResolvedValue(artifact());
  interject.mockResolvedValue({ message: "", at: "", pmReply: null });
  resumeRun.mockResolvedValue(true);
  continueRun.mockResolvedValue(true);
  acceptArtifact.mockResolvedValue(summary());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BrainstormViewer", () => {
  it("renders the latest brainstorm body read-only with version, state, and findings", async () => {
    render(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Artifact content").textContent).toContain(
        "Build the thing.",
      ),
    );
    // fetched the LATEST (v2) id, not the old v1
    expect(getArtifact).toHaveBeenCalledWith("p1", "a-bs-2");
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.getByText("changes_requested")).toBeInTheDocument();
    // the latest review's finding is shown
    expect(screen.getByText("Missing audience")).toBeInTheDocument();
    // body is rendered inside a <pre> (read-only, no markdown injection)
    expect(screen.getByLabelText("Artifact content").tagName).toBe("PRE");
  });

  it("shows a 'newer version available' hint when the summary advances past the opened version", async () => {
    const { rerender } = render(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    // opened pins to v2; no hint yet
    await waitFor(() => expect(getArtifact).toHaveBeenCalledWith("p1", "a-bs-2"));
    expect(screen.queryByText(/newer version/i)).toBeNull();
    // the live summary now carries a newer v3 brainstorm
    rerender(
      <BrainstormViewer
        projectId="p1"
        summary={summary({
          artifacts: [
            artifact({ artifactId: "a-bs-2", version: 2 }),
            artifact({ artifactId: "a-bs-3", version: 3, title: "Newer" }),
          ],
        })}
        running
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByText(/newer version/i)).toBeInTheDocument();
  });

  it("sends a comment as an interjection tagged with the viewed artifact id", async () => {
    const onChanged = vi.fn();
    render(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running
        onClose={() => {}}
        onChanged={onChanged}
      />,
    );
    await waitFor(() => expect(getArtifact).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Comment to the PM"), {
      target: { value: "Focus on memory" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send to PM" }));
    await waitFor(() =>
      expect(interject).toHaveBeenCalledWith("p1", "Focus on memory", "a-bs-2"),
    );
    expect(resumeRun).not.toHaveBeenCalled();
    expect(continueRun).not.toHaveBeenCalled();
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    // Bug 2: "Send to PM" gives a visible confirmation so it isn't read as a no-op.
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toMatch(/sent to the pm/i),
    );
  });

  it("offers 'Send & continue' only when stopped; it interjects then CONTINUES (never the crash-recovery resume)", async () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running
        onClose={onClose}
        onChanged={() => {}}
      />,
    );
    await waitFor(() => expect(getArtifact).toHaveBeenCalled());
    // running -> no continue button
    expect(screen.queryByRole("button", { name: "Send & continue" })).toBeNull();
    // stopped -> continue button appears
    rerender(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running={false}
        onClose={onClose}
        onChanged={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText("Comment to the PM"), {
      target: { value: "go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send & continue" }));
    await waitFor(() => expect(interject).toHaveBeenCalledWith("p1", "go", "a-bs-2"));
    await waitFor(() => expect(continueRun).toHaveBeenCalledWith("p1"));
    // Bug 2 regression lock: the crash-recovery resume endpoint is NEVER used for
    // a review-stopped governance run (that path 409s "run is not recoverable").
    expect(resumeRun).not.toHaveBeenCalled();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("surfaces a continue failure as a visible error (Bug 2 actionable failure)", async () => {
    continueRun.mockRejectedValueOnce(new Error("continue run failed (409)"));
    render(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running={false}
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    await waitFor(() => expect(getArtifact).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Comment to the PM"), {
      target: { value: "go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send & continue" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("continue run failed"),
    );
  });

  it("accepts the viewed artifact id after confirmation", async () => {
    const onClose = vi.fn();
    render(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running={false}
        onClose={onClose}
        onChanged={() => {}}
      />,
    );
    await waitFor(() => expect(getArtifact).toHaveBeenCalled());
    fireEvent.click(
      screen.getByRole("button", { name: "Accept this brainstorm & continue" }),
    );
    // confirm step
    fireEvent.click(screen.getByRole("button", { name: "Confirm accept" }));
    await waitFor(() =>
      expect(acceptArtifact).toHaveBeenCalledWith("p1", "a-bs-2"),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("opens and accepts the SPEC artifact when the spec stage is stuck", async () => {
    // Bug regression: a stuck spec used to open the already-approved brainstorm,
    // so accepting did nothing. With stage="spec" the viewer must target the spec.
    const specArtifact = artifact({
      artifactId: "a-spec-1",
      artifactKind: "spec",
      version: 1,
      title: "Project spec",
    });
    getArtifact.mockResolvedValue(specArtifact);
    const withSpec = summary({
      artifacts: [
        artifact({ state: "approved" }), // brainstorm a-bs-2 (already approved)
        specArtifact,
      ],
    });
    render(
      <BrainstormViewer
        projectId="p1"
        summary={withSpec}
        stage="spec"
        running={false}
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    // It fetched the SPEC body, not the brainstorm.
    await waitFor(() => expect(getArtifact).toHaveBeenCalledWith("p1", "a-spec-1"));
    fireEvent.click(
      screen.getByRole("button", { name: "Accept this spec & continue" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm accept" }));
    // Accept targets the SPEC id — the actual fix.
    await waitFor(() =>
      expect(acceptArtifact).toHaveBeenCalledWith("p1", "a-spec-1"),
    );
  });

  it("surfaces a 409 accept error as visible text", async () => {
    acceptArtifact.mockRejectedValueOnce(
      new Error("This brainstorm was superseded by a newer version — refresh and try again."),
    );
    render(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running={false}
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    await waitFor(() => expect(getArtifact).toHaveBeenCalled());
    fireEvent.click(
      screen.getByRole("button", { name: "Accept this brainstorm & continue" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm accept" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("superseded"),
    );
  });

  it("renders structured fields (not a blank pre) when bodyMarkdown is empty (Bug 1)", async () => {
    // A spec whose written body is blank but whose acceptance criteria + body_json
    // are populated must render the structured content, not an empty box.
    const blankBodySpec = artifact({
      artifactId: "a-spec-1",
      artifactKind: "spec",
      version: 1,
      title: "Auth spec",
      bodyMarkdown: "   ", // whitespace-only — the exact empty-box failure
      bodyJson: { acceptance_criteria: ["Login works", "Logout clears session"] },
    });
    getArtifact.mockResolvedValue(blankBodySpec);
    const withSpec = summary({
      artifacts: [artifact({ state: "approved" }), blankBodySpec],
    });
    render(
      <BrainstormViewer
        projectId="p1"
        summary={withSpec}
        stage="spec"
        running={false}
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    await waitFor(() => expect(getArtifact).toHaveBeenCalledWith("p1", "a-spec-1"));
    const body = screen.getByLabelText("Artifact content");
    // Not a bare blank <pre>: it explains the empty body and shows the criteria.
    expect(body.textContent).toMatch(/no written body/i);
    expect(body.textContent).toContain("Login works");
    expect(body.textContent).toContain("Logout clears session");
  });

  it("renders no member ids or raw tokens", async () => {
    const { container } = render(
      <BrainstormViewer
        projectId="p1"
        summary={summary()}
        running
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    await waitFor(() => expect(getArtifact).toHaveBeenCalled());
    expect(container.textContent).not.toContain("m-rev-secret");
  });
});
