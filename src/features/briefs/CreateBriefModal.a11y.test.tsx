// A11Y-EXTEND — accessibility assertions for CreateBriefModal: Escape closes,
// focus trap cycles Tab/Shift+Tab, and opener focus is restored on unmount.
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CreateBriefModal from "./CreateBriefModal";

vi.mock("../../lib/api/briefs", () => ({
  createBrief: vi.fn(),
  fetchBriefTemplates: vi.fn(),
}));

import { createBrief, fetchBriefTemplates } from "../../lib/api/briefs";

const fetchTemplatesMock = vi.mocked(fetchBriefTemplates);
const createBriefMock = vi.mocked(createBrief);

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  fetchTemplatesMock.mockReset();
  fetchTemplatesMock.mockResolvedValue([]);
  createBriefMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CreateBriefModal a11y", () => {
  it("aria-labelledby points at the dialog title h3", async () => {
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-labelledby")).toBe("create-brief-title");
    const title = document.getElementById("create-brief-title");
    expect(title?.tagName).toBe("H3");
    expect(title?.textContent).toBe("Create brief");
  });

  it("Escape calls onCancel and preventDefaults the event", async () => {
    const onCancel = vi.fn();
    render(<CreateBriefModal onCreated={() => {}} onCancel={onCancel} />);
    const evt = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      document.dispatchEvent(evt);
    });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(evt.defaultPrevented).toBe(true);
  });

  it("Tab on the last focusable cycles back to the first; Shift+Tab on first wraps to last", async () => {
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    const dialog = screen.getByRole("dialog");
    const focusables = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => !el.hasAttribute("disabled"));
    expect(focusables.length).toBeGreaterThan(2);
    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    // Forward Tab on last cycles to first
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

    // Shift+Tab on first wraps to last
    first.focus();
    const shiftTabEvt = new KeyboardEvent("keydown", {
      key: "Tab",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      document.dispatchEvent(shiftTabEvt);
    });
    expect(document.activeElement).toBe(last);
  });

  it("restores focus to the opener element when the modal unmounts", async () => {
    const opener = document.createElement("button");
    opener.textContent = "Open brief modal";
    document.body.appendChild(opener);
    opener.focus();
    expect(document.activeElement).toBe(opener);

    const { unmount } = render(
      <CreateBriefModal onCreated={() => {}} onCancel={() => {}} />,
    );
    await flush();
    // Manually shift focus away to verify restore on unmount.
    const textarea = screen.getByLabelText(/brief markdown/i);
    (textarea as HTMLTextAreaElement).focus();
    expect(document.activeElement).toBe(textarea);

    unmount();
    expect(document.activeElement).toBe(opener);
    document.body.removeChild(opener);
  });
});
