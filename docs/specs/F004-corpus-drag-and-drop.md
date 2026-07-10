# F004 — Drag-and-drop corpus management

**Target version:** v0.1
**Status:** drafted
**Owner:** wiggins-j

---

## Problem

Without a way for users to add their own files, Errorta is a beautiful empty box. The first run can't get to a real "wow" moment until the user can give the AI their own data. The brief-driven collection wedge ([F008](F008-brief-driven-collection.md)) ships later; v0.1 needs the simplest, most-familiar pattern: drag files into a UI, see them get indexed, ask questions about them.

## Acceptance criteria

- Each corpus page has a clear drop zone with text ("Drop files here, or click to browse").
- Multi-file drag-and-drop is supported. The user can drop 100 files at once and the UI doesn't freeze.
- Files are **copied to the corpus storage location** (`~/.errorta/corpora/{name}/files/`) — not referenced by path. This is the portability promise.
- Each file's ingestion status is visible per-file: `queued → extracting → chunking → embedding → ready` (or `failed: <reason>`).
- The user can delete a file from the corpus. Deletion removes its chunks from the vector store AND its file copy from disk.
- The user can re-ingest a single file (e.g. after changing settings) or all files at once.
- Per-corpus stats footer: file count, chunk count, total tokens, total disk size.
- v0.1 supported formats:
  - `.pdf` (text-layer only; OCR fallback in F012)
  - `.docx` (Word)
  - `.xlsx` (Excel — one chunk per sheet by default)
  - `.pptx` (PowerPoint — one chunk per slide)
  - `.txt`, `.md`, `.markdown`
  - `.html`, `.htm`
  - `.json`, `.csv`, `.tsv`
- Unsupported file types are rejected with a clear message naming the format.
- A file larger than a configurable cap (default 100 MB) prompts the user to confirm before ingesting.

## UX flow

1. User navigates to a corpus (or creates a new one via "+ New Corpus").
2. They see the drop zone:

   >  **Drop files here or click to browse**
   > Supported: PDF, Word, Excel, PowerPoint, Markdown, plain text, HTML, JSON, CSV
   > Or [Watch a folder](F005) for auto-ingest (v0.2)

3. They drag in 5 PDFs. Files appear in the list immediately, each with a  status spinner:

   >  Smith_brief.pdf ·  Extracting text…
   >  Q3_financials.xlsx ·  Chunking… (3 of 5)
   >  case_notes.md · ✓ Ready (12 chunks)
   >  deposition.pdf · Warning: Failed: PDF is password-protected. [Provide password]
   >  contract.docx ·  Embedding… (47%)

4. Each row has a hover-revealed delete (`delete`) and re-ingest (`↻`) button.

5. Once all files complete, the user can switch to the **Simulate** / chat tab and ask questions immediately.

## Technical approach

- **Extraction module:** `python/errorta_extract/` — one extractor per format.
  - `pdf.py` — PyMuPDF (fitz) for text-layer extraction. Returns `[{text, page_number, ...}]` per page.
  - `docx.py` — python-docx. One paragraph per chunk, merged if too small.
  - `xlsx.py` — openpyxl. One chunk per sheet by default. Each chunk includes the sheet name + a formatted table.
  - `pptx.py` — python-pptx. One chunk per slide. Includes slide title + body text + speaker notes.
  - `html.py` — BeautifulSoup. Strip nav/footer/script; keep main content.
  - `text.py` — direct read with encoding detection (chardet fallback).
  - `json.py` — pretty-print + chunk on top-level keys.
  - `csv.py` / `tsv.py` — header + N rows per chunk (configurable).
- **Ingestion pipeline:** new file → extractor → AIAR's existing `aiar.rag.ingest` API → ChromaDB. Errorta wraps AIAR with status updates emitted to the frontend via Tauri events.
- **File storage:** files are copied to `~/.errorta/corpora/{name}/files/{original_filename}`. Originals are kept so the source-jump feature (F013) can open them.
- **Manifest:** per-corpus `~/.errorta/corpora/{name}/manifest.json` tracks every ingested file with `{file_id, original_path, copied_path, sha256, chunk_count, chunk_ids, status, ingested_at}`.
- **Status updates:** the Python sidecar streams per-file status to the Tauri frontend via the Tauri event bus, frontend re-renders the file row.
- **Backend endpoints (added to AIAR or Errorta-side):**
  - `POST /api/corpus/{name}/upload` (multipart)
  - `GET /api/corpus/{name}/files`
  - `DELETE /api/corpus/{name}/files/{file_id}`
  - `POST /api/corpus/{name}/files/{file_id}/reingest`

## Dependencies

- [F006](F006-tauri-shell.md) — Tauri shell hosts the drop zone and file list
- AIAR's `aiar.rag.ingest` API — used as the chunking + embedding backend
- AIAR's vector store and metadata schema

## Risks / open questions

- **Large file blast:** the user drops a folder with 50,000 files. Mitigation: confirm dialog at 100+ files, file-type filter dropdown, ability to cancel mid-ingestion.
- **Password-protected PDFs:** ship a "provide password" UI per file. Passwords stored only in memory for the session, never on disk.
- **Encoding hell:** plain text files come in every encoding imaginable. Use chardet with explicit fallback to latin-1 (no errors). Surface the detected encoding in the file metadata.
- **Duplicate detection:** user drops the same file twice. Mitigation: SHA-256 the file content; if it matches an existing entry in the corpus's manifest, prompt "this file is already in this corpus — re-ingest or skip?"
- **Cloud-synced folders:** see [F005](F005-folder-watch.md) — the drop-and-copy pattern handles this correctly because we own the copy after ingest.
- **OCR for scanned PDFs:** explicitly deferred to F012 (v0.2). v0.1 says "PDF text layer only" and rejects PDFs that have no text layer with a helpful error.
