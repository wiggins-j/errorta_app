import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import CouncilQuickStartGuide from "./CouncilQuickStartGuide";
import {
  COUNCIL_FINAL_ANSWER_MODES,
  COUNCIL_STARTING_POINTS,
  COUNCIL_TOPOLOGIES,
} from "./councilQuickStartContent";

afterEach(() => cleanup());

describe("CouncilQuickStartGuide", () => {
  it("renders nothing when closed", () => {
    const { container } = render(
      <CouncilQuickStartGuide open={false} onClose={() => undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("opens as a dialog and closes on the Close button and Escape", () => {
    const onClose = vi.fn();
    render(<CouncilQuickStartGuide open onClose={onClose} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close Quick Start" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("issues no network fetch (static, offline-safe)", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<CouncilQuickStartGuide open onClose={() => undefined} />);
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("documents every enabled starting point, topology, and final-answer mode", () => {
    render(<CouncilQuickStartGuide open onClose={() => undefined} />);
    const body = screen.getByRole("dialog").textContent ?? "";
    for (const name of COUNCIL_STARTING_POINTS) {
      expect(body).toContain(name);
    }
    for (const name of COUNCIL_TOPOLOGIES) {
      expect(body).toContain(name);
    }
    for (const name of COUNCIL_FINAL_ANSWER_MODES) {
      expect(body).toContain(name);
    }
    // Named modes the user cares about.
    expect(body).toContain("Steward");
    expect(body).toContain("Budget & Limits");
    expect(body).toContain("Tools");
  });
});
