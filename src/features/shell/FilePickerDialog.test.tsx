// F040-01 fix — pickPaths must return the Tauri dialog result when the
// `@tauri-apps/plugin-dialog` module is available (the packaged-app path),
// instead of silently returning [] as the old `new Function("import(...)")`
// indirection did when the plugin failed to bundle.

import { afterEach, describe, expect, it, vi } from "vitest";

const _mocks = vi.hoisted(() => ({
  dialogOpen: vi.fn(),
  isTauri: true,
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: (...args: unknown[]) => _mocks.dialogOpen(...args),
}));

vi.mock("../../lib/sidecarPort", () => ({
  isTauriRuntime: () => _mocks.isTauri,
}));

import { pickPaths } from "./FilePickerDialog";

afterEach(() => {
  _mocks.dialogOpen.mockReset();
  _mocks.isTauri = true;
});

describe("pickPaths (Tauri dialog available)", () => {
  it("returns the dialog result for a single-file pick with requireAbsolutePath", async () => {
    _mocks.dialogOpen.mockResolvedValue("/custom/path/agent");
    const paths = await pickPaths({ requireAbsolutePath: true });
    // It no longer silently returns [] when Tauri is present.
    expect(paths).toEqual(["/custom/path/agent"]);
    expect(_mocks.dialogOpen).toHaveBeenCalledTimes(1);
  });

  it("normalizes an array result for a multiple pick", async () => {
    _mocks.dialogOpen.mockResolvedValue(["/a/one", "/a/two"]);
    const paths = await pickPaths({ multiple: true, requireAbsolutePath: true });
    expect(paths).toEqual(["/a/one", "/a/two"]);
  });

  it("returns [] when the user cancels (dialog resolves null)", async () => {
    _mocks.dialogOpen.mockResolvedValue(null);
    const paths = await pickPaths({ requireAbsolutePath: true });
    expect(paths).toEqual([]);
  });

  it("passes through directory + filters + defaultPath to the dialog", async () => {
    _mocks.dialogOpen.mockResolvedValue("/some/dir");
    await pickPaths({
      directory: true,
      filters: [{ name: "exe", extensions: ["", "exe"] }],
      defaultPath: "/start/here",
    });
    expect(_mocks.dialogOpen).toHaveBeenCalledWith({
      multiple: undefined,
      directory: true,
      filters: [{ name: "exe", extensions: ["", "exe"] }],
      defaultPath: "/start/here",
    });
  });

  it("returns the chosen folder for directory + requireAbsolutePath (browseDirectory path)", async () => {
    // This is the exact combo CreateProjectForm.browseDirectory uses for the
    // new-project "Browse…" (Project location + Repo path). It must RETURN the
    // folder the native dialog produced, not silently fall through to [].
    _mocks.dialogOpen.mockResolvedValue("/Users/example/Projects");
    const paths = await pickPaths({
      directory: true,
      requireAbsolutePath: true,
    });
    expect(paths).toEqual(["/Users/example/Projects"]);
    expect(_mocks.dialogOpen).toHaveBeenCalledTimes(1);
  });

  it("keeps the browser fallback when the plugin package is bundled outside Tauri", async () => {
    _mocks.isTauri = false;
    const paths = await pickPaths({
      directory: true,
      requireAbsolutePath: true,
    });
    expect(paths).toEqual([]);
    expect(_mocks.dialogOpen).not.toHaveBeenCalled();
  });
});
