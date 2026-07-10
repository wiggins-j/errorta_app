import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/mobileConnector", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api/mobileConnector")>(
      "../../lib/api/mobileConnector",
    );
  return {
    ...actual,
    getMobileConnectorSettings: vi.fn(),
    putMobileConnectorSettings: vi.fn(),
    getLanAddresses: vi.fn(),
    updateDeviceCapabilities: vi.fn(),
    revokeDevice: vi.fn(),
  };
});

vi.mock("./PairPhoneModal", () => ({
  default: ({ open }: { open: boolean }) =>
    open ? <div data-testid="pair-phone-modal" /> : null,
}));

import * as mobileApi from "../../lib/api/mobileConnector";
import MobileConnectorSettings from "./MobileConnectorSettings";

const mocked = mobileApi as unknown as {
  getMobileConnectorSettings: ReturnType<typeof vi.fn>;
  putMobileConnectorSettings: ReturnType<typeof vi.fn>;
  getLanAddresses: ReturnType<typeof vi.fn>;
  updateDeviceCapabilities: ReturnType<typeof vi.fn>;
  revokeDevice: ReturnType<typeof vi.fn>;
};

const DEVICE = {
  deviceId: "mob_dev_1",
  displayName: "Test iPhone",
  platform: "ios",
  publicKeyFingerprint: "pkfp1234",
  pairedAt: "2026-06-14T20:00:00Z",
  lastSeenAt: null,
  lastIpLabel: null,
  capabilities: {
    read_runs: true,
    start_runs: false,
    send_messages: false,
    cancel_runs: false,
    read_coding_projects: false,
    read_coding_activity: false,
    read_coding_diffs: false,
    send_coding_messages: false,
    start_coding_runs: false,
    resume_coding_runs: false,
    cancel_coding_runs: false,
    edit_coding_plan: false,
    accept_coding_merge_back: false,
    approve_low_risk: false,
    approve_remote_egress: false,
    approve_mcp_elicitation: false,
    approve_code_exec: false,
    approve_code_write: false,
    approve_merge_back: false,
  },
  revokedAt: null,
};

function settings(overrides = {}) {
  return {
    enabled: false,
    bindMode: "disabled",
    explicitHost: null,
    lanBindAddress: null,
    port: 8788,
    requireTls: true,
    pairingEnabled: false,
    pairingPinRequired: false,
    allowedNetworks: ["lan"],
    maxEventStreams: 4,
    deviceCount: 0,
    devices: [],
    lanListener: null,
    ...overrides,
  };
}

beforeEach(() => {
  mocked.getMobileConnectorSettings.mockReset();
  mocked.putMobileConnectorSettings.mockReset();
  mocked.getLanAddresses.mockReset();
  mocked.updateDeviceCapabilities.mockReset();
  mocked.revokeDevice.mockReset();
  mocked.getMobileConnectorSettings.mockResolvedValue(settings());
  mocked.getLanAddresses.mockResolvedValue([
    { address: "192.0.2.14", interface: "default", isDefault: true },
  ]);
});

describe("MobileConnectorSettings", () => {
  it("enables LAN pairing with the selected address", async () => {
    mocked.putMobileConnectorSettings.mockResolvedValue(
      settings({
        enabled: true,
        bindMode: "lan",
        lanBindAddress: "192.0.2.14",
        pairingEnabled: true,
        pairingPinRequired: true,
      }),
    );

    render(<MobileConnectorSettings />);
    await screen.findByText(/Off by default/i);

    fireEvent.click(screen.getByText("Enable"));

    await waitFor(() =>
      expect(mocked.putMobileConnectorSettings).toHaveBeenCalledWith({
        enabled: true,
        bindMode: "lan",
        lanBindAddress: "192.0.2.14",
        port: 8788,
        requireTls: true,
        pairingEnabled: true,
        allowedNetworks: ["lan"],
      }),
    );
  });

  it("opens the pairing modal when the connector is enabled", async () => {
    mocked.getMobileConnectorSettings.mockResolvedValue(
      settings({
        enabled: true,
        bindMode: "lan",
        lanBindAddress: "192.0.2.14",
        pairingEnabled: true,
        pairingPinRequired: true,
      }),
    );
    render(<MobileConnectorSettings />);
    await screen.findByText(/Listening on/i);

    fireEvent.click(screen.getByText("Pair a phone"));

    expect(screen.getByTestId("pair-phone-modal")).toBeInTheDocument();
  });

  it("patches device capabilities and never renders raw pairing secrets", async () => {
    mocked.getMobileConnectorSettings.mockResolvedValue(
      settings({
        enabled: true,
        bindMode: "lan",
        lanBindAddress: "192.0.2.14",
        pairingEnabled: true,
        pairingPinRequired: true,
        devices: [DEVICE],
      }),
    );
    mocked.updateDeviceCapabilities.mockResolvedValue({
      ...DEVICE,
      capabilities: { ...DEVICE.capabilities, start_runs: true },
    });

    render(<MobileConnectorSettings />);
    await screen.findByText("Test iPhone");
    fireEvent.click(screen.getByLabelText("Start runs"));

    await waitFor(() =>
      expect(mocked.updateDeviceCapabilities).toHaveBeenCalledWith(
        "mob_dev_1",
        { start_runs: true },
      ),
    );
    expect(document.body.innerHTML).not.toContain("pairing_token");
    expect(document.body.innerHTML).not.toContain("session_token");
  });

  it("surfaces Coding Team capabilities separately from Council run capabilities", async () => {
    mocked.getMobileConnectorSettings.mockResolvedValue(
      settings({
        enabled: true,
        bindMode: "lan",
        lanBindAddress: "192.0.2.14",
        pairingEnabled: true,
        pairingPinRequired: true,
        devices: [DEVICE],
      }),
    );
    mocked.updateDeviceCapabilities.mockResolvedValue({
      ...DEVICE,
      capabilities: { ...DEVICE.capabilities, read_coding_projects: true },
    });

    render(<MobileConnectorSettings />);
    await screen.findByText("Test iPhone");
    fireEvent.click(screen.getByLabelText("Read Coding Team projects"));

    await waitFor(() =>
      expect(mocked.updateDeviceCapabilities).toHaveBeenCalledWith(
        "mob_dev_1",
        { read_coding_projects: true },
      ),
    );
  });
});
