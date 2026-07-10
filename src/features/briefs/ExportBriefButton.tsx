// F008-EXPORT — Export the active brief's markdown to a local .md file.
//
// Mirror of ImportBriefButton: native save dialog when running inside the
// Tauri shell, synthesized <a download> when running in a plain browser
// (vitest, web dev preview). The component is a pure sink — it consumes
// the markdown already carried by `BriefDetail.markdown` and never hits
// the sidecar, so there is no F008-EXPORT endpoint on the Python side.
//
// As with ImportBriefButton, the Tauri plugin imports are dynamic so the
// plugin modules never leak into browser/test bundles (vitest does not
// have __TAURI_INTERNALS__ and would crash on plugin init).

import { useState } from "react";

interface Props {
  /** Brief id used for the default filename (`${briefId}.md`). */
  briefId: string;
  /** Raw markdown to write to disk verbatim — comes from `BriefDetail.markdown`. */
  markdown: string;
  /** Mirrors the parent's busy gate. */
  disabled?: boolean;
  /** Called after a successful write. `dir` is "(browser download)" in browser mode. */
  onExported?: (info: { slug: string; dir: string }) => void;
  /** Optional class for layout alongside the FSM buttons. */
  className?: string;
  /** Optional label override (defaults to "Export"). */
  label?: string;
}

interface TauriInternals {
  __TAURI_INTERNALS__?: unknown;
}

function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean((window as TauriInternals).__TAURI_INTERNALS__);
}

export default function ExportBriefButton({
  briefId,
  markdown,
  disabled,
  onExported,
  className,
  label = "Export",
}: Props) {
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const saveViaTauri = async () => {
    // Dynamically import so the plugins are not pulled into browser/test bundles.
    const dialog = await import("@tauri-apps/plugin-dialog");
    const fs = await import("@tauri-apps/plugin-fs");
    const pathApi = await import("@tauri-apps/api/path");
    const selected = await dialog.save({
      filters: [{ name: "Brief markdown", extensions: ["md"] }],
      defaultPath: `${briefId}.md`,
    });
    if (selected === null || selected === undefined) {
      // Cancel: leave state untouched.
      return;
    }
    const path = selected;
    await fs.writeTextFile(path, markdown);
    const dir = await pathApi.dirname(path);
    onExported?.({ slug: briefId, dir });
  };

  const saveViaBrowser = () => {
    const blob = new Blob([markdown], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${briefId}.md`;
      anchor.rel = "noopener";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
    } finally {
      URL.revokeObjectURL(url);
    }
    onExported?.({ slug: briefId, dir: "(browser download)" });
  };

  const onClick = async () => {
    setError(null);
    setLoading(true);
    try {
      if (isTauri()) {
        await saveViaTauri();
      } else {
        saveViaBrowser();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <button
        type="button"
        className={className}
        onClick={onClick}
        disabled={disabled || loading}
        aria-label="Export brief to file"
      >
        {loading ? "Exporting…" : label}
      </button>
      {error && (
        <div className="briefs-parse-banner" role="alert">
          {error}
        </div>
      )}
    </>
  );
}
