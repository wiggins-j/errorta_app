import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import EmptyState from "../EmptyState";

describe("EmptyState", () => {
  it("renders message text verbatim", () => {
    render(<EmptyState message="Nothing here yet." />);
    expect(screen.getByText("Nothing here yet.")).toBeInTheDocument();
  });

  it("renders optional title when supplied", () => {
    render(<EmptyState title="All clear" message="Nothing to do." />);
    expect(screen.getByText("All clear")).toBeInTheDocument();
    expect(screen.getByText("Nothing to do.")).toBeInTheDocument();
  });
});
