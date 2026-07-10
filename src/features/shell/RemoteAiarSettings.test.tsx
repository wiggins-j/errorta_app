import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../../lib/api/settings", () => ({
  getRemoteAiarSettings: vi.fn(),
  putRemoteAiarSettings: vi.fn(),
  reconnectRemoteAiarTunnel: vi.fn(),
}));

import * as settingsApi from "../../lib/api/settings";
import RemoteAiarSettings from "./RemoteAiarSettings";

const mocked = settingsApi as unknown as {
  getRemoteAiarSettings: ReturnType<typeof vi.fn>;
  putRemoteAiarSettings: ReturnType<typeof vi.fn>;
  reconnectRemoteAiarTunnel: ReturnType<typeof vi.fn>;
};

const MANAGED = {
  configured: true,
  managed: true,
  base_url: "",
  tunnel_port: null,
  timeout_s: 60,
  verify: true,
  token_configured: true,
  token_preview: "…1234",
  updated_at: "2026-06-18T22:00:00Z",
  ssh_host: "example-host",
  remote_host: "127.0.0.1",
  remote_port: 8766,
  ssh_port: null,
  ssh_username: null,
  ssh_key_path: null,
  auto_start: true,
  tunnel: {
    ssh_host: "example-host",
    remote_host: "127.0.0.1",
    remote_port: 8766,
    local_port: 54999,
    state: "up" as const,
    last_error: "",
    since: "2026-06-18T22:00:01Z",
  },
};

const EMPTY = {
  configured: false,
  base_url: "",
  tunnel_port: null,
  timeout_s: 60,
  verify: true,
  token_configured: false,
  token_preview: null,
  updated_at: null,
};

const CONFIGURED = {
  configured: true,
  base_url: "http://127.0.0.1:8766",
  tunnel_port: 8766,
  timeout_s: 42,
  verify: false,
  token_configured: true,
  token_preview: "…1234",
  updated_at: "2026-06-17T22:00:00Z",
};

beforeEach(() => {
  mocked.getRemoteAiarSettings.mockReset();
  mocked.putRemoteAiarSettings.mockReset();
  mocked.getRemoteAiarSettings.mockResolvedValue(EMPTY);
});

afterEach(() => {
  cleanup();
});

describe("RemoteAiarSettings", () => {
  it("loads the masked remote AIAR state", async () => {
    mocked.getRemoteAiarSettings.mockResolvedValue(CONFIGURED);

    render(<RemoteAiarSettings />);

    await waitFor(() => screen.getByText("Watchdog endpoint"));
    expect(screen.getByText("…1234")).toBeInTheDocument();
    expect(screen.getByDisplayValue("http://127.0.0.1:8766")).toBeInTheDocument();
  });

  it("saves and never re-shows the raw token after the masked response", async () => {
    mocked.putRemoteAiarSettings.mockResolvedValue(CONFIGURED);
    render(<RemoteAiarSettings />);
    await waitFor(() => screen.getByText("Watchdog endpoint"));

    fireEvent.change(screen.getByPlaceholderText("http://127.0.0.1:8766"), {
      target: { value: "http://127.0.0.1:8766" },
    });
    fireEvent.change(screen.getByPlaceholderText("(secret)"), {
      target: { value: "DO-NOT-LEAK-token-1234" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() =>
      expect(mocked.putRemoteAiarSettings).toHaveBeenCalledWith({
        base_url: "http://127.0.0.1:8766",
        tunnel_port: 8766,
        ssh_host: "",
        token: "DO-NOT-LEAK-token-1234",
        timeout_s: 60,
        verify: true,
      }),
    );
    await waitFor(() => screen.getByText("…1234"));
    expect(document.body.innerHTML).not.toContain("DO-NOT-LEAK");
  });

  it("requires a token for first-time save", async () => {
    render(<RemoteAiarSettings />);
    await waitFor(() => screen.getByText("Watchdog endpoint"));

    fireEvent.click(screen.getByText("Save"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Bearer token is required.",
    );
    expect(mocked.putRemoteAiarSettings).not.toHaveBeenCalled();
  });

  it("clears the remote AIAR settings", async () => {
    mocked.getRemoteAiarSettings.mockResolvedValue(CONFIGURED);
    mocked.putRemoteAiarSettings.mockResolvedValue(EMPTY);
    render(<RemoteAiarSettings />);
    await waitFor(() => screen.getByText("…1234"));

    fireEvent.click(screen.getByText("Clear"));

    await waitFor(() =>
      expect(mocked.putRemoteAiarSettings).toHaveBeenCalledWith({ clear: true }),
    );
    await waitFor(() => screen.getByText("not configured"));
  });

  it("loads managed mode and shows the live tunnel state", async () => {
    mocked.getRemoteAiarSettings.mockResolvedValue(MANAGED);
    render(<RemoteAiarSettings />);
    await waitFor(() => screen.getByText("Watchdog endpoint"));
    expect(screen.getByDisplayValue("example-host")).toBeInTheDocument();
    expect(screen.getByTestId("tunnel-state")).toHaveTextContent("Connected");
    expect(screen.getByTestId("tunnel-state")).toHaveTextContent("127.0.0.1:54999");
  });

  it("saves managed mode with the ssh host + remote port", async () => {
    mocked.getRemoteAiarSettings.mockResolvedValue(MANAGED);
    mocked.putRemoteAiarSettings.mockResolvedValue(MANAGED);
    render(<RemoteAiarSettings />);
    await waitFor(() => screen.getByText("Watchdog endpoint"));
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(mocked.putRemoteAiarSettings).toHaveBeenCalledWith(
        expect.objectContaining({ ssh_host: "example-host", remote_port: 8766, auto_start: true }),
      ),
    );
  });

  it("requires an ssh host before saving managed mode", async () => {
    mocked.getRemoteAiarSettings.mockResolvedValue({ ...MANAGED, ssh_host: null, managed: false });
    render(<RemoteAiarSettings />);
    await waitFor(() => screen.getByText("Watchdog endpoint"));
    // Switch to managed but leave the host blank.
    fireEvent.change(screen.getByLabelText("Connection mode"), {
      target: { value: "managed" },
    });
    fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByRole("alert")).toHaveTextContent("SSH host");
    expect(mocked.putRemoteAiarSettings).not.toHaveBeenCalled();
  });

  it("kicks the tunnel via Reconnect", async () => {
    mocked.getRemoteAiarSettings.mockResolvedValue(MANAGED);
    mocked.reconnectRemoteAiarTunnel.mockResolvedValue(MANAGED);
    render(<RemoteAiarSettings />);
    await waitFor(() => screen.getByText("Watchdog endpoint"));
    fireEvent.click(screen.getByText("Reconnect"));
    await waitFor(() => expect(mocked.reconnectRemoteAiarTunnel).toHaveBeenCalled());
  });
});
