// F120-03 (UI) — the provider Test wording: a logged-out CLI reads "Not logged
// in" + the login remediation, never a bare `claude_cli_failed: exit 1:`.
import { cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StatusBadge, useTestConnection } from "./providerRows";
import type { TestConnectionResult } from "../../lib/api/providerKeys";

afterEach(cleanup);

describe("StatusBadge", () => {
  it("renders logged_out with the remediation, not a bare exit string", () => {
    render(
      <StatusBadge
        status={{
          state: "logged_out",
          detail: "claude_cli_error: API Error: 401 … Please run /login",
          remediation: "Run the login command, then Test again.",
          latencyMs: 12,
        }}
      />,
    );
    expect(screen.getByText(/Not logged in/)).toBeInTheDocument();
    expect(screen.getByText(/Run the login command/)).toBeInTheDocument();
    expect(screen.queryByText(/claude_cli_failed: exit/)).not.toBeInTheDocument();
  });

  it("still shows ok + fail states", () => {
    const { rerender } = render(
      <StatusBadge status={{ state: "ok", detail: "ready", latencyMs: 5 }} />,
    );
    expect(screen.getByText(/✓ Connected/)).toBeInTheDocument();
    rerender(<StatusBadge status={{ state: "fail", detail: "boom", latencyMs: 5 }} />);
    expect(screen.getByText(/Failed: boom/)).toBeInTheDocument();
  });

  it("renders rate_limited as an amber connected-but-busy state, not a failure", () => {
    render(
      <StatusBadge
        status={{
          state: "rate_limited",
          detail: "cursor_cli_rate_limited: usage limit",
          remediation: "Wait and retry, or use a different model.",
          latencyMs: 8,
        }}
      />,
    );
    expect(screen.getByText(/Connected — rate-limited/)).toBeInTheDocument();
    expect(screen.queryByText(/Failed:/)).not.toBeInTheDocument();
  });
});

describe("useTestConnection", () => {
  it("maps a logged_out result into the logged_out status", async () => {
    const result: TestConnectionResult = {
      ok: false,
      detail: "API Error: 401 … Please run /login",
      latency_ms: 9,
      state: "logged_out",
      remediation: "Run the login command for this provider in Settings → Providers, then retry.",
    };
    const { result: hook } = renderHook(() =>
      useTestConnection(async () => result),
    );
    hook.current[1](); // trigger
    await waitFor(() => expect(hook.current[0].state).toBe("logged_out"));
    const status = hook.current[0];
    expect(status.state).toBe("logged_out");
    if (status.state === "logged_out") {
      expect(status.remediation).toMatch(/login/i);
    }
  });

  it("maps an ok result into the ok status", async () => {
    const result: TestConnectionResult = { ok: true, detail: "ready", latency_ms: 5 };
    const { result: hook } = renderHook(() => useTestConnection(async () => result));
    hook.current[1]();
    await waitFor(() => expect(hook.current[0].state).toBe("ok"));
  });
});
