import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/auth", () => ({
  approvePairing: vi.fn(),
  denyPairing: vi.fn(),
  listPairingRequests: vi.fn(),
  listTokens: vi.fn(),
  revokeToken: vi.fn(),
}));

import {
  approvePairing,
  denyPairing,
  listPairingRequests,
  listTokens,
  revokeToken,
} from "../../lib/api/auth";
import ConnectedAppsSettings from "./ConnectedAppsSettings";

const mocked = {
  approvePairing: vi.mocked(approvePairing),
  denyPairing: vi.mocked(denyPairing),
  listPairingRequests: vi.mocked(listPairingRequests),
  listTokens: vi.mocked(listTokens),
  revokeToken: vi.mocked(revokeToken),
};

const pair = {
  sessionId: "pair_123",
  status: "pending" as const,
  appSlug: "com.demo.app",
  appName: "Demo App",
  requestedCorpora: ["legal-cases", "notes"],
  requestedScopes: ["prompt", "meta"],
  approvedCorpora: [],
  approvedScopes: [],
  createdAt: "2026-06-23T12:00:00Z",
  expiresAt: "2026-06-23T12:05:00Z",
  issuedAt: null,
  tokenId: null,
};

const token = {
  id: "tok_123",
  appSlug: "com.demo.app",
  appName: "Demo App",
  corpora: ["legal-cases"],
  scopes: ["prompt", "meta"],
  issuedAt: "2026-06-23T12:01:00Z",
  lastUsedAt: null,
};

beforeEach(() => {
  mocked.approvePairing.mockReset();
  mocked.denyPairing.mockReset();
  mocked.listPairingRequests.mockReset();
  mocked.listTokens.mockReset();
  mocked.revokeToken.mockReset();
  mocked.approvePairing.mockResolvedValue({ ...pair, status: "accepted" });
  mocked.denyPairing.mockResolvedValue({ ...pair, status: "denied" });
  mocked.revokeToken.mockResolvedValue(undefined);
});

describe("ConnectedAppsSettings", () => {
  it("approves the selected corpus and scopes", async () => {
    mocked.listPairingRequests
      .mockResolvedValueOnce([pair])
      .mockResolvedValueOnce([{ ...pair, status: "accepted" }]);
    mocked.listTokens.mockResolvedValue([]);
    render(<ConnectedAppsSettings />);

    expect(await screen.findByText("Demo App")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("notes"));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(mocked.approvePairing).toHaveBeenCalledWith("pair_123", {
        corpora: ["legal-cases"],
        scopes: ["prompt", "meta"],
      });
    });
  });

  it("denies a pending request", async () => {
    mocked.listPairingRequests.mockResolvedValue([pair]);
    mocked.listTokens.mockResolvedValue([]);
    render(<ConnectedAppsSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "Deny" }));

    await waitFor(() => expect(mocked.denyPairing).toHaveBeenCalledWith("pair_123"));
  });

  it("revokes an issued token", async () => {
    mocked.listPairingRequests.mockResolvedValue([]);
    mocked.listTokens.mockResolvedValueOnce([token]).mockResolvedValueOnce([]);
    render(<ConnectedAppsSettings />);

    expect(await screen.findByText("Scopes: prompt, meta")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => expect(mocked.revokeToken).toHaveBeenCalledWith("tok_123"));
  });

  it("shows empty states", async () => {
    mocked.listPairingRequests.mockResolvedValue([]);
    mocked.listTokens.mockResolvedValue([]);
    render(<ConnectedAppsSettings />);

    expect(await screen.findByText("No pending requests.")).toBeInTheDocument();
    expect(screen.getByText("No connected apps.")).toBeInTheDocument();
  });
});
