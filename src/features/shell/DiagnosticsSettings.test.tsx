import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/settings", () => ({
  getSettings: vi.fn(),
  setLogLevel: vi.fn(),
}));

vi.mock("../../lib/api/diagnosticsLog", () => ({
  tailLog: vi.fn(),
  streamLog: vi.fn(),
}));

import { streamLog, tailLog } from "../../lib/api/diagnosticsLog";
import { getSettings, setLogLevel } from "../../lib/api/settings";
import { DiagnosticsSettings } from "./DiagnosticsSettings";

const mockedGetSettings = getSettings as unknown as ReturnType<typeof vi.fn>;
const mockedSetLogLevel = setLogLevel as unknown as ReturnType<typeof vi.fn>;
const mockedTailLog = tailLog as unknown as ReturnType<typeof vi.fn>;
const mockedStreamLog = streamLog as unknown as ReturnType<typeof vi.fn>;

class FakeEventSource {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();

  emit(data: string) {
    this.onmessage?.({ data } as MessageEvent<string>);
  }
}

let source: FakeEventSource;

beforeEach(() => {
  mockedGetSettings.mockReset();
  mockedSetLogLevel.mockReset();
  mockedTailLog.mockReset();
  mockedStreamLog.mockReset();
  source = new FakeEventSource();
  mockedGetSettings.mockResolvedValue({ log_level: "info" });
  mockedSetLogLevel.mockImplementation(async (level: "info" | "debug") => ({
    log_level: level,
  }));
  mockedTailLog.mockResolvedValue([]);
  mockedStreamLog.mockResolvedValue(source);
});

describe("DiagnosticsSettings", () => {
  it("loads persisted log level into the debug checkbox", async () => {
    mockedGetSettings.mockResolvedValue({ log_level: "debug" });

    render(<DiagnosticsSettings />);

    expect(await screen.findByLabelText("Debug logging")).toBeChecked();
  });

  it("persists debug toggle changes", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsSettings />);

    const checkbox = await screen.findByLabelText("Debug logging");
    await user.click(checkbox);

    await waitFor(() => expect(mockedSetLogLevel).toHaveBeenCalledWith("debug"));
    expect(checkbox).toBeChecked();
  });

  it("reverts the checkbox and shows retry on save failure", async () => {
    mockedSetLogLevel.mockRejectedValue(new Error("disk full"));
    const user = userEvent.setup();
    render(<DiagnosticsSettings />);

    const checkbox = await screen.findByLabelText("Debug logging");
    await user.click(checkbox);

    expect(await screen.findByRole("alert")).toHaveTextContent("disk full");
    expect(checkbox).not.toBeChecked();

    mockedGetSettings.mockResolvedValue({ log_level: "info" });
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mockedGetSettings).toHaveBeenCalledTimes(2));
  });

  it("opens live log with a tail snapshot and appends stream events", async () => {
    mockedTailLog.mockResolvedValue(["tail-one"]);
    const user = userEvent.setup();
    render(<DiagnosticsSettings />);

    await screen.findByLabelText("Debug logging");
    await user.click(screen.getByText("Live log"));

    const log = await screen.findByRole("log", { name: "Live sidecar log" });
    expect(log).toHaveTextContent("tail-one");
    await waitFor(() => expect(mockedStreamLog).toHaveBeenCalledTimes(1));

    act(() => source.emit("stream-two"));
    expect(log).toHaveTextContent("stream-two");
  });

  it("keeps receiving lines while scroll is paused", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsSettings />);

    await screen.findByLabelText("Debug logging");
    await user.click(screen.getByText("Live log"));
    await waitFor(() => expect(mockedStreamLog).toHaveBeenCalledTimes(1));

    const log = await screen.findByTestId("diagnostics-log-pre");
    Object.defineProperty(log, "scrollHeight", { configurable: true, value: 500 });
    log.scrollTop = 0;

    await user.click(screen.getByRole("button", { name: "Pause scroll" }));
    act(() => source.emit("paused-line"));

    expect(log).toHaveTextContent("paused-line");
    expect(log.scrollTop).toBe(0);
  });

  it("copies only the last 200 visible log lines", async () => {
    mockedTailLog.mockResolvedValue(Array.from({ length: 205 }, (_, i) => `line-${i}`));
    const user = userEvent.setup();
    render(<DiagnosticsSettings />);

    await screen.findByLabelText("Debug logging");
    await user.click(screen.getByText("Live log"));
    await screen.findByText(/line-204/);
    await user.click(screen.getByRole("button", { name: "Copy last 200" }));

    await screen.findByText("Copied 200 lines.");
    const copied = await navigator.clipboard.readText();
    expect(copied).not.toContain("line-4\n");
    expect(copied).toContain("line-5");
    expect(copied).toContain("line-204");
  });

  it("closes the EventSource when the live log disclosure collapses", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsSettings />);

    await screen.findByLabelText("Debug logging");
    const summary = screen.getByText("Live log");
    await user.click(summary);
    await waitFor(() => expect(mockedStreamLog).toHaveBeenCalledTimes(1));
    await user.click(summary);

    await waitFor(() => expect(source.close).toHaveBeenCalledTimes(1));
  });
});
