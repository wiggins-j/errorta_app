import { expect, test } from "@playwright/test";

import { expectNoUnhandledSidecarRequests, openApp } from "./support/sidecarMock";

test.describe("E2E-UI app shell", () => {
  test("TC-10.15 navigates the Knowledge panels without leaving the active corpus context", async ({ page }) => {
    const sidecar = await openApp(page);

    const knowledgeGroup = page.getByRole("button", { name: "Knowledge" });
    await expect(knowledgeGroup).toHaveAttribute("aria-expanded", "true");

    await page.getByRole("button", { name: "Briefs" }).click();
    await expect(page.getByRole("heading", { name: "Briefs", level: 1 })).toBeVisible();
    await expect(page.getByLabel("Active corpus", { exact: true })).toHaveValue("demo-corpus");
    await expect(page.getByText("No briefs target the active corpus yet.")).toBeVisible();

    await page.getByRole("button", { name: "Corpus" }).click();
    await expect(page.getByRole("heading", { name: "Corpus", level: 1 })).toBeVisible();
    await expect(page.getByRole("button", { name: "Check for changes" })).toBeEnabled();
    await expect(page.getByRole("table")).toContainText("welcome.md");

    await page.getByRole("button", { name: "Folder Watcher" }).click();
    await expect(
      page.getByRole("heading", { name: "Folder Watcher", level: 1 }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: "Watch a folder" })).toBeVisible();
    await expect(page.getByText("File source legend")).toBeVisible();

    await expect(page.getByRole("button", { name: "Folder Watcher" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await knowledgeGroup.click();
    await expect(knowledgeGroup).toHaveAttribute("aria-expanded", "false");
    await knowledgeGroup.click();
    await expect(knowledgeGroup).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("button", { name: "Folder Watcher" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expectNoUnhandledSidecarRequests(sidecar);
  });

  test("TC-04.18 creates a shared room from the browser room-management surface", async ({ page }) => {
    const sidecar = await openApp(page, { activeFeature: "rooms" });

    await expect(page.getByRole("heading", { name: "Rooms", level: 2 })).toBeVisible();
    await expect(page.getByText("Demo Room")).toBeVisible();

    await page.getByRole("button", { name: "+ New room" }).click();
    await expect(
      page.getByRole("region", { name: "Council room editor" }),
    ).toBeVisible();
    await expect(page.getByTestId("room-name-input")).toHaveValue("New room");
    await page
      .getByRole("region", { name: "Council room editor" })
      .getByRole("button", { name: "Close" })
      .first()
      .click();

    await expect(page.getByRole("button", { name: /New room rev 1 · draft/ })).toBeVisible();
    expectNoUnhandledSidecarRequests(sidecar);
  });
});
