// F031-DEMO-A11Y-SWEEP — shared a11y test helper for the Council demo
// surface. Wraps axe-core via vitest-axe and filters its findings to
// serious / critical impact only so lower-noise findings don't fail
// the suite. Each disabled rule below carries an inline justification
// per the PM-locked policy from the plan; only add new entries if a
// specific rule misfires in happy-dom or on an intentional layout
// pattern we ship.
import { axe } from "vitest-axe";
import { expect } from "vitest";
import type { Result, RunOptions } from "axe-core";

// axe: region disabled because the per-component tests mount fragments
// without their App.tsx <main> wrapper. The shell-level a11y test
// (CouncilShell.a11y.test.tsx) mounts the full <App> with the real
// landmark structure, so landmark coverage is enforced exactly once,
// at the seam where it's load-bearing.
//
// axe: color-contrast disabled because happy-dom does not implement the
// full CSS cascade-resolved computed-style API axe needs to evaluate
// foreground/background pairs. Every run lands as "incomplete: cannot
// verify," which under the QA P2 #7 helper rewrite would fail every
// test. Color-contrast on the Council demo surface is verified
// manually against `--accent-soft` per the design note from the
// F031-DEMO-A11Y-SWEEP cycle; re-run a real-browser axe sweep before
// promoting changes to demo readiness.
export const A11Y_RULE_OVERRIDES: NonNullable<RunOptions["rules"]> = {
  region: { enabled: false },
  "color-contrast": { enabled: false },
};

/**
 * Run axe against the given container and assert that no serious or
 * critical findings are present.
 *
 * QA P2 #7 (2026-06-12): previously this helper only looked at
 * `results.violations`. axe-core uses a separate `results.incomplete`
 * bucket for findings it could not fully verify — color-contrast in
 * happy-dom routinely lands there because the DOM lacks the full
 * cascade-resolved styles axe needs. Silently dropping those let real
 * problems pass our tests. The helper now treats serious/critical
 * incomplete results as failures too, prefixed `[INCOMPLETE]` in the
 * failure message so the operator can distinguish "axe is sure" from
 * "axe couldn't be sure". To suppress a known happy-dom limitation,
 * add a justified rule override above.
 */
export async function expectNoA11yViolations(
  container: HTMLElement,
): Promise<void> {
  const results = await axe(container, { rules: A11Y_RULE_OVERRIDES });
  const isBlockingImpact = (r: Result) =>
    r.impact === "serious" || r.impact === "critical";

  const blockingViolations = results.violations.filter(isBlockingImpact);
  const blockingIncomplete = results.incomplete.filter(isBlockingImpact);

  if (blockingViolations.length === 0 && blockingIncomplete.length === 0) {
    return;
  }
  // Surface a readable diff in the failure output.
  const viol = blockingViolations.map(
    (v) => `[${v.impact}] ${v.id}: ${v.help} (${v.nodes.length} node(s))`,
  );
  const incomp = blockingIncomplete.map(
    (v) =>
      `[INCOMPLETE ${v.impact}] ${v.id}: ${v.help} (${v.nodes.length} node(s))`,
  );
  const total = blockingViolations.length + blockingIncomplete.length;
  const summary = [...viol, ...incomp].join("\n");
  expect.fail(
    `axe-core found ${total} blocking finding(s):\n${summary}`,
  );
}
