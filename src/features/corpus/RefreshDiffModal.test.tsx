import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RefreshDiffModal from "./RefreshDiffModal";
import type { RefreshDiffResponse } from "./types";
import * as corpusApi from "../../lib/api/corpus";

vi.mock("../../lib/api/corpus", async (orig) => {
  const actual = await orig<typeof import("../../lib/api/corpus")>();
  return {
    ...actual,
    refreshApply: vi.fn(),
  };
});

beforeEach(() => {
  vi.mocked(corpusApi.refreshApply).mockReset();
});

function makeDiff(overrides: Partial<RefreshDiffResponse> = {}): RefreshDiffResponse {
  return {
    corpus: "default",
    added: [],
    removed: [],
    updated: [],
    snapshot_at: "2026-06-08T12:34:56Z",
    partial: false,
    ...overrides,
  };
}

describe("RefreshDiffModal", () => {
  it("renders null when isOpen is false", () => {
    const { container } = render(
      <RefreshDiffModal
        isOpen={false}
        onClose={() => {}}
        corpus="default"
        diff={null}
        loading={false}
        error={null}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders 'No changes' when diff arrays are all empty", () => {
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={makeDiff()}
        loading={false}
        error={null}
      />,
    );
    expect(screen.getByText("No changes")).toBeInTheDocument();
  });

  it("renders three tab counts for a populated diff and shows added entries by default", () => {
    const diff = makeDiff({
      added: [{ original_path: "/docs/a.pdf" }, { original_path: "/docs/b.pdf" }],
      removed: [{ original_path: "/docs/old.pdf" }],
      updated: [
        {
          old: { original_path: "/docs/u-old.pdf" },
          new: { original_path: "/docs/u-new.pdf" },
        },
      ],
    });
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={diff}
        loading={false}
        error={null}
      />,
    );
    expect(screen.getByRole("tab", { name: "Added (2)" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Removed (1)" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Updated (1)" })).toBeInTheDocument();

    // Added tab is selected by default; its entries are visible.
    expect(screen.getByRole("tab", { name: "Added (2)" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("/docs/a.pdf")).toBeInTheDocument();
    expect(screen.getByText("/docs/b.pdf")).toBeInTheDocument();
    // Removed/updated entries are NOT visible while Added is active.
    expect(screen.queryByText("/docs/old.pdf")).not.toBeInTheDocument();
  });

  it("switches the visible list when a different tab is clicked", async () => {
    const user = userEvent.setup();
    const diff = makeDiff({
      added: [{ original_path: "/docs/a.pdf" }],
      removed: [{ original_path: "/docs/old.pdf" }],
      updated: [
        {
          old: { original_path: "/docs/u-old.pdf" },
          new: { original_path: "/docs/u-new.pdf" },
        },
      ],
    });
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={diff}
        loading={false}
        error={null}
      />,
    );

    await user.click(screen.getByRole("tab", { name: "Removed (1)" }));
    expect(screen.getByRole("tab", { name: "Removed (1)" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("/docs/old.pdf")).toBeInTheDocument();
    expect(screen.queryByText("/docs/a.pdf")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Updated (1)" }));
    expect(screen.getByRole("tab", { name: "Updated (1)" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("/docs/u-old.pdf")).toBeInTheDocument();
    expect(screen.getByText("/docs/u-new.pdf")).toBeInTheDocument();
  });

  it("renders a loading state when loading is true and no error/diff yet", () => {
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={null}
        loading={true}
        error={null}
      />,
    );
    expect(screen.getByText(/loading preview/i)).toBeInTheDocument();
    // No tabs while loading.
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  });

  it("renders an error banner when error is non-null (e.g. 404)", () => {
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="missing"
        diff={null}
        loading={false}
        error="HTTP 404 — corpus not found"
      />,
    );
    const banner = screen.getByRole("alert");
    expect(banner).toBeInTheDocument();
    expect(banner.textContent).toContain("404");
    // No tabs while error is shown without a diff.
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  });

  it("renders the snapshot footer using toLocaleString formatting", () => {
    const diff = makeDiff({ snapshot_at: "2026-06-08T12:34:56Z" });
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={diff}
        loading={false}
        error={null}
      />,
    );
    const expected = new Date("2026-06-08T12:34:56Z").toLocaleString();
    expect(
      screen.getByText(new RegExp(`Snapshot taken at ${expected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`)),
    ).toBeInTheDocument();
  });

  it("exposes role='dialog' and aria-modal='true'", () => {
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={makeDiff()}
        loading={false}
        error={null}
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("invokes onClose when the Close button is clicked", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={onClose}
        corpus="default"
        diff={makeDiff()}
        loading={false}
        error={null}
      />,
    );
    await user.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // ---- F015-APPLY: Apply button ----------------------------------------

  it("disables the Apply button when there are no changes", () => {
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="default"
        diff={makeDiff()}
        loading={false}
        error={null}
      />,
    );
    const apply = screen.getByRole("button", { name: /apply/i });
    expect(apply).toBeDisabled();
  });

  it("enables Apply when the diff has changes and calls refreshApply on click", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onApplied = vi.fn();
    vi.mocked(corpusApi.refreshApply).mockResolvedValue({
      ingested: ["a"],
      removed: [],
      updated: [],
      errors: [],
    });
    const diff = makeDiff({
      added: [{ original_path: "/docs/a.pdf" }],
    });
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={onClose}
        corpus="mycorpus"
        diff={diff}
        loading={false}
        error={null}
        onApplied={onApplied}
      />,
    );
    const apply = screen.getByRole("button", { name: /^apply$/i });
    expect(apply).not.toBeDisabled();
    await user.click(apply);
    await waitFor(() => {
      expect(corpusApi.refreshApply).toHaveBeenCalledWith("mycorpus", diff);
    });
    await waitFor(() => {
      expect(onApplied).toHaveBeenCalledTimes(1);
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("shows a spinner / Applying label while the request is in flight", async () => {
    const user = userEvent.setup();
    let resolveFn!: (v: {
      ingested: string[];
      removed: string[];
      updated: string[];
      errors: never[];
    }) => void;
    vi.mocked(corpusApi.refreshApply).mockImplementation(
      () =>
        new Promise((res) => {
          resolveFn = res as typeof resolveFn;
        }),
    );
    const diff = makeDiff({ added: [{ original_path: "/docs/a.pdf" }] });
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="c"
        diff={diff}
        loading={false}
        error={null}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^apply$/i }));
    expect(
      screen.getByRole("button", { name: /applying/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("status", { name: /applying/i })).toBeInTheDocument();
    resolveFn({ ingested: ["a"], removed: [], updated: [], errors: [] });
    await waitFor(() => {
      expect(corpusApi.refreshApply).toHaveBeenCalled();
    });
  });

  it("renders an error banner with a retry button when apply fails", async () => {
    const user = userEvent.setup();
    vi.mocked(corpusApi.refreshApply).mockRejectedValueOnce(
      new Error("HTTP 500 — boom"),
    );
    const diff = makeDiff({ added: [{ original_path: "/docs/a.pdf" }] });
    render(
      <RefreshDiffModal
        isOpen={true}
        onClose={() => {}}
        corpus="c"
        diff={diff}
        loading={false}
        error={null}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^apply$/i }));
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("HTTP 500");
    // Retry succeeds the second time.
    vi.mocked(corpusApi.refreshApply).mockResolvedValueOnce({
      ingested: ["a"],
      removed: [],
      updated: [],
      errors: [],
    });
    await user.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => {
      expect(corpusApi.refreshApply).toHaveBeenCalledTimes(2);
    });
  });
});
