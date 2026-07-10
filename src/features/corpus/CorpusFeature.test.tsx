import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listCorpora: vi.fn(),
  refreshPreview: vi.fn(),
  deleteCorpus: vi.fn(),
  sidecarHealth: vi.fn(),
}));

vi.mock("../../lib/api/corpus", () => ({
  listCorpora: mocks.listCorpora,
  refreshPreview: mocks.refreshPreview,
  deleteCorpus: mocks.deleteCorpus,
  hasCorpusCapability: (
    corpus: { source?: string; capabilities?: Record<string, boolean> } | null,
    capability: string,
  ) => Boolean(corpus?.capabilities?.[capability] ?? corpus?.source === "local"),
  corpusCountLabel: (corpus: { readyCount: number; fileCount: number; unit?: string }) =>
    corpus.unit === "chunks"
      ? `${corpus.readyCount} chunks ready`
      : `${corpus.readyCount}/${corpus.fileCount} files ready`,
}));

vi.mock("../../lib/api", () => ({
  sidecarHealth: mocks.sidecarHealth,
}));

vi.mock("./CorpusDropZone", () => ({
  default: ({ corpus }: { corpus: string }) => (
    <div data-testid="corpus-drop-zone">drop zone: {corpus}</div>
  ),
}));

vi.mock("./RefreshDiffModal", () => ({
  default: ({ corpus, isOpen }: { corpus: string; isOpen: boolean }) => (
    <div data-testid="refresh-diff-modal">
      {isOpen ? `refresh ${corpus}` : "closed"}
    </div>
  ),
}));

vi.mock("../welcome/WelcomeInstaller", () => ({
  default: () => <div data-testid="welcome-installer" />,
}));

import CorpusFeature from "./index";

beforeEach(() => {
  const storage = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => {
        storage.set(key, value);
      },
      removeItem: (key: string) => storage.delete(key),
      clear: () => storage.clear(),
    },
  });
  mocks.listCorpora.mockResolvedValue([
    { name: "alpha", fileCount: 2, readyCount: 2, status: "ready", source: "local" },
    { name: "beta", fileCount: 4, readyCount: 3, status: "indexing", source: "remote" },
  ]);
  mocks.refreshPreview.mockResolvedValue({
    corpus: "alpha",
    added: [],
    removed: [],
    updated: [],
    snapshot_at: "now",
    partial: false,
  });
  mocks.deleteCorpus.mockResolvedValue({ ok: true, corpus: "alpha" });
  mocks.sidecarHealth.mockResolvedValue({
    service: "errorta-sidecar",
    version: "test",
    now: "2026-06-22T00:00:00Z",
    aiar_available: false,
    corpus_backend: { kind: "local", detail: {}, retrieval_coordinated: true },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CorpusFeature", () => {
  it("lists catalog corpora and remote selection does not mount local file controls", async () => {
    render(<CorpusFeature />);

    const picker = await screen.findByLabelText("Active corpus");
    await waitFor(() => expect(screen.getByTestId("corpus-drop-zone")).toHaveTextContent("alpha"));

    fireEvent.change(picker, { target: { value: "beta" } });

    expect(screen.queryByTestId("corpus-drop-zone")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Remote corpus summary")).toHaveTextContent("beta");
    expect(screen.getByText("Check for changes")).toBeDisabled();
    expect(mocks.refreshPreview).not.toHaveBeenCalled();
  });

  it("keeps an explicit new-corpus affordance for fresh corpus names", async () => {
    render(<CorpusFeature />);
    await screen.findByLabelText("Active corpus");

    fireEvent.click(screen.getByText("New local corpus"));
    fireEvent.change(screen.getByLabelText("New corpus name"), {
      target: { value: "fresh corpus!" },
    });

    expect(screen.getByTestId("corpus-drop-zone")).toHaveTextContent("freshcorpus");
  });

  it("deletes the active corpus after confirm and refreshes + clears selection", async () => {
    // Second list call (after delete) drops alpha, leaving beta.
    mocks.listCorpora
      .mockResolvedValueOnce([
        { name: "alpha", fileCount: 2, readyCount: 2, status: "ready", source: "local" },
        { name: "beta", fileCount: 4, readyCount: 3, status: "indexing", source: "remote" },
      ])
      .mockResolvedValueOnce([
        { name: "beta", fileCount: 4, readyCount: 3, status: "indexing", source: "remote" },
      ]);

    render(<CorpusFeature />);
    await screen.findByLabelText("Active corpus");
    await waitFor(() => expect(screen.getByTestId("corpus-drop-zone")).toHaveTextContent("alpha"));

    fireEvent.click(screen.getByTitle("Delete corpus alpha"));
    fireEvent.click(screen.getByText("Confirm delete"));

    await waitFor(() => expect(mocks.deleteCorpus).toHaveBeenCalledWith("alpha"));
    // List refreshed (second listCorpora call) and selection moved off alpha.
    await waitFor(() => expect(mocks.listCorpora).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByTestId("corpus-drop-zone")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Remote corpus summary")).toHaveTextContent("beta");
  });

  it("surfaces an error and keeps the selection when delete fails", async () => {
    mocks.deleteCorpus.mockRejectedValueOnce(new Error("HTTP 503 — remote"));

    render(<CorpusFeature />);
    await screen.findByLabelText("Active corpus");
    await waitFor(() => expect(screen.getByTestId("corpus-drop-zone")).toHaveTextContent("alpha"));

    fireEvent.click(screen.getByTitle("Delete corpus alpha"));
    fireEvent.click(screen.getByText("Confirm delete"));

    await waitFor(() => expect(screen.getByText("HTTP 503 — remote")).toBeInTheDocument());
    // No refetch on failure; selection stays put.
    expect(mocks.listCorpora).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("corpus-drop-zone")).toHaveTextContent("alpha");
  });

  it("does nothing when the delete confirm is cancelled", async () => {
    render(<CorpusFeature />);
    await screen.findByLabelText("Active corpus");
    await waitFor(() => expect(screen.getByTestId("corpus-drop-zone")).toHaveTextContent("alpha"));

    fireEvent.click(screen.getByTitle("Delete corpus alpha"));
    fireEvent.click(screen.getByText("Cancel"));

    expect(mocks.deleteCorpus).not.toHaveBeenCalled();
    expect(mocks.listCorpora).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("corpus-drop-zone")).toHaveTextContent("alpha");
  });
});
