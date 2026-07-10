import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@uiw/react-codemirror", () => ({
  default: () => <textarea />,
}));

import CodingProjectView from "./CodingProjectView";
import type { CodingProjectViewProps } from "./CodingProjectView";
import type { RunStatus } from "../../lib/api/coding";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function runStatus(over: Partial<RunStatus> = {}): RunStatus {
  return { running: false, result: null, recoverable: false, canResume: false, ...over };
}

function props(over: Partial<CodingProjectViewProps> = {}): CodingProjectViewProps {
  return {
    project: {
      id: "todo-app",
      northStar: "Build a todo CLI",
      definitionOfDone: "tests pass",
      target: "new",
      status: "active",
      revision: 1,
    },
    tasks: [],
    decisions: [],
    artifacts: [],
    toolEvents: [],
    ...over,
  };
}

describe("CodingProjectView run feedback (F121 Part A)", () => {
  it("shows 'Starting…' with a disabled Start while phase=starting", () => {
    render(<CodingProjectView {...props({ runPhase: "starting", running: false })} />);
    const region = screen.getByRole("region", { name: "Run controls" });
    // The optimistic Start button is disabled and labeled Starting…
    const startBtn = screen.getByRole("button", { name: /starting/i });
    expect(startBtn).toBeDisabled();
    expect(region).toHaveTextContent(/Starting workers/i);
  });

  it("clicking Start immediately disables the button and shows 'Starting…'", () => {
    const onStartRun = vi.fn(() => true);
    render(<CodingProjectView {...props({ onStartRun })} />);

    fireEvent.click(screen.getByRole("button", { name: /start run/i }));

    expect(onStartRun).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /starting/i })).toBeDisabled();
    expect(screen.getByRole("region", { name: "Run controls" })).toHaveTextContent(
      /Starting workers/i,
    );
  });

  it("keeps Start available when the parent refuses the start action", () => {
    const onStartRun = vi.fn(() => false);
    render(<CodingProjectView {...props({ onStartRun })} />);

    fireEvent.click(screen.getByRole("button", { name: /start run/i }));

    expect(onStartRun).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /start run/i })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /starting/i })).toBeNull();
  });

  it("shows a live 'Working' affordance (not a static label) while phase=working", () => {
    render(
      <CodingProjectView
        {...props({
          runPhase: "working",
          running: true,
          workingHeadline: "Drafting spec",
          runStatus: runStatus({ running: true, state: { status: "running" } }),
        })}
      />,
    );
    const region = screen.getByRole("region", { name: "Run controls" });
    // Working surfaces the live governance headline, and Stop run is available.
    expect(region).toHaveTextContent(/Working — Drafting spec/);
    expect(screen.getByRole("button", { name: /stop run/i })).toBeEnabled();
  });

  it("shows 'Stopping…' with a disabled Stop while phase=stopping", () => {
    render(<CodingProjectView {...props({ runPhase: "stopping", running: true })} />);
    const region = screen.getByRole("region", { name: "Run controls" });
    expect(region).toHaveTextContent(/Stopping/i);
    const stopBtn = screen.getByRole("button", { name: /stopping/i });
    expect(stopBtn).toBeDisabled();
  });

  it("renders 'Stopping…' from a mounted cancel_requested+running state (reload survival)", () => {
    // No runPhase prop — the component derives it from the run-state alone. A
    // cancel that was requested before this mount must still read as Stopping…
    render(
      <CodingProjectView
        {...props({
          running: true,
          runStatus: runStatus({
            running: true,
            state: { status: "running", cancel_requested: true },
          }),
        })}
      />,
    );
    expect(screen.getByRole("region", { name: "Run controls" })).toHaveTextContent(/Stopping/i);
  });

  it("fires onCancelRun when Stop run is clicked while working", () => {
    const onCancelRun = vi.fn(() => true);
    render(
      <CodingProjectView {...props({ runPhase: "working", running: true, onCancelRun })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /stop run/i }));
    expect(onCancelRun).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /stopping/i })).toBeDisabled();
    expect(screen.getByRole("region", { name: "Run controls" })).toHaveTextContent(
      /Stopping/i,
    );
  });
});
