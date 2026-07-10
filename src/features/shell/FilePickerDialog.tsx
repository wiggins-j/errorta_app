// F006 — reusable native file picker wrapper.
//
// Calls Tauri's `plugin-dialog` when running inside the desktop shell, and
// falls back to a hidden <input type="file"> in plain `vite dev` so the same
// component works in both environments. Consumers do not need to know which
// transport was used.

import { useCallback, useRef } from "react";
import { isTauriRuntime } from "../../lib/sidecarPort";

export interface FilePickerOptions {
  /** Allow choosing multiple files. */
  multiple?: boolean;
  /** Pick directories instead of files. */
  directory?: boolean;
  /** Filename filters (Tauri only; ignored in browser fallback). */
  filters?: { name: string; extensions: string[] }[];
  /** Initial directory hint (Tauri only). */
  defaultPath?: string;
  /**
   * F105: when true, the caller needs a real absolute filesystem path (e.g. a
   * delivery root or repo path). The browser `<input type=file>` fallback only
   * yields a bare file name, which is NOT an absolute path — so in the browser
   * we return `[]` instead of injecting a fake name. Tauri (which gives real
   * paths) is unaffected.
   */
  requireAbsolutePath?: boolean;
}

type DialogModule = {
  open: (opts: {
    multiple?: boolean;
    directory?: boolean;
    filters?: { name: string; extensions: string[] }[];
    defaultPath?: string;
  }) => Promise<string | string[] | null>;
};

async function loadTauriDialog(): Promise<DialogModule | null> {
  if (!isTauriRuntime()) return null;
  try {
    // Normal dynamic import so Vite bundles the plugin as a runtime-resolved
    // chunk (the proven pattern from `src/lib/sidecarPort.ts`). The previous
    // `new Function("import(...)")` trick dodged the bundler entirely, so the
    // plugin module never shipped in the packaged app and the runtime import
    // failed silently. The try/catch still covers the plain `vite dev` /
    // browser case where the plugin isn't present at runtime.
    const mod = (await import("@tauri-apps/plugin-dialog")) as DialogModule;
    return mod;
  } catch {
    return null;
  }
}

export async function pickPaths(opts: FilePickerOptions = {}): Promise<string[]> {
  const dialog = await loadTauriDialog();
  if (dialog) {
    const result = await dialog.open({
      multiple: opts.multiple,
      directory: opts.directory,
      filters: opts.filters,
      defaultPath: opts.defaultPath,
    });
    if (result == null) return [];
    return Array.isArray(result) ? result : [result];
  }
  // Browser fallback (no Tauri): the <input type=file> picker can only expose a
  // bare file name, never an absolute path. When the caller needs a real path
  // (requireAbsolutePath) — or is picking a directory, where a name is useless —
  // do NOT inject a fake name; return [] so the UI can ask for a pasted path.
  if (opts.requireAbsolutePath || opts.directory) return [];
  // Otherwise, return file names only (legacy behavior).
  return new Promise<string[]>((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    if (opts.multiple) input.multiple = true;
    if (opts.directory) {
      // webkitdirectory is non-standard but widely supported.
      (input as unknown as { webkitdirectory: boolean }).webkitdirectory = true;
    }
    input.onchange = () => {
      const names = Array.from(input.files ?? []).map((f) => f.name);
      resolve(names);
    };
    input.click();
  });
}

interface ButtonProps {
  label?: string;
  options?: FilePickerOptions;
  onPicked: (paths: string[]) => void;
  disabled?: boolean;
}

/**
 * Convenience button wrapper. Use `pickPaths` directly when you need finer
 * control over flow.
 */
export function FilePickerDialog({
  label = "Browse…",
  options,
  onPicked,
  disabled,
}: ButtonProps) {
  const busy = useRef(false);
  const onClick = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const paths = await pickPaths(options);
      if (paths.length > 0) onPicked(paths);
    } finally {
      busy.current = false;
    }
  }, [options, onPicked]);

  return (
    <button type="button" className="shell-file-picker" onClick={onClick} disabled={disabled}>
      {label}
    </button>
  );
}

export default FilePickerDialog;
