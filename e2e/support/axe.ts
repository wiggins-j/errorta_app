import { createRequire } from "node:module";
import { expect, type Page } from "@playwright/test";

const require = createRequire(import.meta.url);
const axePath = require.resolve("axe-core/axe.min.js");

interface AxeViolation {
  id: string;
  impact: string | null;
  description: string;
  help: string;
  nodes: Array<{ target: string[]; failureSummary?: string }>;
}

export async function expectNoAxeViolations(page: Page, selector = "body") {
  await page.addScriptTag({ path: axePath });
  const violations = await page.evaluate(async (targetSelector) => {
    const target = document.querySelector(targetSelector) ?? document.body;
    const result = await (window as typeof window & {
      axe: {
        run: (
          context: Element,
          options: Record<string, unknown>,
        ) => Promise<{ violations: AxeViolation[] }>;
      };
    }).axe.run(target, {
      resultTypes: ["violations"],
      runOnly: {
        type: "tag",
        values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
      },
    });
    return result.violations.filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    );
  }, selector);

  expect(violations).toEqual([]);
}
