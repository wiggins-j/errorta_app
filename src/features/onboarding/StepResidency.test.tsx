// F-INFRA-12 Phase B Slice 10 — StepResidency tests.
//
// Mocks the residency API client + the Tauri invoke shim, following the same
// pattern used by DataResidencyCard.test.tsx so the two test suites share a
// mental model.
//
// Scenarios:
//   1. Local default — clicking Apply advances + writes the seen sentinel
//      with mode=local; no SSH/Cloud fields appear.
//   2. SSH-remote happy path — host filled, probe returns
//      sidecar_present=true, Apply fires invoke('set_data_residency') AND
//      PUT /residency, both with mode=ssh-remote.
//   3. SSH-remote skip path — fields left blank, Skip surfaces the warning
//      AND persists mode=local (not whatever the user partially typed).
//   4. SSH-remote bootstrap path — SSH reaches a host without a sidecar and
//      Apply delegates install/start/tunnel to the Rust shell.
//   5. Cloud is disabled during first-run onboarding.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/residency", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api/residency")>(
    "../../lib/api/residency",
  );
  return {
    ...actual,
    putResidency: vi.fn(),
  };
});

// F132: StepResidency now resolves Tauri `invoke` through the shared
// `loadTauriInvoke` (src/lib/sidecarPort.ts) instead of a per-file `new
// Function` shim, so the test controls it by mocking that helper.
vi.mock("../../lib/sidecarPort", () => ({
  loadTauriInvoke: vi.fn(),
  isTauriRuntime: () => true,
}));

import { putResidency } from "../../lib/api/residency";
import { loadTauriInvoke } from "../../lib/sidecarPort";
import StepResidency from "./StepResidency";

const mockedPut = putResidency as unknown as ReturnType<typeof vi.fn>;
const mockedLoadInvoke = loadTauriInvoke as unknown as ReturnType<typeof vi.fn>;

// Track `localStorage.setItem` calls by overriding `Storage.prototype.setItem`
// directly. We can't use `vi.spyOn` on happy-dom's localStorage because the
// property descriptors don't expose setItem as a configurable own property,
// and `vi.spyOn` walks the property descriptor. We also can't read
// `localStorage.getItem(...)` back across the FakeFunction install boundary.
// Recording calls into a module-scope array via a wrapper sidesteps both.

// happy-dom v20 does not expose a real localStorage by default — install a
// minimal in-memory shim on globalThis exactly the way App.test.tsx does.
// The component calls `localStorage.setItem(...)` directly; the shim makes
// that succeed (otherwise the markSeen call lands in its try/catch and the
// assertions can't verify the sentinel).
function installLocalStorageShim(): void {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.has(key) ? (store.get(key) as string) : null;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string) {
      store.delete(key);
    },
    setItem(key: string, value: string) {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: shim,
  });
}

function seenWasSet(): boolean {
  return localStorage.getItem("errorta.onboarding.residency.seen") === "1";
}

// ---------------------------------------------------------------------------
// Tauri invoke stub — replaces globalThis.Function so the dynamic
// `new Function(...)` import inside StepResidency resolves to a controllable
// mock. Same trick used by DataResidencyCard.test.tsx.
// ---------------------------------------------------------------------------

interface FakeInvoke {
  invoke: ReturnType<typeof vi.fn>;
}

function installFakeInvoke(): FakeInvoke {
  const invoke = vi.fn();
  mockedLoadInvoke.mockResolvedValue(invoke);
  return { invoke };
}

let fake: FakeInvoke;

beforeEach(() => {
  mockedPut.mockReset();
  mockedLoadInvoke.mockReset();
  installLocalStorageShim();
  fake = installFakeInvoke();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("StepResidency", () => {
  it("local default — Apply advances and persists mode=local", async () => {
    mockedPut.mockResolvedValue({
      config: { mode: "local" },
      tunnel_state: { kind: "down" },
      remote_healthz: null,
    });
    fake.invoke.mockResolvedValue({
      mode: "local",
      ssh_host: null,
      remote_sidecar_port: null,
      local_tunnel_port: null,
      tunnel_state: { kind: "down" },
    });
    const onAdvance = vi.fn();
    const onSkip = vi.fn();

    const user = userEvent.setup();
    render(<StepResidency onAdvance={onAdvance} onSkip={onSkip} />);

    // Local is selected by default; SSH/Cloud panels are hidden.
    expect(screen.getByTestId("onboarding-residency-local")).toBeChecked();
    expect(screen.queryByTestId("onboarding-ssh-fields")).toBeNull();
    expect(screen.queryByTestId("onboarding-cloud-fields")).toBeNull();

    await user.click(screen.getByTestId("onboarding-residency-apply"));

    await waitFor(() => {
      expect(mockedPut).toHaveBeenCalledTimes(1);
    });
    const body = mockedPut.mock.calls[0][0] as Record<string, unknown>;
    expect(body.mode).toBe("local");

    expect(onAdvance).toHaveBeenCalledTimes(1);
    expect(onSkip).not.toHaveBeenCalled();
    expect(seenWasSet()).toBe(true);
  });

  it("SSH-remote happy path — invoke + PUT both fire with mode=ssh-remote", async () => {
    mockedPut.mockResolvedValue({
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
    });
    fake.invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "test_ssh_connection") {
        return {
          uname: "Linux x86_64",
          sidecar_present: true,
          sidecar_version: "0.5.0",
          raw_stdout: "",
        };
      }
      if (cmd === "set_data_residency") {
        return {
          mode: "ssh-remote",
          ssh_host: "example-host",
          remote_sidecar_port: 8770,
          local_tunnel_port: 18770,
          tunnel_state: { kind: "up" },
        };
      }
      throw new Error(`unmocked invoke: ${cmd}`);
    });

    const onAdvance = vi.fn();
    const onSkip = vi.fn();
    const user = userEvent.setup();
    render(<StepResidency onAdvance={onAdvance} onSkip={onSkip} />);

    await user.click(screen.getByTestId("onboarding-residency-ssh"));
    expect(screen.getByTestId("onboarding-ssh-fields")).toBeInTheDocument();

    await user.type(screen.getByTestId("onboarding-ssh-host"), "example-host");
    await user.click(screen.getByTestId("onboarding-ssh-test"));

    await waitFor(() => {
      const probeMsg = screen.getByTestId("onboarding-residency-probe-msg");
      expect(probeMsg).toHaveAttribute("role", "status");
    });

    await user.click(screen.getByTestId("onboarding-residency-apply"));

    await waitFor(() => {
      const calls = fake.invoke.mock.calls.filter(
        (c) => c[0] === "set_data_residency",
      );
      expect(calls).toHaveLength(1);
      const args = calls[0][1] as { newState: Record<string, unknown> };
      expect(args.newState.mode).toBe("ssh-remote");
      expect(args.newState.ssh_host).toBe("example-host");
    });
    await waitFor(() => {
      expect(mockedPut).toHaveBeenCalledTimes(1);
      const body = mockedPut.mock.calls[0][0] as Record<string, unknown>;
      expect(body.mode).toBe("ssh-remote");
      expect(body.ssh_host).toBe("example-host");
      expect(body.local_tunnel_port).toBe(18770);
    });

    expect(onAdvance).toHaveBeenCalledTimes(1);
    expect(seenWasSet()).toBe(true);
  });

  it("SSH-remote bootstrap path — Apply can install a missing remote sidecar", async () => {
    mockedPut.mockResolvedValue({
      config: {
        mode: "ssh-remote",
        ssh_host: "example-host",
        ssh_port: 22,
        remote_sidecar_port: 8770,
        local_tunnel_port: 18771,
      },
      tunnel_state: { kind: "up" },
      remote_healthz: null,
    });
    fake.invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "test_ssh_connection") {
        return {
          uname: "Linux x86_64",
          sidecar_present: false,
          sidecar_version: null,
          raw_stdout: "",
        };
      }
      if (cmd === "set_data_residency") {
        return {
          mode: "ssh-remote",
          ssh_host: "example-host",
          remote_sidecar_port: 8770,
          local_tunnel_port: 18771,
          tunnel_state: { kind: "up" },
        };
      }
      throw new Error(`unmocked invoke: ${cmd}`);
    });

    const onAdvance = vi.fn();
    const onSkip = vi.fn();
    const user = userEvent.setup();
    render(<StepResidency onAdvance={onAdvance} onSkip={onSkip} />);

    await user.click(screen.getByTestId("onboarding-residency-ssh"));
    await user.type(screen.getByTestId("onboarding-ssh-host"), "example-host");
    await user.click(screen.getByTestId("onboarding-ssh-test"));

    await waitFor(() => {
      expect(screen.getByTestId("onboarding-residency-probe-msg")).toHaveTextContent(
        /apply will install/i,
      );
    });

    await user.click(screen.getByTestId("onboarding-residency-apply"));

    await waitFor(() => {
      expect(mockedPut).toHaveBeenCalledTimes(1);
    });
    const body = mockedPut.mock.calls[0][0] as Record<string, unknown>;
    expect(body.mode).toBe("ssh-remote");
    expect(body.local_tunnel_port).toBe(18771);
    expect(onAdvance).toHaveBeenCalledTimes(1);
  });

  it("SSH-remote skip path — warning shown and mode=local persisted", async () => {
    mockedPut.mockResolvedValue({
      config: { mode: "local" },
      tunnel_state: { kind: "down" },
      remote_healthz: null,
    });
    fake.invoke.mockResolvedValue({
      mode: "local",
      ssh_host: null,
      remote_sidecar_port: null,
      local_tunnel_port: null,
      tunnel_state: { kind: "down" },
    });
    const onAdvance = vi.fn();
    const onSkip = vi.fn();
    const user = userEvent.setup();
    render(<StepResidency onAdvance={onAdvance} onSkip={onSkip} />);

    await user.click(screen.getByTestId("onboarding-residency-ssh"));
    expect(screen.getByTestId("onboarding-ssh-fields")).toBeInTheDocument();
    // Fields blank → Apply is disabled (Test connection wasn't run).
    expect(screen.getByTestId("onboarding-residency-apply")).toBeDisabled();

    await user.click(screen.getByTestId("onboarding-residency-skip-step"));

    await waitFor(() => {
      expect(
        screen.getByTestId("onboarding-residency-skip-warning"),
      ).toBeInTheDocument();
    });

    // The skip path persists local mode regardless of what the user typed.
    await waitFor(() => {
      expect(mockedPut).toHaveBeenCalledTimes(1);
    });
    const body = mockedPut.mock.calls[0][0] as Record<string, unknown>;
    expect(body.mode).toBe("local");

    expect(onAdvance).toHaveBeenCalledTimes(1);
    expect(onSkip).not.toHaveBeenCalled();
    expect(seenWasSet()).toBe(true);
  });

  it("Cloud is disabled during first-run onboarding", async () => {
    const onAdvance = vi.fn();
    const onSkip = vi.fn();
    render(<StepResidency onAdvance={onAdvance} onSkip={onSkip} />);

    expect(screen.getByTestId("onboarding-residency-cloud")).toBeDisabled();
    expect(screen.queryByTestId("onboarding-cloud-fields")).toBeNull();
    expect(mockedPut).not.toHaveBeenCalled();
    expect(onAdvance).not.toHaveBeenCalled();
  });
});
