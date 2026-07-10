import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import KnowledgeHeader from "./KnowledgeHeader";
import type { KnowledgeCorpusSelection } from "./useKnowledgeCorpusSelection";

// The header pings /healthz on mount; keep the test hermetic.
vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../../lib/api");
  return {
    ...actual,
    sidecarHealth: vi.fn().mockResolvedValue({ corpus_backend: null }),
  };
});

afterEach(cleanup);

function selection(): KnowledgeCorpusSelection {
  return {
    corpora: [],
    loading: false,
    error: null,
    selectedName: "",
    selected: null,
    setSelectedName: () => {},
    reload: async () => [],
  };
}

describe("KnowledgeHeader — Quick Start control (F134)", () => {
  it("renders the Quick Start control and opens the guide", () => {
    const onOpenQuickStart = vi.fn();
    render(
      <KnowledgeHeader
        title="Corpus"
        spec="F004"
        selection={selection()}
        onOpenQuickStart={onOpenQuickStart}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Quick Start/ }));
    expect(onOpenQuickStart).toHaveBeenCalledTimes(1);
  });

  it("omits the control when onOpenQuickStart is not provided", () => {
    render(<KnowledgeHeader title="Corpus" spec="F004" selection={selection()} />);
    expect(
      screen.queryByRole("button", { name: /Quick Start/ }),
    ).not.toBeInTheDocument();
  });
});
