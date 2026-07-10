import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import CorpusPicker from "./CorpusPicker";
import type { CorpusSummary } from "../../lib/api/corpus";

const CORPORA: CorpusSummary[] = [
  { name: "alpha", fileCount: 2, readyCount: 1, status: "indexing", source: "local" },
  { name: "beta", fileCount: 0, readyCount: 3737, status: "ready", source: "remote" },
];

afterEach(() => cleanup());

describe("CorpusPicker", () => {
  it("renders catalog options and the selected corpus source badge", () => {
    render(
      <CorpusPicker
        label="Corpus"
        value="beta"
        onChange={() => undefined}
        corpora={CORPORA}
        allowEmpty
      />,
    );

    const select = screen.getByLabelText("Corpus") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual([
      "Select corpus",
      "alpha (1/2 files ready)",
      "beta (3737 chunks ready)",
    ]);
    const selection = screen.getByLabelText("Corpus selection");
    expect(within(selection).getByText("beta")).toBeInTheDocument();
    expect(within(selection).getByText("remote")).toBeInTheDocument();
  });

  it("fires single-select changes", () => {
    const onChange = vi.fn();
    render(
      <CorpusPicker
        label="Existing corpus"
        value=""
        onChange={onChange}
        corpora={CORPORA}
        allowEmpty
      />,
    );

    fireEvent.change(screen.getByLabelText("Existing corpus"), {
      target: { value: "alpha" },
    });

    expect(onChange).toHaveBeenCalledWith("alpha");
  });

  it("supports multi-select changes via checkboxes", () => {
    const onChange = vi.fn();
    render(
      <CorpusPicker
        label="Room corpora"
        multiple
        value={["alpha"]}
        onChange={onChange}
        corpora={CORPORA}
      />,
    );

    // alpha is pre-selected; ticking beta adds it.
    const beta = screen.getByRole("checkbox", { name: /beta/ });
    fireEvent.click(beta);
    expect(onChange).toHaveBeenCalledWith(["alpha", "beta"]);

    // un-ticking alpha removes it.
    onChange.mockClear();
    const alpha = screen.getByRole("checkbox", { name: /alpha/ });
    fireEvent.click(alpha);
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("shows empty and loading states", () => {
    const { rerender } = render(
      <CorpusPicker
        label="Corpus"
        value=""
        onChange={() => undefined}
        corpora={[]}
        allowEmpty
      />,
    );
    expect(screen.getByText("No corpora available")).toBeInTheDocument();

    rerender(
      <CorpusPicker
        label="Corpus"
        value=""
        onChange={() => undefined}
        corpora={[]}
        loading
        allowEmpty
      />,
    );
    expect(screen.getByText("Loading corpora…")).toBeInTheDocument();
  });

  it("renders a delete affordance only when onDeleteCorpus is provided", () => {
    const onDeleteCorpus = vi.fn();
    const { rerender } = render(
      <CorpusPicker
        label="Corpus"
        value="beta"
        onChange={() => undefined}
        corpora={CORPORA}
        allowEmpty
      />,
    );
    // No handler → no delete button.
    expect(screen.queryByTitle("Delete corpus beta")).toBeNull();

    rerender(
      <CorpusPicker
        label="Corpus"
        value="beta"
        onChange={() => undefined}
        corpora={CORPORA}
        allowEmpty
        onDeleteCorpus={onDeleteCorpus}
      />,
    );
    fireEvent.click(screen.getByTitle("Delete corpus beta"));
    fireEvent.click(screen.getByText("Confirm delete"));
    expect(onDeleteCorpus).toHaveBeenCalledWith("beta");
  });

  it("keeps a configured selection that is absent from the catalog", () => {
    render(
      <CorpusPicker
        label="Room corpora"
        multiple
        value={["legacy-corpus"]}
        onChange={() => undefined}
        corpora={[]}
      />,
    );

    expect(screen.getByText("legacy-corpus")).toBeInTheDocument();
    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.getAllByText(/missing from catalog/).length).toBeGreaterThan(0);
  });
});
