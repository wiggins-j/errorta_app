import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WatchStatusIndicator } from "./WatchStatusIndicator";
import type { WatchStatus } from "./types";

function baseStatus(overrides: Partial<WatchStatus> = {}): WatchStatus {
  return {
    corpus: "demo",
    watching: true,
    alive: true,
    paused: false,
    last_scan_ok: true,
    last_scan_at: new Date(Date.now() - 30_000).toISOString(),
    last_heartbeat: new Date(Date.now() - 10_000).toISOString(),
    heartbeat_age_seconds: 10,
    stale: false,
    ...overrides,
  };
}

describe("WatchStatusIndicator", () => {
  it("renders 'Not watching' when status is null", () => {
    render(<WatchStatusIndicator status={null} />);
    expect(screen.getByText("Not watching")).toBeInTheDocument();
  });

  it("renders 'Not watching' when status.watching is false", () => {
    render(<WatchStatusIndicator status={baseStatus({ watching: false })} />);
    expect(screen.getByText("Not watching")).toBeInTheDocument();
  });

  it("shows healthy label and last-scan time without a stale banner", () => {
    render(<WatchStatusIndicator status={baseStatus()} />);
    expect(screen.getByText("Healthy")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /force rescan/i })).not.toBeInTheDocument();
  });

  it("renders stale banner with Force rescan button when stale and onForceRescan provided", () => {
    const onForceRescan = vi.fn();
    render(
      <WatchStatusIndicator
        status={baseStatus({ stale: true, heartbeat_age_seconds: 142 })}
        onForceRescan={onForceRescan}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Watcher stale (142s since last heartbeat)");
    expect(screen.getByRole("button", { name: /force rescan/i })).toBeInTheDocument();
  });

  it("calls onForceRescan with the corpus name when Force rescan is clicked", async () => {
    const user = userEvent.setup();
    const onForceRescan = vi.fn();
    render(
      <WatchStatusIndicator
        status={baseStatus({ corpus: "aerospace", stale: true })}
        onForceRescan={onForceRescan}
      />,
    );
    await user.click(screen.getByRole("button", { name: /force rescan/i }));
    expect(onForceRescan).toHaveBeenCalledTimes(1);
    expect(onForceRescan).toHaveBeenCalledWith("aerospace");
  });

  it("omits Force rescan button when stale but no onForceRescan handler is provided", () => {
    render(<WatchStatusIndicator status={baseStatus({ stale: true })} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /force rescan/i })).not.toBeInTheDocument();
  });

  it("shows 'Scan failed' label when last_scan_ok is false", () => {
    render(
      <WatchStatusIndicator
        status={baseStatus({ last_scan_ok: false, last_error: "permission denied" })}
      />,
    );
    expect(screen.getByText("Scan failed")).toBeInTheDocument();
    expect(screen.getByText(/permission denied/)).toBeInTheDocument();
  });

  it("shows 'Paused' label when paused is true", () => {
    render(<WatchStatusIndicator status={baseStatus({ paused: true })} />);
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });
});
