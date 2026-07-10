import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import QuickStartGuide from "./QuickStartGuide";
import { QUICK_START_SECTIONS } from "./quickStartContent";
import { expectNoA11yViolations } from "../council/a11y-helpers";

afterEach(cleanup);

describe("QuickStartGuide", () => {
  it("renders nothing when closed", () => {
    const { container } = render(
      <QuickStartGuide open={false} onClose={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders all eight sections in order when open", () => {
    render(<QuickStartGuide open onClose={() => {}} />);
    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(headings).toEqual(QUICK_START_SECTIONS.map((s) => s.title));
    expect(headings).toHaveLength(8);
  });

  it("does not fetch anything on render (static guarantee)", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    render(<QuickStartGuide open onClose={() => {}} />);
    expect(fetchSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("closes on Escape, Close button, and backdrop click", () => {
    const onClose = vi.fn();
    const { rerender } = render(<QuickStartGuide open onClose={onClose} />);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    rerender(<QuickStartGuide open onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Close Quick Start" }));
    expect(onClose).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("restores focus to the opener when it closes", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    expect(document.activeElement).toBe(opener);

    const { rerender } = render(<QuickStartGuide open onClose={() => {}} />);
    // Focus moved into the dialog on open.
    expect(document.activeElement).not.toBe(opener);

    rerender(<QuickStartGuide open={false} onClose={() => {}} />);
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("has a table-of-contents entry per section", () => {
    render(<QuickStartGuide open onClose={() => {}} />);
    const nav = screen.getByRole("navigation", { name: "Quick Start contents" });
    const links = nav.querySelectorAll("button.quickstart-toc-link");
    expect(links).toHaveLength(QUICK_START_SECTIONS.length);
  });

  it("has no serious or critical a11y violations", async () => {
    const { container } = render(<QuickStartGuide open onClose={() => {}} />);
    await expectNoA11yViolations(container);
  });
});
