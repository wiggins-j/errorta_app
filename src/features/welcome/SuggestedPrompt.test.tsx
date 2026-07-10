import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import SuggestedPrompt from "./SuggestedPrompt";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SuggestedPrompt — F109 Run → judge handoff", () => {
  it("dispatches errorta:navigate to judge carrying the prompt; no fetch/toast", async () => {
    const dispatched: CustomEvent[] = [];
    const listener = (e: Event) => dispatched.push(e as CustomEvent);
    window.addEventListener("errorta:navigate", listener);
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    render(
      <SuggestedPrompt prompt="What license is AIAR under?" corpusName="welcome" />,
    );

    await userEvent.click(screen.getByRole("button", { name: /run in judge/i }));

    expect(dispatched).toHaveLength(1);
    expect(dispatched[0].detail).toEqual({
      view: "judge",
      prompt: "What license is AIAR under?",
    });
    // No network call, and no stale "coming in F001" toast.
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(screen.queryByText(/coming in f001/i)).toBeNull();
    expect(screen.queryByText(/simulate/i)).toBeNull();

    window.removeEventListener("errorta:navigate", listener);
  });

  it("carries the edited value, not the original prompt", async () => {
    const dispatched: CustomEvent[] = [];
    const listener = (e: Event) => dispatched.push(e as CustomEvent);
    window.addEventListener("errorta:navigate", listener);

    render(<SuggestedPrompt prompt="original" corpusName="welcome" />);

    const textarea = screen.getByRole("textbox");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "edited prompt");
    await userEvent.click(screen.getByRole("button", { name: /run in judge/i }));

    expect(dispatched[0].detail).toEqual({
      view: "judge",
      prompt: "edited prompt",
    });

    window.removeEventListener("errorta:navigate", listener);
  });

  it("no longer references F001 / Simulate anywhere", () => {
    render(<SuggestedPrompt prompt="hi" corpusName="welcome" />);
    expect(screen.queryByText(/f001/i)).toBeNull();
    expect(screen.queryByText(/simulate/i)).toBeNull();
  });
});
