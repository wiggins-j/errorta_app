// A11Y-EXTEND — accessibility assertions for RefreshDiffModal: Escape closes,
// focus trap cycles, and opener focus is restored on close.
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import RefreshDiffModal from "./RefreshDiffModal";
import type { RefreshDiffResponse } from "./types";

function makeDiff(
  overrides: Partial<RefreshDiffResponse> = {},
): RefreshDiffResponse {
  return {
    corpus: "default",
    added: [
      { original_path: "/docs/added-a.pdf" },
      { original_path: "/docs/added-b.pdf" },
    ],
    removed: [{ original_path: "/docs/removed.pdf" }],
    updated: [],
    snapshot_at: "2026-06-08T12:34:56Z",
    partial: false,
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RefreshDiffModal a11y", () => {
  it("aria-labelledby points to the dialog h3 title", () => {
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={makeDiff()}
        loading={false}
        error={null}
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-labelledby")).toBe("refresh-diff-title");
    const title = document.getElementById("refresh-diff-title");
    expect(title?.tagName).toBe("H3");
    expect(title?.textContent).toContain("Preview changes for default");
  });

  it("Escape calls onClose and preventDefaults the event", () => {
    const onClose = vi.fn();
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={onClose}
        corpus="default"
        diff={makeDiff()}
        loading={false}
        error={null}
      />,
    );
    const evt = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      document.dispatchEvent(evt);
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(evt.defaultPrevented).toBe(true);
  });

  it("focus-trap: Tab on last focusable wraps to first; Shift+Tab on first wraps to last", () => {
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={makeDiff()}
        loading={false}
        error={null}
      />,
    );
    const dialog = screen.getByRole("dialog");
    const focusables = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => !el.hasAttribute("disabled"));
    expect(focusables.length).toBeGreaterThan(2);
    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    last.focus();
    expect(document.activeElement).toBe(last);
    const tabEvt = new KeyboardEvent("keydown", {
      key: "Tab",
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      document.dispatchEvent(tabEvt);
    });
    expect(document.activeElement).toBe(first);

    first.focus();
    const shiftTab = new KeyboardEvent("keydown", {
      key: "Tab",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      document.dispatchEvent(shiftTab);
    });
    expect(document.activeElement).toBe(last);
  });

  it("restores focus to the opener element on unmount", () => {
    const opener = document.createElement("button");
    opener.textContent = "Open refresh modal";
    document.body.appendChild(opener);
    opener.focus();
    expect(document.activeElement).toBe(opener);

    const { unmount } = render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={makeDiff()}
        loading={false}
        error={null}
      />,
    );
    const closeBtn = screen.getByRole("button", { name: /close/i });
    closeBtn.focus();
    expect(document.activeElement).toBe(closeBtn);

    unmount();
    expect(document.activeElement).toBe(opener);
    document.body.removeChild(opener);
  });
});
