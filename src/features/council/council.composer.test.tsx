// Vitest — the dry-fake checkbox must be hidden when import.meta.env.PROD === true
// (invariant 10: fake members are first-class but never silently default in prod).
import { describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import CouncilPromptComposer from "./CouncilPromptComposer";

describe("CouncilPromptComposer", () => {
  it("hides Fake-run toggle when import.meta.env.PROD is true", () => {
    vi.stubEnv("PROD", true);
    render(
      <CouncilPromptComposer
        disabled={false}
        onRun={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(screen.queryByLabelText(/Fake-run/i)).toBeNull();
    vi.unstubAllEnvs();
  });

  it("shows Fake-run toggle in dev builds", () => {
    vi.stubEnv("PROD", false);
    render(
      <CouncilPromptComposer
        disabled={false}
        onRun={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(screen.getByText(/Fake-run/i)).toBeInTheDocument();
    vi.unstubAllEnvs();
  });

  it("sends an interjection (not a new run) while the run is live", () => {
    cleanup();
    const onRun = vi.fn();
    const onInterject = vi.fn();
    render(
      <CouncilPromptComposer
        disabled={false}
        onRun={onRun}
        onCancel={() => undefined}
        onInterject={onInterject}
        runState="running"
      />,
    );
    const box = screen.getByLabelText(/message the council/i);
    fireEvent.change(box, { target: { value: "steer toward cost" } });
    fireEvent.click(screen.getByTestId("council-interject-send"));
    expect(onInterject).toHaveBeenCalledWith("steer toward cost");
    expect(onRun).not.toHaveBeenCalled();
    // Stop is still available while live.
    expect(screen.getByText("Stop")).toBeInTheDocument();
  });

  it("does not offer Send when no interjection handler is wired", () => {
    cleanup();
    render(
      <CouncilPromptComposer
        disabled={false}
        onRun={() => undefined}
        onCancel={() => undefined}
        runState="running"
      />,
    );
    expect(screen.queryByTestId("council-interject-send")).toBeNull();
  });
});
