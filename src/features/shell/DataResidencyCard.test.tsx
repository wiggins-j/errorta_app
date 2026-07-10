// F-INFRA-12 Phase B Slice 9 — DataResidencyCard tests.
//
// Mocks two seams:
//   1. The residency API client (getResidency / putResidency / probeResidency)
//      via vi.mock("../../lib/api/residency").
//   2. The dynamic `new Function("s","return import(...)")` trick the
//      component uses to load @tauri-apps/api/core (so we can inject a
//      controllable `invoke` mock without a real Tauri runtime). Same pattern
//      used by AppShellSettings and StepResidency.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/residency", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api/residency")>(
    "../../lib/api/residency",
  );
  return {
    ...actual,
    getResidency: vi.fn(),
    putResidency: vi.fn(),
    probeResidency: vi.fn(),
  };
});

import {
  getResidency,
  probeResidency,
  putResidency,
  type ResidencyGetResponse,
} from "../../lib/api/residency";
vi.mock("../../lib/sidecarPort", () => ({
  loadTauriInvoke: vi.fn(),
  isTauriRuntime: () => true,
}));

import { DataResidencyCard } from "./DataResidencyCard";
import { loadTauriInvoke } from "../../lib/sidecarPort";

const mockedGet = getResidency as unknown as ReturnType<typeof vi.fn>;
const mockedPut = putResidency as unknown as ReturnType<typeof vi.fn>;
const mockedProbe = probeResidency as unknown as ReturnType<typeof vi.fn>;
const mockedLoadInvoke = loadTauriInvoke as unknown as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// F132: DataResidencyCard now resolves Tauri `invoke` through the shared
// `loadTauriInvoke` (src/lib/sidecarPort.ts). The test controls it by mocking
// that helper (see the vi.mock below) instead of swapping globalThis.Function.
// ---------------------------------------------------------------------------

interface FakeInvoke {
  invoke: ReturnType<typeof vi.fn>;
}

function installFakeInvoke(): FakeInvoke {
  const invoke = vi.fn();
  mockedLoadInvoke.mockResolvedValue(invoke);
  return { invoke };
}

function localResponse(): ResidencyGetResponse {
  return {
    config: { mode: "local" },
    tunnel_state: { kind: "down" },
    remote_healthz: null,
  };
}

function sshResponse(): ResidencyGetResponse {
  return {
    config: {
      mode: "ssh-remote",
      ssh_host: "example-host",
      ssh_port: 22,
      ssh_key_path: "~/.ssh/id_ed25519",
      ssh_username: null,
      remote_sidecar_port: 8770,
    },
    tunnel_state: { kind: "up" },
    remote_healthz: null,
  };
}

let fake: FakeInvoke;

beforeEach(() => {
  mockedGet.mockReset();
  mockedPut.mockReset();
  mockedProbe.mockReset();
  mockedLoadInvoke.mockReset();
  fake = installFakeInvoke();
  // Default: data_residency_mode returns a sensible snapshot so the 5s poll
  // doesn't surface weird tunnel state in unrelated assertions.
  fake.invoke.mockImplementation(async (cmd: string) => {
    if (cmd === "data_residency_mode") {
      return {
        mode: "local",
        ssh_host: null,
        remote_sidecar_port: null,
        local_tunnel_port: null,
        tunnel_state: { kind: "down" },
      };
    }
    throw new Error(`unmocked invoke: ${cmd}`);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("DataResidencyCard", () => {
  it("renders local mode by default and hides SSH/Cloud fields", async () => {
    mockedGet.mockResolvedValue(localResponse());
    render(<DataResidencyCard />);
    const localRadio = await screen.findByTestId("residency-mode-local");
    expect(localRadio).toBeChecked();
    expect(screen.queryByTestId("ssh-fields")).toBeNull();
    expect(screen.queryByTestId("cloud-fields")).toBeNull();
    // The migration warning must NOT appear in the initial local state.
    expect(screen.queryByTestId("migration-warning")).toBeNull();
  });

  it("normalizes legacy string tunnel_state responses from the sidecar", async () => {
    mockedGet.mockResolvedValue({
      config: { mode: "local" },
      tunnel_state: "down",
      remote_healthz: null,
    } as unknown as ResidencyGetResponse);

    render(<DataResidencyCard />);

    await screen.findByTestId("residency-mode-local");
    const badge = screen.getByTestId("tunnel-badge");
    expect(badge).toHaveAttribute("data-kind", "down");
    expect(badge).toHaveTextContent("Local");
  });

  it("reveals SSH fields when ssh-remote is selected and Apply calls invoke + PUT", async () => {
    mockedGet.mockResolvedValue(localResponse());
    mockedPut.mockResolvedValue(sshResponse());
    fake.invoke.mockImplementation(async (cmd: string, args?: unknown) => {
      if (cmd === "data_residency_mode") {
        return {
          mode: "local",
          ssh_host: null,
          remote_sidecar_port: null,
          local_tunnel_port: null,
          tunnel_state: { kind: "down" },
        };
      }
      if (cmd === "set_data_residency") {
        return {
          mode: "ssh-remote",
          ssh_host: (args as { newState?: { ssh_host?: string } })?.newState
            ?.ssh_host ?? null,
          remote_sidecar_port: 8770,
          local_tunnel_port: 18770,
          tunnel_state: { kind: "up" },
        };
      }
      throw new Error(`unmocked invoke: ${cmd}`);
    });

    const user = userEvent.setup();
    render(<DataResidencyCard />);
    await screen.findByTestId("residency-mode-local");

    await user.click(screen.getByTestId("residency-mode-ssh-remote"));
    expect(screen.getByTestId("ssh-fields")).toBeInTheDocument();

    await user.type(screen.getByTestId("ssh-host"), "example-host");
    await user.click(screen.getByTestId("residency-apply"));

    await waitFor(() => {
      const calls = fake.invoke.mock.calls.filter(
        (c) => c[0] === "set_data_residency",
      );
      expect(calls).toHaveLength(1);
      const args = calls[0][1] as { newState: Record<string, unknown> };
      expect(args.newState.mode).toBe("ssh-remote");
      expect(args.newState.ssh_host).toBe("example-host");
      expect(args.newState.ssh_port).toBe(22);
      expect(args.newState.remote_sidecar_port).toBe(8770);
    });
    await waitFor(() => {
      expect(mockedPut).toHaveBeenCalledTimes(1);
      const body = mockedPut.mock.calls[0][0] as Record<string, unknown>;
      expect(body.mode).toBe("ssh-remote");
      expect(body.ssh_host).toBe("example-host");
    });

    expect(await screen.findByTestId("residency-apply-ok")).toBeInTheDocument();
  });

  it("renders the SshProbeReport on test_ssh_connection success", async () => {
    mockedGet.mockResolvedValue(localResponse());
    fake.invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "data_residency_mode") {
        return {
          mode: "local",
          ssh_host: null,
          remote_sidecar_port: null,
          local_tunnel_port: null,
          tunnel_state: { kind: "down" },
        };
      }
      if (cmd === "test_ssh_connection") {
        return {
          uname: "Linux x86_64",
          sidecar_present: true,
          sidecar_version: "0.5.0",
          raw_stdout: "Linux x86_64\n0.5.0",
        };
      }
      throw new Error(`unmocked invoke: ${cmd}`);
    });

    const user = userEvent.setup();
    render(<DataResidencyCard />);
    await screen.findByTestId("residency-mode-local");
    await user.click(screen.getByTestId("residency-mode-ssh-remote"));
    await user.type(screen.getByTestId("ssh-host"), "example-host");
    await user.click(screen.getByTestId("ssh-test"));

    const ok = await screen.findByTestId("ssh-test-ok");
    expect(ok).toHaveTextContent("Reachable");
    expect(ok).toHaveTextContent("Linux x86_64");
    expect(ok).toHaveTextContent("present");
  });

  it("renders the error and keeps Apply enabled when test_ssh_connection rejects", async () => {
    mockedGet.mockResolvedValue(localResponse());
    fake.invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "data_residency_mode") {
        return {
          mode: "local",
          ssh_host: null,
          remote_sidecar_port: null,
          local_tunnel_port: null,
          tunnel_state: { kind: "down" },
        };
      }
      if (cmd === "test_ssh_connection") {
        throw new Error("permission denied");
      }
      throw new Error(`unmocked invoke: ${cmd}`);
    });

    const user = userEvent.setup();
    render(<DataResidencyCard />);
    await screen.findByTestId("residency-mode-local");
    await user.click(screen.getByTestId("residency-mode-ssh-remote"));
    await user.type(screen.getByTestId("ssh-host"), "example-host");
    await user.click(screen.getByTestId("ssh-test"));

    const err = await screen.findByTestId("test-error");
    expect(err).toHaveTextContent("permission denied");

    // Apply stays enabled so the user can retry after fixing the issue.
    expect(screen.getByTestId("residency-apply")).not.toBeDisabled();

    // Badge should enter the error state. The badge lives in the card header.
    const badge = screen.getByTestId("tunnel-badge");
    expect(badge).toHaveAttribute("data-kind", "error");
  });

  it("shows cloud as planned but disables selection until auth ships", async () => {
    mockedGet.mockResolvedValue(localResponse());

    const user = userEvent.setup();
    render(<DataResidencyCard />);
    const cloud = await screen.findByTestId("residency-mode-cloud");

    expect(cloud).toBeDisabled();
    await user.click(cloud);

    expect(screen.queryByTestId("cloud-fields")).toBeNull();
    expect(mockedProbe).not.toHaveBeenCalled();
  });

  it("surfaces the migration warning when switching away from ssh-remote", async () => {
    mockedGet.mockResolvedValue(sshResponse());
    const user = userEvent.setup();
    render(<DataResidencyCard />);
    await screen.findByTestId("residency-mode-ssh-remote");

    // Currently on ssh-remote → no warning yet (mode unchanged).
    expect(screen.queryByTestId("migration-warning")).toBeNull();

    await user.click(screen.getByTestId("residency-mode-local"));
    expect(screen.getByTestId("migration-warning")).toBeInTheDocument();
  });

  it("does NOT surface the migration warning when staying on the same mode", async () => {
    mockedGet.mockResolvedValue(sshResponse());
    const user = userEvent.setup();
    render(<DataResidencyCard />);
    await screen.findByTestId("residency-mode-ssh-remote");

    // Tap the same radio — no change.
    await user.click(screen.getByTestId("residency-mode-ssh-remote"));
    expect(screen.queryByTestId("migration-warning")).toBeNull();
  });
});
