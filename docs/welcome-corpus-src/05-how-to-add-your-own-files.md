# How to add your own files

Errorta works on **corpora** — collections of documents you can ask
questions about. The welcome corpus you started with is one corpus.
You can add as many more as you like.

## The fastest way: drag and drop

1. Open the Corpora pane in the Errorta window.
2. Click **New corpus** and give it a name (e.g. "Research papers" or
   "House documents").
3. Drag a folder or a set of files onto the drop zone.

Errorta walks the folder, picks up everything it can read, and
ingests it. You'll see progress as it goes.

## What file types can Errorta ingest?

- **PDFs** — extracted with text-only (OCR is not on by default; if
  your PDF is a scanned image, the text won't be picked up).
- **Word documents** — `.docx`.
- **Markdown** — `.md`.
- **HTML** — `.html` and `.htm`.
- **Plain text** — `.txt`.
- **Spreadsheets** — `.xlsx` and `.csv`.
- **PowerPoint** — `.pptx`.

If a file is encrypted or password-protected, Errorta will tell you
and skip it.

## What happens to my files?

Nothing leaves your machine. Errorta extracts text from each file,
breaks it into chunks, computes embeddings, and stores them in a
local database under your Errorta data directory. The original files
stay where you put them.

You can delete a corpus at any time. Deleting it removes the
extracted text and the embeddings; your original files are
untouched.

## What if my files change?

Errorta supports a **folder watch** mode that re-ingests files when
they change on disk. Turn it on for any corpus from the Corpora
pane.

You can also point Errorta at a folder and have it re-check for
changes on demand instead of watching continuously.

## Tips for good results

- **Smaller, focused corpora work better than one giant catch-all.**
  Errorta retrieves from the corpus you select for each query.
  Splitting "everything I read" into "papers" and "house docs" gives
  you tighter retrieval.
- **Filenames matter.** Errorta uses filenames as part of how it
  recognizes sources. If a file is called `untitled-3.pdf`, citations
  to it will be confusing.
- **Use the judge.** When Errorta gives you an answer that looks
  wrong, accept-with-correction instead of just regenerating. The
  next time you ask the same question, your correction is part of
  the answer.
