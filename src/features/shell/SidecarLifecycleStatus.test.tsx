import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("../../lib/api/diagnostics", () => ({
  getSidecarLifecycle: vi.fn(),
}));

import { getSidecarLifecycle } from "../../lib/api/diagnostics";
import SidecarLifecycleStatus from "./SidecarLifecycleStatus";

const _api = getSidecarLifecycle as unknown as ReturnType<typeof vi.fn>;

function lifecycle(signature: string) {
  return {
    component: "sidecar",
    pid: 123,
    sidecar_version: "0.1.0-alpha.0",
    residency_mode: "local",
    config_signature: signature,
    signature_inputs: {},
  };
}

afterEach(() => {
  cleanup();
  _api.mockReset();
});

describe("SidecarLifecycleStatus", () => {
  it("shows running status with version and residency mode", async () => {
    _api.mockResolvedValue(lifecycle("cfg-aaa"));
    render(<SidecarLifecycleStatus />);
    await waitFor(() => screen.getByText(/Sidecar running/));
    expect(screen.getByText(/v0\.1\.0-alpha\.0/)).toBeInTheDocument();
    expect(screen.getByText("cfg-aaa")).toBeInTheDocument();
  });

  it("recommends a restart when the config signature changes after boot", async () => {
    // First poll: cfg-aaa (captured as boot). Second poll: cfg-bbb (changed).
    _api
      .mockResolvedValueOnce(lifecycle("cfg-aaa"))
      .mockResolvedValue(lifecycle("cfg-bbb"));
    render(<SidecarLifecycleStatus pollMs={5} />);
    await waitFor(() =>
      expect(screen.getByText(/restart Errorta to apply it/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/config changed/i)).toBeInTheDocument();
  });

  it("shows unreachable when the sidecar cannot be polled", async () => {
    _api.mockRejectedValue(new Error("down"));
    render(<SidecarLifecycleStatus />);
    await waitFor(() => screen.getByText(/Backend offline/));
  });
});
