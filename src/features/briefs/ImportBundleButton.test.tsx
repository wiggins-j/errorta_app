import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ImportBundleButton from "./ImportBundleButton";

vi.mock("../../lib/api/briefs", () => ({
  importBundle: vi.fn(),
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(),
}));
vi.mock("@tauri-apps/plugin-fs", () => ({
  readFile: vi.fn(),
  readBinaryFile: vi.fn(),
}));

import { importBundle } from "../../lib/api/briefs";

const importBundleMock = vi.mocked(importBundle);

function setTauri(present: boolean) {
  if (present) {
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
  } else {
    delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  }
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  importBundleMock.mockReset();
});

afterEach(() => {
  setTauri(false);
});

describe("ImportBundleButton (browser path)", () => {
  beforeEach(() => setTauri(false));

  it("uploads the picked file, POSTs via importBundle, and fires onCreated", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    importBundleMock.mockResolvedValue({
      brief_id: "imported-bundle-1",
      corpus_name: "default",
      files_imported: 5,
      warnings: [],
      timestamp_imported: "2026-06-08T00:00:00Z",
    });

    render(<ImportBundleButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import brief bundle from file/i }));

    const input = screen.getByTestId("import-bundle-file-input") as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "bundle.tar.gz", {
      type: "application/gzip",
    });
    await user.upload(input, file);
    await flush();

    expect(importBundleMock).toHaveBeenCalledTimes(1);
    const callArgs = importBundleMock.mock.calls[0];
    expect(callArgs[0]).toBeInstanceOf(File);
    expect(callArgs[1]).toEqual(expect.objectContaining({ renameTo: undefined }));
    expect(onCreated).toHaveBeenCalledWith("imported-bundle-1");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("surfaces a conflict banner on 409 and retries with rename_to when affordance clicked", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();

    const conflictErr = new Error(
      'HTTP 409 on /briefs/import-bundle: {"detail":{"code":"already_exists","message":"Brief with id \\"x\\" already exists in corpus \\"y\\". Pass rename_to to import under a different id.","brief_id":"x"}}',
    ) as Error & { status?: number; body?: unknown };
    conflictErr.status = 409;
    conflictErr.body = {
      detail: {
        code: "already_exists",
        message:
          'Brief with id "x" already exists in corpus "y". Pass rename_to to import under a different id.',
        brief_id: "x",
      },
    };

    importBundleMock
      .mockRejectedValueOnce(conflictErr)
      .mockResolvedValueOnce({
        brief_id: "x-renamed",
        corpus_name: "default",
        files_imported: 5,
        warnings: [],
        timestamp_imported: "2026-06-08T00:00:00Z",
      });

    render(<ImportBundleButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import brief bundle from file/i }));
    const input = screen.getByTestId("import-bundle-file-input") as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "bundle.tar.gz", {
      type: "application/gzip",
    });
    await user.upload(input, file);
    await flush();

    // Conflict banner is visible.
    const alert = screen.getByRole("alert");
    expect(alert.textContent ?? "").toMatch(/already exists/i);
    expect(alert.textContent ?? "").not.toMatch(/HTTP 409/);
    expect(onCreated).not.toHaveBeenCalled();

    // Click the rename-retry affordance.
    const retryBtn = screen.getByRole("button", { name: /import with new id/i });
    await user.click(retryBtn);
    await flush();

    expect(importBundleMock).toHaveBeenCalledTimes(2);
    const secondCall = importBundleMock.mock.calls[1]!;
    const secondOpts = secondCall[1]!;
    expect(secondOpts.renameTo).toBeTruthy();
    expect(secondOpts.renameTo).toMatch(/^x-/);
    expect(onCreated).toHaveBeenCalledWith("x-renamed");
  });

  it("does not call onCreated on a non-409 error", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    const err = new Error("HTTP 400 on /briefs/import-bundle: bad tar") as Error & {
      status?: number;
    };
    err.status = 400;
    importBundleMock.mockRejectedValue(err);

    render(<ImportBundleButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import brief bundle from file/i }));
    const input = screen.getByTestId("import-bundle-file-input") as HTMLInputElement;
    const file = new File([new Uint8Array([0])], "bundle.tar.gz");
    await user.upload(input, file);
    await flush();

    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /import with new id/i }),
    ).not.toBeInTheDocument();
  });
});
