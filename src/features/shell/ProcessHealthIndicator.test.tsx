import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/shell", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api/shell")>(
      "../../lib/api/shell",
    );
  return {
    ...actual,
    processes: vi.fn(),
  };
});

import * as shellApi from "../../lib/api/shell";
import { ProcessHealthIndicator } from "./ProcessHealthIndicator";

const mockedShellApi = shellApi as unknown as {
  processes: ReturnType<typeof vi.fn>;
};

describe("ProcessHealthIndicator", () => {
  beforeEach(() => {
    mockedShellApi.processes.mockReset();
    mockedShellApi.processes.mockResolvedValue({});
  });

  afterEach(() => {
    cleanup();
  });

  it("treats a missing processes array as empty instead of throwing", async () => {
    render(<ProcessHealthIndicator intervalMs={0} />);

    expect(await screen.findByText("no managed processes")).toBeInTheDocument();
  });

  it("drops malformed process entries and renders valid ones", async () => {
    mockedShellApi.processes.mockResolvedValueOnce({
      processes: [
        { pid: "bad" },
        {
          pid: 123,
          name: "errorta-sidecar",
          role: "sidecar",
          status: "running",
          cpu_percent: 0,
          rss_bytes: 2048,
          started_at: null,
        },
      ],
    });

    render(<ProcessHealthIndicator intervalMs={0} />);

    expect(await screen.findByText("errorta-sidecar")).toBeInTheDocument();
    expect(screen.queryByText("#bad")).toBeNull();
  });
});
