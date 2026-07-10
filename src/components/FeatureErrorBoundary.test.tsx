import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeatureErrorBoundary } from "./FeatureErrorBoundary";

function ThrowingChild({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error("shell card exploded");
  return <div>healthy child</div>;
}

describe("FeatureErrorBoundary", () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    consoleErrorSpy.mockRestore();
  });

  it("contains a feature render error instead of unmounting the app shell", () => {
    render(
      <FeatureErrorBoundary featureLabel="Shell" resetKey="shell">
        <ThrowingChild shouldThrow />
      </FeatureErrorBoundary>,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Shell failed to load");
    expect(alert).toHaveTextContent("shell card exploded");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("retries the child without switching tabs", () => {
    let shouldThrow = true;
    const { rerender } = render(
      <FeatureErrorBoundary featureLabel="Shell" resetKey="shell">
        <ThrowingChild shouldThrow={shouldThrow} />
      </FeatureErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    shouldThrow = false;
    rerender(
      <FeatureErrorBoundary featureLabel="Shell" resetKey="shell">
        <ThrowingChild shouldThrow={shouldThrow} />
      </FeatureErrorBoundary>,
    );
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    expect(screen.getByText("healthy child")).toBeInTheDocument();
  });

  it("resets automatically when the active feature changes", () => {
    const { rerender } = render(
      <FeatureErrorBoundary featureLabel="Shell" resetKey="shell">
        <ThrowingChild shouldThrow />
      </FeatureErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Shell failed to load");

    rerender(
      <FeatureErrorBoundary featureLabel="Judge" resetKey="judge">
        <ThrowingChild shouldThrow={false} />
      </FeatureErrorBoundary>,
    );

    expect(screen.getByText("healthy child")).toBeInTheDocument();
  });
});
