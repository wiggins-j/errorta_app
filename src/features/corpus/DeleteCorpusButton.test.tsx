import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import DeleteCorpusButton from "./DeleteCorpusButton";

afterEach(() => cleanup());

describe("DeleteCorpusButton", () => {
  it("requires a confirm before calling onDelete", async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined);
    render(<DeleteCorpusButton name="alpha" onDelete={onDelete} />);

    // First click reveals the confirm, but does not delete yet.
    fireEvent.click(screen.getByTitle("Delete corpus alpha"));
    expect(onDelete).not.toHaveBeenCalled();
    expect(
      screen.getByText(/Delete corpus .*alpha.* and all its files/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText("Confirm delete"));
    expect(onDelete).toHaveBeenCalledTimes(1);
  });

  it("cancel dismisses the confirm without deleting", () => {
    const onDelete = vi.fn();
    render(<DeleteCorpusButton name="beta" onDelete={onDelete} />);

    fireEvent.click(screen.getByTitle("Delete corpus beta"));
    fireEvent.click(screen.getByText("Cancel"));

    expect(onDelete).not.toHaveBeenCalled();
    // Back to the plain trigger button.
    expect(screen.getByTitle("Delete corpus beta")).toBeInTheDocument();
  });
});
