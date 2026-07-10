import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import BriefStatusPanel from "./BriefStatusPanel";
import type { LiveStatus } from "../../lib/api/briefs";

vi.mock("../../lib/api/briefs", () => ({
  statusBrief: vi.fn(),
}));

vi.mock("../../lib/sidecarPort", () => ({
  getSidecarBase: vi.fn().mockResolvedValue("http://127.0.0.1:8770"),
}));

import { statusBrief } from "../../lib/api/briefs";

const statusBriefMock = vi.mocked(statusBrief);

function makeLiveStatus(overrides: Partial<LiveStatus> = {}): LiveStatus {
  return {
    brief_id: "b1",
    run_id: "run-1",
    state: "RUNNING",
    per_source: [
      {
        name: "arxiv",
        state: "running",
        docs_collected: 12,
        docs_refused: 1,
        page_or_offset: 100,
      },
    ],
    compliance_refusals: [],
    failures: [],
    ingested_count: 12,
    ...overrides,
  };
}

// Mock EventSource implementation we can inspect from tests.
interface MockES {
  url: string;
  onmessage: ((ev: { data: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close: ReturnType<typeof vi.fn>;
}

let lastES: MockES | null = null;
let allES: MockES[] = [];

class MockEventSource {
  url: string;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  close = vi.fn();
  constructor(url: string) {
    this.url = url;
    lastES = this as unknown as MockES;
    allES.push(this as unknown as MockES);
  }
}

beforeEach(() => {
  statusBriefMock.mockReset();
  statusBriefMock.mockResolvedValue(makeLiveStatus());
  lastES = null;
  allES = [];
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("BriefStatusPanel", () => {
  it("renders loading state when snapshot is null", () => {
    statusBriefMock.mockReturnValue(new Promise(() => {})); // never resolves
    render(<BriefStatusPanel briefId="b1" state="DRAFT" />);
    expect(screen.getByText(/loading status/i)).toBeInTheDocument();
  });

  it("renders per_source table and collapsible sections after snapshot loads", async () => {
    render(<BriefStatusPanel briefId="b1" state="DRAFT" />);
    await flush();
    expect(screen.getByText("arxiv")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText(/compliance refusals/i)).toBeInTheDocument();
    expect(screen.getByText(/^failures /i)).toBeInTheDocument();
    expect(screen.getByText(/raw snapshot/i)).toBeInTheDocument();
  });

  it("does NOT open an EventSource for non-active states (DRAFT)", async () => {
    render(<BriefStatusPanel briefId="b1" state="DRAFT" />);
    await flush();
    expect(lastES).toBeNull();
  });

  it("opens an EventSource when state is RUNNING", async () => {
    render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    expect(lastES).not.toBeNull();
    expect(lastES!.url).toContain("/briefs/b1/status");
  });

  it("opens an EventSource when state is PAUSED", async () => {
    render(<BriefStatusPanel briefId="b1" state="PAUSED" />);
    await flush();
    expect(lastES).not.toBeNull();
  });

  it("message handler parses JSON and updates the snapshot in state", async () => {
    render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    const es = lastES!;
    act(() => {
      es.onmessage?.({
        data: JSON.stringify(makeLiveStatus({ ingested_count: 99 })),
      });
    });
    expect(screen.getByText(/ingested 99/i)).toBeInTheDocument();
  });

  it("ignores non-JSON keepalives without throwing or closing the stream", async () => {
    render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    const es = lastES!;
    act(() => {
      es.onmessage?.({ data: "ping" });
    });
    expect(es.close).not.toHaveBeenCalled();
  });

  it("calls es.close() when the streamed state transitions to a terminal state", async () => {
    render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    const es = lastES!;
    act(() => {
      es.onmessage?.({
        data: JSON.stringify(makeLiveStatus({ state: "COMPLETED" })),
      });
    });
    expect(es.close).toHaveBeenCalledTimes(1);
  });

  it("calls es.close() exactly once when state transitions to FAILED", async () => {
    render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    const es = lastES!;
    act(() => {
      es.onmessage?.({
        data: JSON.stringify(makeLiveStatus({ state: "FAILED" })),
      });
    });
    expect(es.close).toHaveBeenCalledTimes(1);
  });

  it("calls es.close() exactly once when state transitions to ARCHIVED", async () => {
    render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    const es = lastES!;
    act(() => {
      es.onmessage?.({
        data: JSON.stringify(makeLiveStatus({ state: "ARCHIVED" })),
      });
    });
    expect(es.close).toHaveBeenCalledTimes(1);
  });

  it("sets streamError when the onerror handler is invoked", async () => {
    render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    const es = lastES!;
    act(() => {
      es.onerror?.(new Event("error"));
    });
    expect(screen.getByText(/event stream interrupted/i)).toBeInTheDocument();
  });

  it("calls es.close() on unmount", async () => {
    const { unmount } = render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    const es = lastES!;
    expect(es.close).not.toHaveBeenCalled();
    unmount();
    expect(es.close).toHaveBeenCalled();
  });

  it("re-opens the EventSource when briefId changes", async () => {
    const { rerender } = render(<BriefStatusPanel briefId="b1" state="RUNNING" />);
    await flush();
    expect(allES.length).toBe(1);
    const firstES = allES[0];
    rerender(<BriefStatusPanel briefId="b2" state="RUNNING" />);
    await flush();
    expect(allES.length).toBe(2);
    // The first stream is torn down by the cleanup callback.
    expect(firstES.close).toHaveBeenCalled();
    expect(allES[1].url).toContain("/briefs/b2/status");
  });

  it("renders streamError when the initial snapshot fetch rejects", async () => {
    statusBriefMock.mockRejectedValue(new Error("boom"));
    render(<BriefStatusPanel briefId="b1" state="DRAFT" />);
    await flush();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });
});
