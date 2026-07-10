# AIAR setup — what it is and how to connect it

First-run onboarding only asks you to connect the models you want to use (API
keys, subscription CLIs, or local Ollama). Everything about **AIAR** — the
retrieval layer — is configured later, in **Settings**. This guide explains what
AIAR does and how to set it up.

## What AIAR is

[AIAR](https://github.com/wiggins-j/aiar) is the free, open-source (Apache-2.0)
retrieval framework Errorta is built on. It's the substrate that turns "a chat
with a model" into "a grounded answer over your own documents." AIAR provides:

- **Retrieval / RAG** — pulls the relevant passages from your corpora.
- **LLM-as-judge** — every answer gets a structured verdict on whether it holds up.
- **Grounding store** — accepted corrections persist and feed forward into future
  answers for similar questions.

Inside Errorta, AIAR is what powers **Knowledge** answers, **Council** retrieval,
and the **Coding Team**'s grounding. Connecting a model (onboarding) gives you a
chat; AIAR is what makes the answer trustworthy and specific to your data.

## The two modes

### Local, in-process (default — nothing to do)

By default Errorta runs AIAR **inside its own sidecar on your machine**. There is
no setup, no network, and no server to stand up — ingest documents in
**Knowledge** and ask away. This is the right mode for most people and is fully
private: your corpora never leave the device.

### Remote / hosted AIAR

If you already run a **deployed AIAR server** (for example a shared team instance
or a bigger box with more compute), you can point Errorta at it instead. Do this
in **Settings › Knowledge & connections**:

- **AIAR connection** — the canonical AIAR runtime used for Judge / Knowledge /
  Council / Coding. Choose local (this machine) or a remote AIAR service.
- **Remote AIAR tunnel** — connection details for a remote AIAR service: the
  server **URL**, a **bearer token** (if the server requires one), a request
  **timeout**, whether to **verify TLS**, and a **reconnect** control. Example
  URL shape: `https://remote.example.com:8766`.

Errorta stores these settings locally; the token is only sent to the AIAR server
you configured.

## Data residency — where your calls run

Where each request originates is a separate, explicit choice in **Settings › Data
residency**:

- **Local** — the sidecar on this machine originates every call and holds your
  keys.
- **SSH-remote** — Errorta runs the sidecar on a remote host over SSH; that host
  originates the calls.
- **Hosted** — a hosted sidecar originates the calls; your desktop only holds the
  access token.

The rule in every mode: **the active sidecar originates every model call and
holds the keys** — the desktop never fans keys out to multiple destinations. Pick
the residency that matches where you want your data and compute to live.

## Your first corpus

AIAR answers are only as good as what you give it. To get started:

1. Open **Knowledge**.
2. Drag in some documents (PDF, DOCX, XLSX, PPTX, HTML, or plain text), or install
   the small starter corpus offered there.
3. Ask a question — you'll get an answer **with a verdict**, and any correction
   you accept is remembered for next time.

## Summary

- **Onboarding** = connect your models. That's it.
- **AIAR** = the retrieval layer, set up in **Settings** (local by default).
- **Knowledge** = where you add documents and ask grounded questions.
