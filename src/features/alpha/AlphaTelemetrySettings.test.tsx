import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AlphaTelemetrySettings from "./AlphaTelemetrySettings";
import * as api from "../../lib/api/alpha";
import { BackendReadyProvider } from "../../lib/backendReady";
import { expectNoA11yViolations } from "../council/a11y-helpers";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// The component only fetches once the backend is ready, so wrap every render.
function renderReady() {
  return render(
    <BackendReadyProvider ready={true}>
      <AlphaTelemetrySettings />
    </BackendReadyProvider>,
  );
}

function stubConsent(gateEnabled: boolean, extrasEnabled = true) {
  vi.spyOn(api, "getTelemetryConsent").mockResolvedValue({ gateEnabled, extrasEnabled });
}

describe("AlphaTelemetrySettings", () => {
  it("renders nothing when the gate is off (production)", async () => {
    stubConsent(false);
    const { container } = renderReady();
    await waitFor(() => expect(api.getTelemetryConsent).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("shows the honest copy + opt-out when the gate is on", async () => {
    stubConsent(true, true);
    renderReady();
    expect(await screen.findByRole("heading", { name: "Alpha telemetry" })).toBeInTheDocument();
    expect(screen.getByText(/never see your documents/i)).toBeInTheDocument();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
  });

  it("toggling the extras calls the backend", async () => {
    stubConsent(true, true);
    const setSpy = vi.spyOn(api, "setTelemetryExtras").mockResolvedValue(false);
    renderReady();
    const box = await screen.findByRole("checkbox");
    fireEvent.click(box);
    await waitFor(() => expect(setSpy).toHaveBeenCalledWith(false));
  });

  it("the inspector shows the pending counts (names only)", async () => {
    stubConsent(true, true);
    vi.spyOn(api, "getTelemetryInspect").mockResolvedValue({
      extrasEnabled: true,
      floor: { launches: 2 },
      queue: [{ event: "feature_used", name: "judge_run", count: 3 }],
      queueLen: 1,
    });
    renderReady();
    fireEvent.click(await screen.findByRole("button", { name: /see exactly what we send/i }));
    expect(await screen.findByText("launches")).toBeInTheDocument();
    expect(screen.getByText("judge_run")).toBeInTheDocument();
    expect(screen.getByText(/× 3/)).toBeInTheDocument();
  });

  it("has no serious/critical a11y violations", async () => {
    stubConsent(true, true);
    const { container } = renderReady();
    await screen.findByRole("heading", { name: "Alpha telemetry" });
    await expectNoA11yViolations(container);
  });
});
