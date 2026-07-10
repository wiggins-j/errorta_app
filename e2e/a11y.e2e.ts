import { expect, test } from "@playwright/test";

import { expectNoAxeViolations } from "./support/axe";
import { expectNoUnhandledSidecarRequests, openApp } from "./support/sidecarMock";

test.describe("E2E-A11Y primary surfaces", () => {
  test("@a11y TC-10.15 app shell and Judge surface have no serious axe violations", async ({ page }) => {
    const sidecar = await openApp(page);

    await expect(page.getByRole("heading", { name: "Judge", level: 2 })).toBeVisible();
    await expectNoAxeViolations(page, "body");
    expectNoUnhandledSidecarRequests(sidecar);
  });

  test("@a11y TC-01.17 TC-01.25 Knowledge panels have no serious axe violations", async ({ page }) => {
    const sidecar = await openApp(page, { activeFeature: "briefs" });

    await expect(page.getByRole("heading", { name: "Briefs", level: 1 })).toBeVisible();
    await expectNoAxeViolations(page, "main");

    await page.getByRole("button", { name: "Corpus" }).click();
    await expect(page.getByRole("heading", { name: "Corpus", level: 1 })).toBeVisible();
    await expectNoAxeViolations(page, "main");

    await page.getByRole("button", { name: "Folder Watcher" }).click();
    await expect(
      page.getByRole("heading", { name: "Folder Watcher", level: 1 }),
    ).toBeVisible();
    await expectNoAxeViolations(page, "main");
    expectNoUnhandledSidecarRequests(sidecar);
  });

  test("@a11y TC-04.16 room management has no serious axe violations", async ({ page }) => {
    const sidecar = await openApp(page, { activeFeature: "rooms" });

    await expect(page.getByRole("heading", { name: "Rooms", level: 2 })).toBeVisible();
    await expectNoAxeViolations(page, "main");
    expectNoUnhandledSidecarRequests(sidecar);
  });
});
