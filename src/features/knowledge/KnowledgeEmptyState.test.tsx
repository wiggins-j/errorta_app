import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import KnowledgeEmptyState from "./KnowledgeEmptyState";

afterEach(cleanup);

describe("KnowledgeEmptyState — Quick Start offer (F134)", () => {
  it("shows the three build cards", () => {
    render(<KnowledgeEmptyState />);
    expect(screen.getByText("Upload files")).toBeInTheDocument();
    expect(screen.getByText("Build from brief")).toBeInTheDocument();
    expect(screen.getByText("Watch folder")).toBeInTheDocument();
  });

  it("does not show the prominent offer without onOpenQuickStart", () => {
    render(<KnowledgeEmptyState />);
    expect(
      screen.queryByText(/Read the 2-minute Quick Start/),
    ).not.toBeInTheDocument();
  });

  it("shows the offer and opens the guide when provided", () => {
    const onOpenQuickStart = vi.fn();
    render(<KnowledgeEmptyState onOpenQuickStart={onOpenQuickStart} />);
    fireEvent.click(screen.getByText(/Read the 2-minute Quick Start/));
    expect(onOpenQuickStart).toHaveBeenCalledTimes(1);
  });

  it("hides the offer once dismissed", () => {
    render(
      <KnowledgeEmptyState onOpenQuickStart={() => {}} quickStartDismissed />,
    );
    expect(
      screen.queryByText(/Read the 2-minute Quick Start/),
    ).not.toBeInTheDocument();
    // Build cards remain.
    expect(screen.getByText("Upload files")).toBeInTheDocument();
  });

  it("calls onDismissQuickStart from the Dismiss control", () => {
    const onDismissQuickStart = vi.fn();
    render(
      <KnowledgeEmptyState
        onOpenQuickStart={() => {}}
        onDismissQuickStart={onDismissQuickStart}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Dismiss the Quick Start guide" }),
    );
    expect(onDismissQuickStart).toHaveBeenCalledTimes(1);
  });
});
