import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/shell", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api/shell")>(
      "../../lib/api/shell",
    );
  return {
    ...actual,
    status: vi.fn(),
    sidecarPort: vi.fn(),
  };
});

vi.mock("./ProcessHealthIndicator", () => ({
  ProcessHealthIndicator: () => <div data-testid="process-health" />,
}));
vi.mock("./OllamaHostField", () => ({
  OllamaHostField: () => <div data-testid="ollama-host" />,
}));
vi.mock("./DiagnosticsExport", () => ({
  DiagnosticsExport: () => <div data-testid="diagnostics-export" />,
}));
vi.mock("./SidecarLifecycleStatus", () => ({
  default: () => <div data-testid="sidecar-lifecycle" />,
}));
vi.mock("./DiagnosticsSettings", () => ({
  DiagnosticsSettings: () => <div data-testid="diagnostics-settings" />,
}));
vi.mock("./DataResidencyCard", () => ({
  DataResidencyCard: () => <div data-testid="data-residency" />,
}));
vi.mock("./UpdatesCard", () => ({
  UpdatesCard: () => <div data-testid="updates-card" />,
}));
vi.mock("./ProviderKeysSettings", () => ({
  default: () => <div data-testid="provider-keys" />,
}));

import * as shellApi from "../../lib/api/shell";
import AppShellSettings from "./AppShellSettings";

const mockedShellApi = shellApi as unknown as {
  status: ReturnType<typeof vi.fn>;
  sidecarPort: ReturnType<typeof vi.fn>;
};

describe("AppShellSettings", () => {
  beforeEach(() => {
    mockedShellApi.status.mockReset();
    mockedShellApi.sidecarPort.mockReset();
    mockedShellApi.status.mockResolvedValue({});
    mockedShellApi.sidecarPort.mockResolvedValue({});
  });

  afterEach(() => {
    cleanup();
  });

  it("renders fallback Shell status when the sidecar returns a partial payload", async () => {
    render(<AppShellSettings />);

    await waitFor(() => {
      expect(screen.getByText(/not measured yet/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/8770/)).toBeInTheDocument();
    // F134 — Ollama status moved to its own Settings section; Shell no longer
    // renders an Ollama card.
    expect(screen.queryByText(/unreachable: status unavailable/i)).not.toBeInTheDocument();
    expect(screen.getByTestId("process-health")).toBeInTheDocument();
    expect(screen.getByTestId("sidecar-lifecycle")).toBeInTheDocument();
    expect(screen.getByTestId("diagnostics-export")).toBeInTheDocument();
    expect(screen.queryByTestId("provider-keys")).not.toBeInTheDocument();
    expect(screen.queryByTestId("system-tray")).not.toBeInTheDocument();
    expect(screen.queryByTestId("diagnostics-settings")).not.toBeInTheDocument();
  });

  it("surfaces shell status load failures inline", async () => {
    mockedShellApi.status.mockRejectedValueOnce(new Error("status route down"));

    render(<AppShellSettings />);

    await waitFor(() => {
      expect(screen.getByText(/status route down/i)).toBeInTheDocument();
    });
  });
});
