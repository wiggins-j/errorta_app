// F031-DEMO-A11Y-SWEEP Task 1 — smoke test for the vitest-axe harness.
// Renders a small known-clean component (AiarReadinessBanner in its
// "needs AIAR" state) and asserts the shared helper returns clean.
// Exists only to prove the integration works under happy-dom; the
// real per-component sweep lives in the Task 2 *.a11y.test.tsx files.
import { render } from "@testing-library/react";
import { describe, it } from "vitest";
import AiarReadinessBanner from "./AiarReadinessBanner";
import { expectNoA11yViolations } from "./a11y-helpers";

describe("a11y-smoke", () => {
  it("smoke_clean_component_returns_no_violations", async () => {
    const { container } = render(<AiarReadinessBanner available={false} />);
    await expectNoA11yViolations(container);
  });
});
