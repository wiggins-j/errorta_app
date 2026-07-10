// F134 — Knowledge Quick Start guide: static ship copy.
//
// This module is the SINGLE SOURCE OF TRUTH for the guide's copy. It is plain
// structured JSX (no Markdown runtime, no external assets) so the guide renders
// offline / in browser-dev / against remote AIAR with zero fetches. Every UI
// label named below was verified against the shipped components on 2026-07-02
// (see docs/superpowers/plans/2026-07-02-F134-knowledge-quick-start-guide.md,
// "Verified labels"). If you reword anything that names a button/state, re-verify
// it against source.
//
// The three panel one-liners are ALSO exported here (PANEL_BLURBS) so the F132
// per-panel `blurb=` props and this guide share one copy source and can't drift
// (locked by quickStartContent.test.tsx).
import type { ReactNode } from "react";

export interface QuickStartSection {
  id: string;
  title: string;
  body: ReactNode;
}

/**
 * F132 per-panel blurbs, sourced here so the guide and the panel headers can't
 * diverge. These strings are the exact copy the panels already ship.
 */
export const PANEL_BLURBS = {
  corpus:
    "A corpus is a set of documents Errorta can answer questions about. Add files here, or point it at a folder. Once built, a corpus can be used across Errorta — ask a graded question on the Judge tab, or attach it to a Council room or Coding Team so those models answer from your documents.",
  briefs:
    "Don't have documents yet? Write a short brief describing what you want, and Errorta collects a corpus for you. It fetches from public sources over the internet using built-in connectors (arXiv, NASA NTRS, and general web pages that allow it) — no API keys or subscription CLI needed. Create one below, then run it to build the corpus.",
  watch:
    "Point Errorta at a folder and it keeps the selected corpus in sync — new and changed files are ingested automatically, so the corpus stays current without re-importing by hand.",
} as const;

export const QUICK_START_SECTIONS: QuickStartSection[] = [
  {
    id: "what-is-knowledge",
    title: "What is Knowledge?",
    body: (
      <>
        <p>
          <strong>Knowledge is where you build the library Errorta answers
          from.</strong>{" "}
          That library is called a <strong>corpus</strong> — a set of documents
          (PDFs, text, Word/Excel/PowerPoint, web pages, and more) that Errorta
          reads, splits into chunks, and indexes so it can retrieve the right
          passages when you ask a question.
        </p>
        <p>
          You can have several corpora — one per topic, project, or source. At
          the top of every Knowledge panel there&apos;s an{" "}
          <strong>Active corpus</strong> picker: it sets which corpus these three
          panels act on — the one you inspect, add files to, or watch. (When
          you&apos;re ready to <em>ask questions</em>, you pick a corpus on the{" "}
          <strong>Judge</strong> tab — see &ldquo;Use your corpus.&rdquo;)
        </p>
        <p>Knowledge has three panels, and they all work on that same corpus:</p>
        <ul>
          <li>
            <strong>Corpus</strong> — add files by hand and see what&apos;s in the
            corpus.
          </li>
          <li>
            <strong>Briefs</strong> — describe what you want in a short document
            and let Errorta collect it from public sources.
          </li>
          <li>
            <strong>Folder Watcher</strong> — point Errorta at a folder and let it
            keep the corpus in sync automatically.
          </li>
        </ul>
        <p>
          You don&apos;t need all three. Pick the one that matches where your
          documents are.
        </p>
      </>
    ),
  },
  {
    id: "three-ways",
    title: "The three ways to build a corpus",
    body: (
      <>
        <table className="quickstart-table">
          <thead>
            <tr>
              <th>Start here if…</th>
              <th>Use</th>
              <th>What it does</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>You already have the files on this computer</td>
              <td>
                <strong>Corpus</strong> (upload)
              </td>
              <td>Drag files in; Errorta ingests them.</td>
            </tr>
            <tr>
              <td>You don&apos;t have the files yet, but you can describe them</td>
              <td>
                <strong>Briefs</strong>
              </td>
              <td>
                You write a short &ldquo;brief&rdquo;; Errorta collects a corpus
                from public sources (arXiv, NASA NTRS, web pages).
              </td>
            </tr>
            <tr>
              <td>Your files live in a folder that keeps changing</td>
              <td>
                <strong>Folder Watcher</strong>
              </td>
              <td>
                Errorta watches the folder and ingests new/changed files
                automatically.
              </td>
            </tr>
          </tbody>
        </table>
        <p>
          You can also mix them: build from a brief, then watch a folder to keep
          adding to the same corpus.
        </p>
      </>
    ),
  },
  {
    id: "sample-corpus",
    title: "Fastest start: the sample corpus",
    body: (
      <>
        <p>
          If you just want to see how it works, install the{" "}
          <strong>sample corpus</strong> first.
        </p>
        <ol>
          <li>
            Open the <strong>Corpus</strong> panel.
          </li>
          <li>
            Expand <strong>&ldquo;Add a sample corpus.&rdquo;</strong>
          </li>
          <li>
            Pick the offered starter (Errorta&apos;s own docs — under 5 MB, fully
            deletable) and install it. You&apos;ll see it download, verify,
            extract, and ingest.
          </li>
          <li>
            When it finishes, it&apos;s an ordinary corpus you can ask questions
            about on the <strong>Judge</strong> tab — there&apos;s even a suggested
            prompt that opens Judge ready to run.
          </li>
        </ol>
        <p>Delete it any time — it&apos;s an ordinary corpus.</p>
      </>
    ),
  },
  {
    id: "build-from-files",
    title: "Build a corpus from files (Corpus panel)",
    body: (
      <>
        <p>Use this when the documents are already on your machine.</p>
        <ol>
          <li>
            Open <strong>Corpus</strong> and click <strong>New local
            corpus</strong>.
          </li>
          <li>
            Type a name (letters, numbers, <code>-</code> and <code>_</code>).
            This becomes the corpus id.
          </li>
          <li>
            A drop zone appears: <strong>drag files in, or click to
            browse.</strong>{" "}
            The line under it lists the file types Errorta accepts.
          </li>
          <li>
            Each file moves through visible states:{" "}
            <strong>Queued → Extracting text → Chunking → Embedding → Ready.</strong>{" "}
            A file that fails shows <strong>Failed</strong> with the reason on
            hover; click <strong>Re-ingest</strong> on that row to retry, or{" "}
            <strong>Delete</strong> to remove it.
          </li>
          <li>
            A footer shows totals: files, chunks, tokens, and disk used.
          </li>
        </ol>
        <p className="quickstart-note-heading">Good to know:</p>
        <ul>
          <li>
            <strong>Big files:</strong> anything over 100 MB asks you to confirm
            before ingesting.
          </li>
          <li>
            <strong>Duplicates:</strong> if you add a file that&apos;s already in
            the corpus (same contents), Errorta asks whether to{" "}
            <strong>Skip</strong> it or <strong>Re-ingest</strong> (overwrite).
          </li>
          <li>
            <strong>Password-protected PDFs</strong> are detected and skipped for
            now (password entry is coming in a later update).
          </li>
          <li>
            <strong>Check for changes:</strong> if files on disk changed since you
            ingested them, use <strong>Check for changes</strong> to preview
            what&apos;s new/updated/removed before re-ingesting.
          </li>
        </ul>
      </>
    ),
  },
  {
    id: "build-from-brief",
    title: "Build a corpus from a brief (Briefs panel)",
    body: (
      <>
        <p>
          Use this when you <em>don&apos;t have the documents yet</em> but you can
          describe them. A <strong>brief</strong> is a short document (Markdown
          with a small settings header) that names a target corpus and the public
          sources to collect from. Errorta runs it and builds the corpus for you —
          reproducibly, so you can re-run it later to refresh.
        </p>
        <p className="quickstart-note-heading">Set one up:</p>
        <ol>
          <li>
            Open <strong>Briefs</strong> and click <strong>Create brief.</strong>
          </li>
          <li>
            <strong>Pick a starting point.</strong> Choose a template to pre-fill
            the brief — a <strong>Blank</strong> starter for the bare structure, or
            a worked domain example (such as Aerospace or Python). The exact set of
            templates offered depends on what your sidecar provides;{" "}
            <strong>Blank</strong> is always available.
          </li>
          <li>
            <strong>Edit the brief.</strong> The top is a small settings block:
            <ul>
              <li>
                <code>project</code> — a human name for this collection.
              </li>
              <li>
                <code>corpus</code> — the corpus it will build (this is what shows
                up as an Active corpus).
              </li>
              <li>
                <code>sensitivity</code> — e.g. <code>Public</code>. Errorta only
                collects sources allowed by the compliance rules.
              </li>
              <li>
                <code>refresh</code> — <code>manual</code> (you re-run it) or a
                cadence.
              </li>
              <li>
                <code>sources</code> — the connectors to pull from (arXiv, NASA
                NTRS, or a web page). The templates show the shape; the connector
                guide covers the details.
              </li>
            </ul>
            Below that, plain Markdown describing what you want. Click{" "}
            <strong>Create.</strong>
          </li>
          <li>
            <strong>Validate first (recommended).</strong> With the brief in{" "}
            <strong>Draft</strong>, click <strong>Validate (preview)</strong>. This
            runs a dry pass — no downloads — and reports, per source, how many
            documents it <em>would</em> collect, how many pass the compliance
            check, and how many are refused (with sample reasons). Fix the brief
            until the preview looks right.
          </li>
          <li>
            <strong>Run it.</strong> Click <strong>Run</strong>. The brief moves{" "}
            <strong>Draft → Validating → Running</strong>, and a status panel
            streams live progress: a per-source table (source · state · collected ·
            refused), plus expandable <strong>Compliance refusals</strong> and{" "}
            <strong>Failures</strong> sections. It ends at{" "}
            <strong>Completed</strong> (or <strong>Failed</strong>, with reasons).
          </li>
          <li>
            <strong>Open the corpus.</strong> When it completes, click{" "}
            <strong>Open corpus</strong> — this jumps to the <strong>Corpus</strong>{" "}
            panel with your new corpus selected, where you&apos;ll see the collected
            files finish ingesting (same Ready/Failed states as uploads).
          </li>
          <li>
            <strong>Later:</strong> re-run a completed brief (<strong>Refresh</strong>)
            to pull new documents into the same corpus, or{" "}
            <strong>Archive</strong> it when you&apos;re done. You can also{" "}
            <strong>Export</strong> the brief to share it.
          </li>
        </ol>
        <p>
          Think of a brief as a <em>recipe</em> for a corpus: it&apos;s
          re-runnable, shareable, and tells you exactly where every document came
          from.
        </p>
      </>
    ),
  },
  {
    id: "folder-watcher",
    title: "Keep a corpus current with a folder (Folder Watcher panel)",
    body: (
      <>
        <p>
          Use this when your documents live in a folder that keeps changing — a
          downloads folder, a synced notes folder, a project directory. The watcher
          ingests new and changed files automatically, so the corpus stays current
          without re-importing by hand.{" "}
          <strong>Folder watching works on local corpora only.</strong>
        </p>
        <p className="quickstart-note-heading">Set one up:</p>
        <ol>
          <li>
            Make sure the corpus you want to keep current is selected as the{" "}
            <strong>Active corpus</strong> (top of the panel). Create a local
            corpus first if you need one.
          </li>
          <li>
            Open <strong>Folder Watcher</strong> and click{" "}
            <strong>Pick a folder.</strong>
          </li>
          <li>
            Errorta scans the folder and shows what it found: the number of
            supported files, total size, and a rough ingest-time estimate.
          </li>
          <li>
            <strong>Choose file types</strong> (optional): checkboxes let you
            include only the extensions you want.
          </li>
          <li>
            <strong>Choose what happens when a file is deleted</strong> from the
            folder:
            <ul>
              <li>
                <strong>Remove from corpus</strong> (default) — the corpus mirrors
                the folder.
              </li>
              <li>
                <strong>Keep in corpus, mark &ldquo;source missing&rdquo;</strong> —
                the document stays retrievable even after the file leaves the
                folder.
              </li>
            </ul>
          </li>
          <li>
            <strong>Cloud-sync warning:</strong> if the folder is inside iCloud /
            OneDrive / Google Drive, Errorta warns you (sync placeholders can look
            like partial files) and asks you to acknowledge before continuing.
          </li>
          <li>
            Click <strong>Start watching.</strong>
          </li>
        </ol>
        <p>
          <strong>Once it&apos;s watching</strong>, the panel shows a status card:
          the watched path, the file count, a health indicator, and the last scan
          time. From there you can <strong>Pause / Resume</strong>,{" "}
          <strong>Change folder</strong>, <strong>Stop watching</strong>, switch
          the on-delete policy, and <strong>Force rescan</strong> to pick up changes
          immediately. The status refreshes on its own; if scanning stalls
          you&apos;ll see a <strong>stale</strong> indicator.
        </p>
        <p>
          Files added by the watcher are tagged as <strong>watched</strong> in the
          corpus file list, so you can tell them apart from files you uploaded by
          hand.
        </p>
      </>
    ),
  },
  {
    id: "use-your-corpus",
    title: "Use your corpus",
    body: (
      <>
        <p>
          Once a corpus has some <strong>Ready</strong> files, ask questions
          against it on the <strong>Judge</strong> tab:
        </p>
        <ol>
          <li>
            Open the <strong>Judge</strong> tab.
          </li>
          <li>
            Choose your corpus in Judge&apos;s own corpus picker, then type a
            question in the prompt runner. (Heads up: the Judge tab has its{" "}
            <em>own</em> corpus selector — the Knowledge <strong>Active
            corpus</strong> picker governs the Knowledge panels, not Judge, so pick
            your corpus again here.)
          </li>
          <li>
            Click <strong>Run.</strong> Errorta retrieves the most relevant
            passages from that corpus, answers with them, and shows a{" "}
            <strong>verdict</strong> (a self-check of whether the answer is
            supported) plus whether the answer was grounded in your corpus.
          </li>
          <li>
            Over time, the <strong>Metrics</strong> and <strong>Replay</strong>{" "}
            tabs let you track answer quality and re-run past prompts.
          </li>
        </ol>
        <p>
          The corpus you built in Knowledge is exactly what powers those answers.
        </p>
      </>
    ),
  },
  {
    id: "local-vs-remote",
    title: "Local vs. remote corpora",
    body: (
      <>
        <p>
          Some corpora are <strong>local</strong> (stored on this machine) and some
          are <strong>remote</strong> (an AIAR instance Errorta connects to). The
          Active-corpus picker badges each one.
        </p>
        <ul>
          <li>
            <strong>Local corpora</strong> support everything above: upload, folder
            watch, file inspection, refresh.
          </li>
          <li>
            <strong>Remote corpora</strong> are read/summary only here. Upload,
            folder watch, and local file actions are disabled for them, because
            those actions write to <em>local</em> storage. That&apos;s expected —
            not an error.
          </li>
          <li>
            If the header shows <strong>&ldquo;retrieval not coordinated,&rdquo;</strong>{" "}
            it means the corpus list is coming from a remote backend that
            isn&apos;t yet wired to answer retrieval. You can inspect it, but
            querying alignment is tracked separately.
          </li>
        </ul>
      </>
    ),
  },
];

/** Table of contents derived from the sections — single source, no drift. */
export const QUICK_START_TOC: { id: string; title: string }[] =
  QUICK_START_SECTIONS.map((s) => ({ id: s.id, title: s.title }));
