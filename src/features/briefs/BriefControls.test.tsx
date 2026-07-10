import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import BriefControls from "./BriefControls";
import type { BriefStateValue } from "./types";

function makeHandlers() {
  return {
    onValidate: vi.fn<() => void>(),
    onRun: vi.fn<() => void>(),
    onPause: vi.fn<() => void>(),
    onRefresh: vi.fn<() => void>(),
    onArchive: vi.fn<() => void>(),
  };
}

function renderControls(state: BriefStateValue, busy = false) {
  const h = makeHandlers();
  render(
    <BriefControls
      state={state}
      briefId="demo-brief"
      markdown="---\nproject: Demo\n---\n"
      busy={busy}
      {...h}
    />,
  );
  return h;
}

describe("BriefControls — per-FSM-state button visibility", () => {
  it("DRAFT shows only Validate", () => {
    renderControls("DRAFT");
    expect(screen.getByRole("button", { name: /validate/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^run$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pause/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /archive/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /refresh/i })).not.toBeInTheDocument();
  });

  it("VALIDATING shows Validate + Run (DRAFT and RUNNING are reachable)", () => {
    renderControls("VALIDATING");
    expect(screen.getByRole("button", { name: /^run$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pause/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /archive/i })).not.toBeInTheDocument();
  });

  it("RUNNING shows Pause (not Run, not Validate)", () => {
    renderControls("RUNNING");
    expect(screen.getByRole("button", { name: /pause/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^run$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /validate/i })).not.toBeInTheDocument();
  });

  it("PAUSED shows Resume + Archive", () => {
    renderControls("PAUSED");
    expect(screen.getByRole("button", { name: /resume/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /archive/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pause/i })).not.toBeInTheDocument();
  });

  it("COMPLETED shows Run + Refresh + Archive", () => {
    renderControls("COMPLETED");
    expect(screen.getByRole("button", { name: /^run$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /refresh/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /archive/i })).toBeInTheDocument();
  });

  it("FAILED shows Archive (DRAFT reachable but Run not directly)", () => {
    renderControls("FAILED");
    expect(screen.getByRole("button", { name: /archive/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pause/i })).not.toBeInTheDocument();
  });

  it("ARCHIVED renders terminal copy and keeps Export available", () => {
    renderControls("ARCHIVED");
    expect(screen.getByText(/archived — no further actions/i)).toBeInTheDocument();
    // Export must still render so users can extract the markdown after archival.
    expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
    // No FSM actions.
    expect(screen.queryByRole("button", { name: /validate/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^run$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pause/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /archive/i })).not.toBeInTheDocument();
  });

  it.each(["DRAFT", "VALIDATING", "RUNNING", "PAUSED", "COMPLETED", "FAILED"] as const)(
    "renders Export in non-ARCHIVED state %s",
    (state) => {
      renderControls(state);
      expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
    },
  );

  it("RUNNING — Export sits between Run/Resume slot and Pause in DOM order", () => {
    renderControls("RUNNING");
    const buttons = screen.getAllByRole("button").map((b) => b.textContent ?? "");
    const exportIdx = buttons.findIndex((t) => /export/i.test(t));
    const pauseIdx = buttons.findIndex((t) => /pause/i.test(t));
    expect(exportIdx).toBeGreaterThanOrEqual(0);
    expect(pauseIdx).toBeGreaterThanOrEqual(0);
    expect(exportIdx).toBeLessThan(pauseIdx);
  });

  it("DRAFT — Export renders after Validate", () => {
    renderControls("DRAFT");
    const buttons = screen.getAllByRole("button").map((b) => b.textContent ?? "");
    const validateIdx = buttons.findIndex((t) => /validate/i.test(t));
    const exportIdx = buttons.findIndex((t) => /export/i.test(t));
    expect(validateIdx).toBeLessThan(exportIdx);
  });

  it("busy=true disables every visible button (COMPLETED has three)", () => {
    renderControls("COMPLETED", true);
    const buttons = screen.getAllByRole("button");
    expect(buttons.length).toBeGreaterThan(0);
    for (const b of buttons) {
      expect(b).toBeDisabled();
    }
  });

  it("fires the correct handler when the corresponding button is clicked", async () => {
    const user = userEvent.setup();
    const h = renderControls("COMPLETED");
    await user.click(screen.getByRole("button", { name: /^run$/i }));
    await user.click(screen.getByRole("button", { name: /refresh/i }));
    await user.click(screen.getByRole("button", { name: /archive/i }));
    expect(h.onRun).toHaveBeenCalledTimes(1);
    expect(h.onRefresh).toHaveBeenCalledTimes(1);
    expect(h.onArchive).toHaveBeenCalledTimes(1);
  });

  it("fires onValidate when Validate is clicked from DRAFT", async () => {
    const user = userEvent.setup();
    const h = renderControls("DRAFT");
    await user.click(screen.getByRole("button", { name: /validate/i }));
    expect(h.onValidate).toHaveBeenCalledTimes(1);
  });

  it("fires onPause when Pause is clicked from RUNNING", async () => {
    const user = userEvent.setup();
    const h = renderControls("RUNNING");
    await user.click(screen.getByRole("button", { name: /pause/i }));
    expect(h.onPause).toHaveBeenCalledTimes(1);
  });
});
