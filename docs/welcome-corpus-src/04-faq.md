# Errorta FAQ

## Is Errorta free?

Yes. Errorta is free to download and use. There is no paid tier, no
trial, no upgrade prompt. The underlying framework (AIAR) is also
free and open-source under the Apache-2.0 license.

## Does Errorta phone home?

No. Errorta runs entirely on your machine. It does not send your
prompts, your documents, your corrections, or any usage data to
anyone. The only outbound network calls are:

- Downloading the optional welcome corpus on first run (you can
  decline).
- Downloading Ollama or model weights if you choose to install them
  through Errorta (you can also install Ollama and models by hand).
- Checking for Errorta updates (you can disable this).

Nothing about your questions, your answers, or your corpus ever
leaves the device.

## What language models does Errorta use?

Errorta uses local models served by [Ollama](https://ollama.com/) by
default. When you first run Errorta, it detects your hardware and
suggests a model size you can actually run. You can pick a different
model at any time. Errorta does not require a cloud API key.

## Can I add my own files?

Yes. Drag a folder of PDFs, Word docs, Markdown files, HTML pages,
spreadsheets, or plaintext onto the Errorta window and Errorta will
ingest them as a corpus you can query. See the next page for the
specifics.

## What is AIAR?

AIAR is the open-source framework Errorta is built on. AIAR does the
retrieval, the LLM-judge, and the grounding. Errorta is the desktop
shell that turns AIAR into something you can use without writing
Python. See `03-built-on-aiar.md` for the details.

## What does "judge loop" mean?

Every answer Errorta gives gets graded by a second LLM call (the
"judge"). The judge produces a structured verdict — accepted,
rejected, or partial — with a reason. When you accept the verdict,
the correction is remembered: next time you ask the same kind of
question, Errorta uses your accepted correction as part of the
context. See `01-the-judge-loop.md` for the full picture.

## Does Errorta need internet to run?

After the initial setup (downloading Ollama + a model + optionally
the welcome corpus), no. Errorta works offline. You can run it on a
laptop without a connection.

## How do I get help?

The Errorta source tree includes an `errorta-downloads` companion
site for documentation. If you find a bug, the project's GitHub
issues page is the right place to report it.
