import { expect, test } from "@playwright/test";

import { expectNoUnhandledSidecarRequests, openApp } from "./support/sidecarMock";

test.describe("E2E-UI judge", () => {
  test("TC-02.1 TC-02.4 runs a prompt, renders the verdict, and fetches prior verdicts", async ({ page }) => {
    const sidecar = await openApp(page);

    await expect(page.getByRole("heading", { name: "Judge", level: 2 })).toBeVisible();
    await expect(page.getByText("AIAR: connected on example-host")).toBeVisible();

    await page.getByLabel("Prompt").fill("What does Errorta do with AIAR?");
    await page.getByRole("button", { name: "Run" }).click();

    await expect(page.getByText("AIAR says the demo corpus")).toBeVisible();
    await expect(page.getByLabel("Verdict rating: pass")).toBeVisible();
    await expect(page.getByText("Compared to your last run")).toBeVisible();
    await expect(page.getByText("Prior verdict found")).toBeVisible();
    await expect(page.getByText("Pass rate (all-time)")).toBeVisible();

    expect(sidecar.judgeRequests).toHaveLength(1);
    expect(sidecar.judgeRequests[0]).toMatchObject({
      prompt: "What does Errorta do with AIAR?",
    });
    expectNoUnhandledSidecarRequests(sidecar);
  });

  test("TC-02.8 supports keyboard movement across Judge tabs", async ({ page }) => {
    const sidecar = await openApp(page);

    const metricsTab = page.getByRole("tab", { name: "Metrics" });
    await metricsTab.focus();
    await page.keyboard.press("ArrowRight");

    const replayTab = page.getByRole("tab", { name: "Replay" });
    await expect(replayTab).toBeFocused();
    await expect(replayTab).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("judge-replay")).toBeVisible();
    await expect(page.getByTestId("judge-replay-hint")).toBeVisible();
    expectNoUnhandledSidecarRequests(sidecar);
  });
});
