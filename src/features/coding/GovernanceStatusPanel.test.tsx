import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import GovernanceStatusPanel from "./GovernanceStatusPanel";
import type { GovernanceStatus } from "../../lib/api/coding";

afterEach(cleanup);

function status(over: Partial<GovernanceStatus> = {}): GovernanceStatus {
  return {
    mode: "strict",
    stage: "brainstorm",
    status: "under_review",
    headline: "Brainstorm — under review",
    actorMemberId: "m-rev",
    actorLabel: "Echo-REV",
    reviewPass: "reviewer",
    steps: [
      { stage: "brainstorm", state: "under_review" },
      { stage: "spec", state: "pending" },
      { stage: "plan", state: "pending" },
      { stage: "build", state: "pending" },
      { stage: "done", state: "pending" },
    ],
    buildProgress: null,
    ...over,
  };
}

describe("GovernanceStatusPanel", () => {
  it("renders collapsed Building Progress summary from a strict under_review status", () => {
    render(<GovernanceStatusPanel status={status()} />);
    const panel = screen.getByText("Building Progress").closest("details") as HTMLDetailsElement;
    expect(panel).not.toBeNull();
    expect(panel.open).toBe(false);
    expect(screen.getByText("Brainstorm — under review")).toBeInTheDocument();
    expect(screen.getByText("· Echo-REV")).toBeInTheDocument();
    expect(screen.getByText("Under review")).toBeInTheDocument();
  });

  it("opens to show all five stages with aria-current on the active stage", () => {
    render(<GovernanceStatusPanel status={status()} />);
    fireEvent.click(screen.getByText("Building Progress"));
    const region = screen.getByLabelText("Governance status");
    const current = region.querySelector('[aria-current="step"]');
    expect(current).not.toBeNull();
    expect(current?.textContent).toContain("Brainstorm");
    expect(screen.getByText("Brainstorm")).toBeInTheDocument();
    expect(screen.getByText("Spec")).toBeInTheDocument();
    expect(screen.getByText("Plan")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("shows the PM actor on the PM review pass", () => {
    render(
      <GovernanceStatusPanel
        status={status({
          reviewPass: "pm",
          actorMemberId: "m-pm",
          actorLabel: "PM-Prime",
          steps: [
            { stage: "brainstorm", state: "under_review" },
            { stage: "spec", state: "pending" },
            { stage: "plan", state: "pending" },
            { stage: "build", state: "pending" },
            { stage: "done", state: "pending" },
          ],
        })}
      />,
    );
    expect(screen.getByText("· PM-Prime")).toBeInTheDocument();
  });

  it("renders changes-requested with a flagged step", () => {
    render(
      <GovernanceStatusPanel
        status={status({
          status: "changes_requested",
          headline: "Brainstorm — changes requested",
          actorLabel: "PM-Prime",
          steps: [
            { stage: "brainstorm", state: "changes_requested" },
            { stage: "spec", state: "pending" },
            { stage: "plan", state: "pending" },
            { stage: "build", state: "pending" },
            { stage: "done", state: "pending" },
          ],
        })}
      />,
    );
    expect(screen.getByText("Brainstorm — changes requested")).toBeInTheDocument();
    expect(screen.getByText("Changes requested")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Building Progress"));
    const region = screen.getByLabelText("Governance status");
    expect(region.querySelector(".coding-gov-step-changes_requested")).not.toBeNull();
  });

  it("shows build progress (N/M) during the build stage", () => {
    render(
      <GovernanceStatusPanel
        status={status({
          stage: "build",
          status: "building",
          headline: "Building",
          actorLabel: null,
          reviewPass: null,
          actorMemberId: null,
          buildProgress: { done: 3, total: 8 },
          steps: [
            { stage: "brainstorm", state: "approved" },
            { stage: "spec", state: "approved" },
            { stage: "plan", state: "approved" },
            { stage: "build", state: "building" },
            { stage: "done", state: "pending" },
          ],
        })}
      />,
    );
    expect(screen.getByText("(3/8)")).toBeInTheDocument();
    // "Building" appears both as the headline and the pill; assert the pill.
    const region = screen.getByLabelText("Governance status");
    expect(region.querySelector(".coding-gov-pill-building")?.textContent).toBe("Building");
    expect(region.querySelector(".coding-gov-step-building")?.textContent).toContain(
      "building",
    );
  });

  it("renders nothing when governance is off", () => {
    const { container } = render(
      <GovernanceStatusPanel
        status={status({ mode: "off", status: null })}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when status is null", () => {
    const { container } = render(
      <GovernanceStatusPanel status={null} />,
    );
    expect(container.firstChild).toBeNull();
  });


  it("renders the inline Read/Comment/Accept actions for a stuck status", () => {
    const onOpen = vi.fn();
    const onComment = vi.fn();
    render(
      <GovernanceStatusPanel
        status={status({
          status: "stuck",
          needsHuman: true,
          reviewRound: 3,
          headline: "Brainstorm — needs you · stuck after 3 rounds",
          steps: [
            { stage: "brainstorm", state: "stuck" },
            { stage: "spec", state: "pending" },
            { stage: "plan", state: "pending" },
            { stage: "build", state: "pending" },
            { stage: "done", state: "pending" },
          ],
        })}
        onOpenBrainstorm={onOpen}
        onCommentBrainstorm={onComment}
      />,
    );
    // F125: a stuck/needs-human run auto-expands so the blocking call-to-action
    // is visible without a click.
    const panel = screen.getByText("Building Progress").closest("details") as HTMLDetailsElement;
    expect(panel.open).toBe(true);
    expect(screen.getByText("Needs you")).toBeInTheDocument();
    const stuckRegion = screen.getByLabelText("Governance needs you");
    expect(stuckRegion.textContent).toMatch(/after 3 rounds/i);
    const region = screen.getByLabelText("Governance status");
    expect(region.querySelector(".coding-gov-step-stuck")?.textContent).toContain(
      "needs you",
    );
    fireEvent.click(screen.getByRole("button", { name: "Read brainstorm" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    expect(onComment).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Accept & continue" }));
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("opens approved artifact-backed stages from their step buttons", () => {
    const onOpenStage = vi.fn();
    render(
      <GovernanceStatusPanel
        status={status({
          stage: "build",
          status: "building",
          headline: "Building",
          actorLabel: null,
          actorMemberId: null,
          reviewPass: null,
          steps: [
            { stage: "brainstorm", state: "approved" },
            { stage: "spec", state: "approved" },
            { stage: "plan", state: "approved" },
            { stage: "build", state: "building" },
            { stage: "done", state: "pending" },
          ],
        })}
        onOpenStage={onOpenStage}
      />,
    );
    fireEvent.click(screen.getByText("Building Progress"));
    fireEvent.click(screen.getByRole("button", { name: "Open Brainstorm details" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Spec details" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Plan details" }));
    expect(onOpenStage.mock.calls).toEqual([["brainstorm"], ["spec"], ["plan"]]);
    expect(screen.queryByRole("button", { name: "Open Build details" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open Done details" })).toBeNull();
  });

  it("opens stuck and changes-requested artifact-backed stages from their step buttons", () => {
    const onOpenStage = vi.fn();
    render(
      <GovernanceStatusPanel
        status={status({
          status: "changes_requested",
          headline: "Spec — changes requested",
          stage: "spec",
          steps: [
            { stage: "brainstorm", state: "approved" },
            { stage: "spec", state: "changes_requested" },
            { stage: "plan", state: "stuck" },
            { stage: "build", state: "pending" },
            { stage: "done", state: "pending" },
          ],
        })}
        onOpenStage={onOpenStage}
      />,
    );
    fireEvent.click(screen.getByText("Building Progress"));
    fireEvent.click(screen.getByRole("button", { name: "Open Spec details" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Plan details" }));
    expect(onOpenStage.mock.calls).toEqual([["spec"], ["plan"]]);
  });

  it("does not make drafting, under-review, or pending stages clickable", () => {
    render(
      <GovernanceStatusPanel
        status={status({
          steps: [
            { stage: "brainstorm", state: "under_review" },
            { stage: "spec", state: "drafting" },
            { stage: "plan", state: "pending" },
            { stage: "build", state: "pending" },
            { stage: "done", state: "pending" },
          ],
        })}
        onOpenStage={() => {}}
      />,
    );
    fireEvent.click(screen.getByText("Building Progress"));
    expect(screen.queryByRole("button", { name: "Open Brainstorm details" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open Spec details" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open Plan details" })).toBeNull();
  });

  it("never renders raw artifact content or member ids", () => {
    const { container } = render(
      <GovernanceStatusPanel
        status={status({ actorMemberId: "m-secret-id-xyz" })}
      />,
    );
    // the actor member id is data only — only the human label is shown
    expect(container.textContent).not.toContain("m-secret-id-xyz");
  });
});
