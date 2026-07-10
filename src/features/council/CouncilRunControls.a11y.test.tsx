// F031-DEMO-A11Y-SWEEP Task 2 — axe-core sweep for the Pause / Resume
// / Cancel toolbar. Disabled buttons must still carry accessible names
// (axe-core will catch missing names on a disabled control).
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, it } from "vitest";

import CouncilRunControls from "./CouncilRunControls";
import type { CouncilRunStatus } from "./types";
import { expectNoA11yViolations } from "./a11y-helpers";

function status(state: CouncilRunStatus["state"]): CouncilRunStatus {
  return { runId: "r-1", state, backendStatus: state };
}

afterEach(() => cleanup());

describe("CouncilRunControls a11y", () => {
  it("no_violations_idle", async () => {
    // Idle == paused: Pause disabled, Resume enabled, Cancel enabled.
    const { container } = render(
      <CouncilRunControls
        status={status("paused")}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
      />,
    );
    await expectNoA11yViolations(container);
  });

  it("no_violations_running", async () => {
    const { container } = render(
      <CouncilRunControls
        status={status("running")}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
      />,
    );
    await expectNoA11yViolations(container);
  });

  it("no_violations_in_flight_transition", async () => {
    // Terminal "done" state — Cancel disabled, Pause disabled, Resume
    // disabled. Tests the all-disabled triplet path.
    const { container } = render(
      <CouncilRunControls
        status={status("done")}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
      />,
    );
    await expectNoA11yViolations(container);
  });
});
