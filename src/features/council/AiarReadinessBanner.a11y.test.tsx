// F031-DEMO-A11Y-SWEEP Task 2 — axe-core sweep for AiarReadinessBanner.
// Both readiness states: banner shown (available=false) renders the
// warn message + onboarding link; banner hidden (available=true)
// renders nothing. Both must pass.
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, it } from "vitest";

import AiarReadinessBanner from "./AiarReadinessBanner";
import { expectNoA11yViolations } from "./a11y-helpers";

afterEach(() => cleanup());

describe("AiarReadinessBanner a11y", () => {
  it("no_violations_when_not_ready", async () => {
    const { container } = render(<AiarReadinessBanner available={false} />);
    await expectNoA11yViolations(container);
  });

  it("no_violations_when_ready", async () => {
    // available=true returns null from the component; the container
    // is an empty wrapper, which axe-core treats as clean.
    const { container } = render(<AiarReadinessBanner available={true} />);
    await expectNoA11yViolations(container);
  });
});
