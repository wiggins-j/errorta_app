import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider, useToast } from "../toast";

function Trigger({ message, details }: { message: string; details?: string }) {
  const toast = useToast();
  return (
    <button type="button" onClick={() => toast.show({ message, details })}>
      fire
    </button>
  );
}

describe("toast", () => {
  afterEach(() => {
    // Ensure any test that opted into fake timers restores real timers
    // before the next test runs (RTL cleanup runs via setup file).
    vi.useRealTimers();
  });

  it("portal mounts to document.body when toast is shown", async () => {
    render(
      <ToastProvider>
        <Trigger message="Boom" details="stack trace here" />
      </ToastProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: /fire/i }));
    // Portal mounts under document.body. role=status (aria-live) picks it up.
    const toasts = await screen.findAllByRole("status");
    const messageNode = toasts.find((n) => n.textContent?.includes("Boom"));
    expect(messageNode).toBeDefined();
    // The portal node is a direct child of document.body, not the RTL container.
    expect(messageNode?.closest(".errorta-toast")?.parentElement).toBe(
      document.body,
    );
  });

  it("Copy action writes message + details to navigator.clipboard.writeText", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const originalClipboard = (navigator as Navigator).clipboard;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    try {
      render(
        <ToastProvider>
          <Trigger message="Boom" details="details xyz" />
        </ToastProvider>,
      );
      await userEvent.click(screen.getByRole("button", { name: /fire/i }));
      const copyBtn = await screen.findByRole("button", {
        name: /copy error details/i,
      });
      await userEvent.click(copyBtn);
      expect(writeText).toHaveBeenCalledTimes(1);
      expect(writeText.mock.calls[0][0]).toMatch(/Boom/);
      expect(writeText.mock.calls[0][0]).toMatch(/details xyz/);
    } finally {
      if (originalClipboard) {
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: originalClipboard,
        });
      }
    }
  });

  it("auto-dismisses after 8 seconds", () => {
    vi.useFakeTimers();
    try {
      render(
        <ToastProvider>
          <Trigger message="Auto bye" />
        </ToastProvider>,
      );
      // fireEvent is synchronous — pairs cleanly with fake timers without
      // needing a userEvent advanceTimers handshake.
      fireEvent.click(screen.getByRole("button", { name: /fire/i }));
      expect(
        screen.getAllByRole("status").some((n) =>
          n.textContent?.includes("Auto bye"),
        ),
      ).toBe(true);
      act(() => {
        vi.advanceTimersByTime(8000);
      });
      expect(
        screen
          .queryAllByRole("status")
          .some((n) => n.textContent?.includes("Auto bye")),
      ).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("manual dismiss button removes the toast immediately", async () => {
    render(
      <ToastProvider>
        <Trigger message="Manual close" />
      </ToastProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: /fire/i }));
    expect(
      screen
        .getAllByRole("status")
        .some((n) => n.textContent?.includes("Manual close")),
    ).toBe(true);
    const dismissBtn = await screen.findByRole("button", { name: /dismiss/i });
    await userEvent.click(dismissBtn);
    expect(
      screen
        .queryAllByRole("status")
        .some((n) => n.textContent?.includes("Manual close")),
    ).toBe(false);
  });

  it("Copy handler does NOT issue any fetch call (local-only)", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const originalClipboard = (navigator as Navigator).clipboard;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    try {
      render(
        <ToastProvider>
          <Trigger message="No-net" details="payload" />
        </ToastProvider>,
      );
      await userEvent.click(screen.getByRole("button", { name: /fire/i }));
      const copyBtn = await screen.findByRole("button", {
        name: /copy error details/i,
      });
      await userEvent.click(copyBtn);
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      fetchSpy.mockRestore();
      if (originalClipboard) {
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: originalClipboard,
        });
      }
    }
  });
});
