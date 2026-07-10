import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BackendReadyProvider } from "../../lib/backendReady";

// The cards fetch on mount; stub them to isolate the Settings layout + gating.
vi.mock("../shell/ProviderKeysSettings", () => ({
  default: () => <button>provider-keys-action</button>,
}));
vi.mock("../shell/DiagnosticsSettings", () => ({
  DiagnosticsSettings: () => <div data-testid="diagnostics" />,
}));
vi.mock("../shell/MobileConnectorSettings", () => ({
  default: () => <button>mobile-connector-action</button>,
}));
vi.mock("../shell/RemoteAiarSettings", () => ({
  default: () => <div data-testid="remote-aiar" />,
}));
vi.mock("./ConnectedAppsSettings", () => ({
  default: () => <button>connected-apps-action</button>,
}));
vi.mock("../aiar/AiarConnectionCard", () => ({
  default: () => <div data-testid="aiar-connection-card" />,
}));
vi.mock("../hardware/index", () => ({
  default: () => <div data-testid="hardware-feature" />,
}));
vi.mock("../../lib/api/settings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api/settings")>()),
  getToolsSettings: vi.fn(),
  putToolsSettings: vi.fn(),
}));

import * as settingsApi from "../../lib/api/settings";
import Settings from "./index";

const _settingsApi = settingsApi as unknown as {
  getToolsSettings: ReturnType<typeof vi.fn>;
  putToolsSettings: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  _settingsApi.getToolsSettings.mockReset();
  _settingsApi.putToolsSettings.mockReset();
  _settingsApi.getToolsSettings.mockResolvedValue({
    searxng_url: "",
    configured: false,
    env_configured: false,
  });
  _settingsApi.putToolsSettings.mockResolvedValue({
    searxng_url: "https://search.example.com",
    configured: true,
    env_configured: false,
  });
});

function renderSettings(ready: boolean) {
  return render(
    <BackendReadyProvider ready={ready}>
      <Settings />
    </BackendReadyProvider>,
  );
}

describe("Settings tab", () => {
  it("owns the configuration cards removed from Shell", () => {
    renderSettings(true);
    expect(screen.getByRole("heading", { name: "Provider keys" })).toBeInTheDocument();
    expect(screen.getByText("provider-keys-action")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Debug logging" })).toBeInTheDocument();
    expect(screen.getByTestId("diagnostics")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "AIAR connection" })).toBeInTheDocument();
    expect(screen.getByTestId("aiar-connection-card")).toBeInTheDocument();
    // F113: Hardware moved out of the sidebar into Settings.
    expect(screen.getByTestId("hardware-feature")).toBeInTheDocument();
  });

  it("shows a 'What is AIAR?' help affordance with a guide link", () => {
    renderSettings(true);
    expect(screen.getByTestId("settings-aiar-help")).toBeInTheDocument();
    expect(screen.getByText("What is AIAR?")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Read the AIAR guide/i });
    expect(link.getAttribute("href")).toContain("docs/AIAR_SETUP.md");
  });

  it("hosts the Mobile connector card (moved out of Shell)", () => {
    renderSettings(true);
    expect(screen.getByRole("heading", { name: "Mobile connector" })).toBeInTheDocument();
    expect(screen.getByText("mobile-connector-action")).toBeInTheDocument();
  });

  it("hosts the Connected apps card", () => {
    renderSettings(true);
    expect(screen.getByRole("heading", { name: "Connected apps" })).toBeInTheDocument();
    expect(screen.getByText("connected-apps-action")).toBeInTheDocument();
  });

  it("hosts the global Tools card for SearXNG settings", async () => {
    renderSettings(true);
    expect(screen.getByRole("heading", { name: "Tools" })).toBeInTheDocument();
    const input = await screen.findByTestId("settings-searxng-url");
    fireEvent.change(input, { target: { value: "https://search.example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(_settingsApi.putToolsSettings).toHaveBeenCalledWith({
        searxng_url: "https://search.example.com",
      }),
    );
  });

  it("greys out backend-dependent cards while the backend is not ready", () => {
    const { container } = renderSettings(false);
    // The waiting hint appears on each gated card.
    expect(screen.getAllByText(/Available once the local backend is ready/i).length).toBeGreaterThan(0);
    // The gated wrapper disables interaction.
    const gated = container.querySelectorAll(".settings-card-gated");
    expect(gated.length).toBeGreaterThanOrEqual(3); // provider keys, mobile, logging
    gated.forEach((el) => expect(el).toHaveAttribute("aria-disabled", "true"));
  });

});
