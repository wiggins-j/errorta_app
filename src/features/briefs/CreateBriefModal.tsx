// F008 Briefs — create modal with a small set of starter templates.
//
// F014-LIB: templates are now sourced from the sidecar's
// `GET /briefs/templates` endpoint (which scans `docs/examples/briefs/`).
// The hardcoded `TEMPLATES` map below is retained as an offline-only
// fallback so the modal still works when the sidecar is unreachable
// (network error, sidecar down, tests that mock only `createBrief`).
import { useEffect, useRef, useState } from "react";
import {
  createBrief,
  fetchBriefTemplates,
  type BriefTemplate,
} from "../../lib/api/briefs";

interface Props {
  onCreated: (briefId: string, corpusName?: string) => void;
  onCancel: () => void;
  /**
   * Optional preselected template id. When provided and the id matches a
   * loaded template, the modal seeds the textarea from that template
   * instead of "Blank". Lets a "Templates" launcher elsewhere preselect
   * a card without changing the modal's public flow.
   */
  initialTemplateId?: string;
  initialCorpusName?: string;
}

const TEMPLATES: Record<string, { label: string; body: string }> = {
  blank: {
    label: "Blank",
    body: `---
project: My Project
corpus: my-corpus
sensitivity: Public
refresh: manual
description: Describe the corpus here.
sources: []
---

# My Project

Brief body.
`,
  },
  aerospace: {
    label: "Aerospace",
    body: `---
project: Aerospace Mini
corpus: aerospace-mini
sensitivity: Public
refresh: manual
description: Small aerospace seed corpus from arXiv.
tags:
  - aerospace
sources:
  - name: arxiv
    config:
      categories:
        - cs.RO
        - astro-ph.IM
      date_from: '2024-01-01'
---

# Aerospace Mini

A small public aerospace corpus.
`,
  },
  regulations: {
    label: "Regulations",
    body: `---
project: Regulations
corpus: regulations
sensitivity: Public
refresh: manual
description: Public regulatory documents.
tags:
  - regulations
sources: []
---

# Regulations

Public regulatory corpus.
`,
  },
  python: {
    label: "Python",
    body: `---
project: Python Docs
corpus: python-docs
sensitivity: Public
refresh: manual
description: Curated slice of Python ecosystem documentation.
tags:
  - python
sources: []
---

# Python Docs

Python documentation corpus.
`,
  },
  medical: {
    label: "Medical",
    body: `---
project: Medical Public
corpus: medical-public
sensitivity: Public
refresh: manual
description: Public-domain medical literature.
tags:
  - medical
sources: []
---

# Medical Public

Public medical reference corpus.
`,
  },
};

type TemplateKey = keyof typeof TEMPLATES;

interface TemplateEntry {
  id: string;
  label: string;
  body: string;
}

// Build the offline-fallback list once at module load. Ordering matches
// the previous hardcoded layout (Blank first) so the picker UX is stable
// when the sidecar is unreachable.
const FALLBACK_ENTRIES: TemplateEntry[] = (Object.keys(TEMPLATES) as TemplateKey[]).map(
  (key) => ({ id: key, label: TEMPLATES[key].label, body: TEMPLATES[key].body }),
);

function mergeEntries(
  fallback: TemplateEntry[],
  remote: BriefTemplate[] | null,
): TemplateEntry[] {
  // Always keep "Blank" at index 0 — it's the only entry that doesn't
  // correspond to a docs/examples/briefs file. Remote entries augment the
  // fallback list; for stale-while-revalidate we replace any fallback
  // entry whose id matches a remote id (so docs edits surface immediately
  // after the network resolves).
  if (!remote || remote.length === 0) return fallback;
  const blank = fallback.find((e) => e.id === "blank");
  const head: TemplateEntry[] = blank ? [blank] : [];
  const seen = new Set<string>(blank ? ["blank"] : []);
  for (const t of remote) {
    if (seen.has(t.id)) continue;
    seen.add(t.id);
    head.push({
      id: t.id,
      // Prefer the title from the brief itself; if the API returned an
      // empty string for whatever reason, fall back to the id.
      label: t.title || t.id,
      // Use the full markdown body, not the (capped) preview. Server-side
      // `markdown_preview` is truncated at ~600 chars — submitting that as
      // the brief body would silently drop frontmatter/body content
      // (F014-LIB regression fix).
      body: t.markdown,
    });
  }
  // Append any remaining fallback entries not already covered by remote
  // (e.g. "python"/"medical" placeholders that don't exist as files yet).
  for (const e of fallback) {
    if (seen.has(e.id)) continue;
    seen.add(e.id);
    head.push(e);
  }
  return head;
}

function applyCorpusName(markdown: string, corpusName?: string): string {
  const clean = (corpusName ?? "").trim();
  if (!clean) return markdown;
  if (/^corpus:\s*.*$/m.test(markdown)) {
    return markdown.replace(/^corpus:\s*.*$/m, `corpus: ${clean}`);
  }
  if (/^project:\s*.*$/m.test(markdown)) {
    return markdown.replace(/^project:\s*.*$/m, (line) => `${line}\ncorpus: ${clean}`);
  }
  return `---\ncorpus: ${clean}\n---\n\n${markdown}`;
}

export default function CreateBriefModal({
  onCreated,
  onCancel,
  initialTemplateId,
  initialCorpusName,
}: Props) {
  const [entries, setEntries] = useState<TemplateEntry[]>(FALLBACK_ENTRIES);
  const initialId = initialTemplateId ?? "blank";
  const initialEntry =
    FALLBACK_ENTRIES.find((e) => e.id === initialId) ?? FALLBACK_ENTRIES[0];
  const [templateId, setTemplateId] = useState<string>(initialEntry.id);
  const [markdown, setMarkdown] = useState<string>(
    applyCorpusName(initialEntry.body, initialCorpusName),
  );
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  // Capture opener focus on mount; restore on unmount.
  useEffect(() => {
    previouslyFocusedRef.current =
      (typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null) ?? null;
    return () => {
      const opener = previouslyFocusedRef.current;
      if (opener && typeof opener.focus === "function") {
        try {
          opener.focus();
        } catch {
          // ignore
        }
      }
    };
  }, []);

  // Escape closes. Tab/Shift+Tab cycle focus within the dialog.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onCancelRef.current?.();
        return;
      }
      if (e.key === "Tab" && dialogRef.current) {
        const focusables = dialogRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        );
        const enabled = Array.from(focusables).filter(
          (el) => !el.hasAttribute("disabled"),
        );
        if (enabled.length === 0) return;
        const first = enabled[0];
        const last = enabled[enabled.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !dialogRef.current.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (active === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  useEffect(() => {
    // Stale-while-revalidate: render fallback immediately, then swap in the
    // sidecar's response when it lands. If the sidecar is unreachable we
    // keep the fallback list and swallow the error (callers see the
    // hardcoded templates so the modal stays usable offline).
    let cancelled = false;
    // Wrap the fetch in Promise.resolve so a synchronous throw (e.g. a test
    // that mocks the module without providing `fetchBriefTemplates`) is
    // routed into the catch handler instead of crashing the effect.
    Promise.resolve()
      .then(() => fetchBriefTemplates())
      .then((remote) => {
        if (cancelled) return;
        setEntries((current) => mergeEntries(FALLBACK_ENTRIES, remote) || current);
        // If the caller preselected an id that only exists in the remote
        // list, swap the textarea to its body now that we have it.
        if (initialTemplateId) {
          const hit = remote.find((t) => t.id === initialTemplateId);
          if (hit) {
            setTemplateId(hit.id);
            setMarkdown(applyCorpusName(hit.markdown, initialCorpusName));
          }
        }
      })
      .catch(() => {
        // Network/sidecar error: keep fallback. Intentionally silent — the
        // user can still create a brief from the offline template set.
      });
    return () => {
      cancelled = true;
    };
  }, [initialTemplateId, initialCorpusName]);

  const pickTemplate = (id: string) => {
    const hit = entries.find((e) => e.id === id);
    if (!hit) return;
    setTemplateId(id);
    setMarkdown(applyCorpusName(hit.body, initialCorpusName));
  };

  const onSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const summary = await createBrief(markdown);
      onCreated(summary.brief_id, summary.corpus_name);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      ref={dialogRef}
      className="briefs-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="create-brief-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="briefs-modal">
        <h3 id="create-brief-title">Create brief</h3>
        <div className="briefs-modal-templates">
          {entries.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={templateId === entry.id ? "primary" : ""}
              onClick={() => pickTemplate(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </div>
        <textarea
          value={markdown}
          onChange={(e) => setMarkdown(e.target.value)}
          spellCheck={false}
          aria-label="Brief markdown"
        />
        {error && (
          <div className="briefs-parse-banner" role="alert">
            {error}
          </div>
        )}
        <div className="briefs-modal-actions">
          <button type="button" onClick={onCancel} disabled={submitting}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            onClick={onSubmit}
            disabled={submitting}
          >
            {submitting ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
