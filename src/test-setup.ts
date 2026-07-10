import "@testing-library/jest-dom/vitest";
import { afterEach, expect, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import * as axeMatchers from "vitest-axe/matchers";
import "vitest-axe/extend-expect";

// F031-DEMO-A11Y-SWEEP Task 1 — register vitest-axe matchers globally so
// any test author can reach for `expect(...).toHaveNoViolations()`
// directly. Council components use the shared `expectNoA11yViolations`
// helper from `src/features/council/a11y-helpers.ts` for the impact
// filter; the raw matcher is still available for ad-hoc cases.
expect.extend(axeMatchers);

// happy-dom doesn't implement Element.prototype.scrollIntoView. Stub it
// so components that auto-scroll on mount (e.g. ContextProvenanceCompare's
// focused-column effect) don't throw under vitest.
if (typeof Element !== "undefined") {
  Element.prototype.scrollIntoView = vi.fn();
}

afterEach(() => {
  cleanup();
});
