import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import CouncilRunControls from "./CouncilRunControls";
import type { CouncilRunStatus } from "./types";

function status(state: CouncilRunStatus["state"]): CouncilRunStatus {
  return { runId: "r-1", state, backendStatus: state };
}

afterEach(() => cleanup());

describe("CouncilRunControls", () => {
  it("renders nothing when there is no run", () => {
    const { container } = render(
      <CouncilRunControls
        status={null}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("Pause enabled when running, Resume disabled, Cancel enabled", () => {
    render(
      <CouncilRunControls
        status={status("running")}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Pause run" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Resume run" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel run" })).not.toBeDisabled();
  });

  it("Resume enabled when paused, Pause disabled", () => {
    render(
      <CouncilRunControls
        status={status("paused")}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Pause run" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Resume run" })).not.toBeDisabled();
  });

  it("Resume enabled when awaiting_decision (QA P2 lock)", () => {
    // F031-09: /resume is valid from awaiting_decision too. Without
    // this lock the UI strands ask-paused runs because Resume stays
    // disabled and there's no separate decision UI in the demo shell.
    render(
      <CouncilRunControls
        status={status("awaiting_decision")}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Resume run" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Pause run" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel run" })).not.toBeDisabled();
  });

  it("Cancel disabled when terminal", () => {
    render(
      <CouncilRunControls
        status={status("done")}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeDisabled();
  });

  it("calls the right callback on click", () => {
    const onPause = vi.fn();
    const onResume = vi.fn();
    const onCancel = vi.fn();
    render(
      <CouncilRunControls
        status={status("running")}
        onPause={onPause}
        onResume={onResume}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Pause run" }));
    expect(onPause).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    expect(onCancel).toHaveBeenCalled();
  });
});
