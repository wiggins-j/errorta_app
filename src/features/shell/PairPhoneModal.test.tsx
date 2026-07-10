import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("qrcode", () => ({
  default: {
    toCanvas: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock("../../lib/api/mobileConnector", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api/mobileConnector")>(
      "../../lib/api/mobileConnector",
    );
  return {
    ...actual,
    startPairing: vi.fn(),
    getPairingStatus: vi.fn(),
  };
});

import QRCode from "qrcode";
import * as mobileApi from "../../lib/api/mobileConnector";
import PairPhoneModal from "./PairPhoneModal";

const mockedApi = mobileApi as unknown as {
  startPairing: ReturnType<typeof vi.fn>;
  getPairingStatus: ReturnType<typeof vi.fn>;
};

const mockedQr = QRCode as unknown as {
  toCanvas: ReturnType<typeof vi.fn>;
};

const PAYLOAD = {
  schema: "errorta.mobile_pairing.v1" as const,
  connector_id: "mobconn_1",
  desktop_name: "Errorta Desktop",
  hosts: [{ kind: "lan", host: "192.0.2.14" }],
  port: 8788,
  tls_cert_sha256: "abc123",
  pairing_token: "PAIRING_TOKEN_SECRET",
  expires_at: "2099-06-14T20:00:00Z",
};

beforeEach(() => {
  mockedApi.startPairing.mockReset();
  mockedApi.getPairingStatus.mockReset();
  mockedQr.toCanvas.mockClear();
  mockedQr.toCanvas.mockResolvedValue(undefined);
  mockedApi.startPairing.mockResolvedValue({
    sessionId: "mobpair_1",
    expiresAt: "2099-06-14T20:00:00Z",
    pin: "418207",
    pairingPayload: PAYLOAD,
  });
  mockedApi.getPairingStatus.mockResolvedValue({
    sessionId: "mobpair_1",
    state: "awaiting_approval",
    requiresPin: true,
    pinAttemptsRemaining: 5,
    expiresAt: "2099-06-14T20:00:00Z",
    deviceDraft: {
      display_name: "Test iPhone",
      platform: "ios",
      public_key_fingerprint: "pkfp",
      submitted_at: "2099-06-14T20:00:01Z",
    },
  });
});

describe("PairPhoneModal", () => {
  it("renders the PIN separately and encodes only the pairing payload as QR", async () => {
    render(
      <PairPhoneModal open onClose={vi.fn()} onPaired={vi.fn()} />,
    );

    await screen.findByText("418207");
    await waitFor(() => expect(mockedQr.toCanvas).toHaveBeenCalled());

    const encoded = mockedQr.toCanvas.mock.calls[0][1] as string;
    expect(encoded).toBe(JSON.stringify(PAYLOAD));
    expect(encoded).not.toContain("418207");
  });

  it("shows the connected device while waiting for PIN entry", async () => {
    render(
      <PairPhoneModal open onClose={vi.fn()} onPaired={vi.fn()} />,
    );

    await screen.findByText(/Connected: Test iPhone/i);
    expect(screen.getAllByText("Enter PIN on phone").length).toBeGreaterThan(0);
  });

  it("notifies and closes after consumed status", async () => {
    const onClose = vi.fn();
    const onPaired = vi.fn();
    mockedApi.getPairingStatus.mockResolvedValue({
      sessionId: "mobpair_1",
      state: "consumed",
      requiresPin: true,
      pinAttemptsRemaining: 5,
      expiresAt: "2099-06-14T20:00:00Z",
      deviceDraft: null,
    });

    render(<PairPhoneModal open onClose={onClose} onPaired={onPaired} />);

    await waitFor(() => expect(onPaired).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });
});
