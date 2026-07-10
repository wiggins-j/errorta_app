// F008-IMPORT — Import a brief by loading a local .md file from disk.
//
// In the Tauri shell we use the native file dialog + fs plugins so the
// chooser feels native and we can read arbitrary paths. In a plain browser
// (vitest, web dev preview) we fall back to a hidden <input type="file">
// so the same component is still usable. Both paths converge on a single
// `createBrief(markdown)` POST.
//
// Local pre-validation guards against the common mistake of pointing the
// importer at a plain markdown file with no YAML frontmatter — we'd rather
// surface a friendly inline error than round-trip a 422 from the sidecar.

import { useRef, useState, type ChangeEvent } from "react";
import { createBrief, validateMarkdown } from "../../lib/api/briefs";

interface Props {
  /** Called with the new brief_id after a successful POST /briefs. */
  onCreated: (briefId: string) => void;
  /** Optional class for layout alongside the existing Templates button. */
  className?: string;
  /** Optional label override (defaults to "Import"). */
  label?: string;
}

const FRONTMATTER_RE = /^---\s*\r?\n[\s\S]*?\r?\n---\s*(\r?\n|$)/;
const SIZE_WARN_THRESHOLD = 50_000;

interface TauriInternals {
  __TAURI_INTERNALS__?: unknown;
}

function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean((window as TauriInternals).__TAURI_INTERNALS__);
}

interface StructuredErrorEntry {
  msg?: string;
  message?: string;
  loc?: unknown;
  field?: string;
  [key: string]: unknown;
}

/**
 * Pull a readable message out of the Error thrown by `request()` in
 * `src/lib/api.ts`. That helper throws `new Error("HTTP <status> on
 * <path>: <body>")`, where `<body>` is the raw JSON detail (or a snippet
 * thereof). We try to surface the first field-level validation message
 * when the body is structured, otherwise fall back to the raw text.
 */
export function extractBackendMessage(err: unknown): string {
  if (!(err instanceof Error)) return String(err);
  const m = err.message.match(/^HTTP \d+ on [^:]+: (.*)$/s);
  const tail = m ? m[1] : err.message;
  // Try to parse the tail as JSON. The sidecar normally returns
  // `{ "detail": <thing> }` where `<thing>` is either a string (slug
  // conflict) or `{ errors: [{ msg, loc, ... }] }` for parse failures.
  try {
    const parsed: unknown = JSON.parse(tail);
    if (parsed && typeof parsed === "object") {
      const detail = (parsed as { detail?: unknown }).detail;
      if (typeof detail === "string") return detail;
      if (detail && typeof detail === "object") {
        const errors = (detail as { errors?: unknown }).errors;
        if (Array.isArray(errors) && errors.length > 0) {
          const top = errors
            .slice(0, 2)
            .map((e: StructuredErrorEntry) => e?.msg ?? e?.message)
            .filter((x): x is string => typeof x === "string" && x.length > 0);
          if (top.length > 0) return top.join("; ");
        }
        const msg = (detail as { msg?: unknown; message?: unknown }).msg
          ?? (detail as { message?: unknown }).message;
        if (typeof msg === "string") return msg;
      }
    }
  } catch {
    // Body wasn't JSON; fall through to the raw text.
  }
  return tail || err.message;
}

function hasFrontmatter(markdown: string): boolean {
  return FRONTMATTER_RE.test(markdown);
}

/**
 * Render a list of structured validation error entries as one inline string.
 * Mirrors the shape returned by `/briefs/validate-markdown`: each entry is a
 * dict that usually carries `msg` (or `message`). We keep the first three so
 * the banner stays readable; anything beyond that gets summarised.
 */
function formatValidationErrors(
  errors: Array<Record<string, unknown>>,
): string {
  const messages: string[] = [];
  for (const e of errors) {
    const msg = e?.msg ?? e?.message;
    if (typeof msg === "string" && msg.length > 0) messages.push(msg);
  }
  if (messages.length === 0) return "Brief failed validation";
  const head = messages.slice(0, 3).join("; ");
  if (messages.length > 3) {
    return `${head} (+${messages.length - 3} more)`;
  }
  return head;
}

export default function ImportBriefButton({
  onCreated,
  className,
  label = "Import",
}: Props) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const handleMarkdown = async (markdown: string) => {
    if (!hasFrontmatter(markdown)) {
      setError(
        "Brief must start with YAML frontmatter delimited by --- lines",
      );
      return;
    }
    if (markdown.length > SIZE_WARN_THRESHOLD) {
      // Non-fatal: warn but proceed. We log to console so the dev shell
      // surfaces it, but we don't block the user.
      // eslint-disable-next-line no-console
      console.warn(
        `ImportBriefButton: brief is ${markdown.length} chars (> ${SIZE_WARN_THRESHOLD}); proceeding`,
      );
    }
    setLoading(true);
    setError(null);
    try {
      // F008-IMPORT-VAL — pre-flight the brief through the stateless
      // validator. A failed validation must NOT persist anything, so we
      // surface inline errors and bail before createBrief().
      const validation = await validateMarkdown(markdown);
      if (!validation.ok) {
        setError(formatValidationErrors(validation.errors));
        return;
      }
      const summary = await createBrief(markdown);
      onCreated(summary.brief_id);
    } catch (err) {
      setError(extractBackendMessage(err));
    } finally {
      setLoading(false);
    }
  };

  const openTauriDialog = async () => {
    // Dynamically import so the plugin isn't pulled into browser/test bundles.
    const dialog = await import("@tauri-apps/plugin-dialog");
    const fs = await import("@tauri-apps/plugin-fs");
    const selected = await dialog.open({
      multiple: false,
      filters: [{ name: "Markdown", extensions: ["md", "markdown"] }],
    });
    if (selected === null || selected === undefined) {
      // Cancel: leave state untouched.
      return;
    }
    const path = Array.isArray(selected) ? selected[0] : selected;
    if (!path || typeof path !== "string") return;
    const markdown = await fs.readTextFile(path);
    await handleMarkdown(markdown);
  };

  const openBrowserPicker = () => {
    // Reset the input value first so selecting the same file twice still fires.
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
      fileInputRef.current.click();
    }
  };

  const onBrowserFileSelected = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return; // Cancel: untouched.
    const markdown = await file.text();
    await handleMarkdown(markdown);
  };

  const onClick = async () => {
    setError(null);
    try {
      if (isTauri()) {
        await openTauriDialog();
      } else {
        openBrowserPicker();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setLoading(false);
    }
  };

  return (
    <>
      <button
        type="button"
        className={className ?? "briefs-list-import-btn"}
        onClick={onClick}
        disabled={loading}
        aria-label="Import brief from file"
      >
        {loading ? "Importing…" : label}
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept=".md,.markdown"
        style={{ display: "none" }}
        onChange={onBrowserFileSelected}
        aria-hidden="true"
        data-testid="import-brief-file-input"
      />
      {error && (
        <div className="briefs-parse-banner" role="alert">
          {error}
        </div>
      )}
    </>
  );
}
