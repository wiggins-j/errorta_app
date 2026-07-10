import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import RetryBanner from "../RetryBanner";

describe("RetryBanner", () => {
  it("onRetry click invokes handler (parent owns attempt state)", async () => {
    const onRetry = vi.fn();
    render(
      <RetryBanner reason="timeout" attempts={0} max={3} onRetry={onRetry} />,
    );
    const btn = screen.getByRole("button", { name: /retry/i });
    await userEvent.click(btn);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("attempts equals max disables retry with calm cap message and no model-switch hint", () => {
    render(
      <RetryBanner reason="server" attempts={3} max={3} onRetry={() => {}} />,
    );
    expect(
      screen.getByText(/try again later or check your local model/i),
    ).toBeInTheDocument();
    // No retry button visible once capped.
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    // Explicitly assert no model-switch copy.
    expect(screen.queryByText(/switch model/i)).toBeNull();
    expect(screen.queryByText(/different model/i)).toBeNull();
  });

  it("renders reason-specific cause text for each RetryReason", () => {
    const { rerender } = render(
      <RetryBanner reason="timeout" attempts={0} onRetry={() => {}} />,
    );
    expect(
      screen.getByText(/judge model took too long to respond/i),
    ).toBeInTheDocument();

    rerender(
      <RetryBanner reason="unparseable" attempts={0} onRetry={() => {}} />,
    );
    expect(
      screen.getByText(/judge response couldn't be parsed/i),
    ).toBeInTheDocument();

    rerender(<RetryBanner reason="server" attempts={0} onRetry={() => {}} />);
    expect(
      screen.getByText(/local sidecar returned a server error/i),
    ).toBeInTheDocument();
  });

  it("renders role=status live region with data-reason attribute", () => {
    const { container } = render(
      <RetryBanner reason="timeout" attempts={1} max={3} onRetry={() => {}} />,
    );
    const banner = container.querySelector(".retry-banner");
    expect(banner).not.toBeNull();
    expect(banner?.getAttribute("role")).toBe("status");
    expect(banner?.getAttribute("data-reason")).toBe("timeout");
  });

  it("renders attempt counter Attempt N of MAX before cap", () => {
    render(
      <RetryBanner reason="timeout" attempts={1} max={3} onRetry={() => {}} />,
    );
    // attempts=1 => next attempt is 2 of 3.
    expect(screen.getByText(/attempt\s*2\s*of\s*3/i)).toBeInTheDocument();
  });

  it("does NOT issue any fetch calls (presentational only)", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    try {
      const onRetry = vi.fn();
      render(
        <RetryBanner reason="timeout" attempts={0} max={3} onRetry={onRetry} />,
      );
      await userEvent.click(screen.getByRole("button", { name: /retry/i }));
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      fetchSpy.mockRestore();
    }
  });
});
