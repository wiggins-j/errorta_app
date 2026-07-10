# Built on AIAR

Errorta is the polished desktop app. **AIAR** is the framework
underneath it.

AIAR is the open-source library that does the actual work: hybrid
retrieval (BM25 + vector + reranking), the LLM-as-judge pipeline that
grades every answer, the grounding store that remembers your
corrections, the service API that lets other apps on your machine talk
to a local LLM. AIAR is published under the **Apache-2.0** license
and lives at:

> <https://github.com/wiggins-j/aiar>

You can read every line of code that decides whether Errorta gives
you a good answer or a bad one. Nothing is hidden. Nothing is
proprietary about how the retrieval, judging, or grounding works.

## Why split it?

A framework and a product have different jobs. AIAR's job is to be
clean, composable, and easy to learn from. Errorta's job is to be a
desktop app that you can install and use without writing Python.

Splitting them lets each one be good at its job:

- AIAR stays a focused Python library that other people can adopt,
  fork, and contribute to.
- Errorta stays a polished native experience that turns AIAR into
  something a non-Python user can hand to their team.

## What this means for you

- The interesting parts of Errorta — the judge loop, the grounding
  store, the retrieval pipeline — are all readable, auditable, and
  reusable under Apache-2.0.
- If you want to build something else on top of AIAR, the framework
  is right there. The Errorta source tree shows one good way to use
  it.
- If you find a bug in how Errorta retrieves or judges, the fix
  usually lives in AIAR. We file fixes upstream where they belong.

The local-only promise — your data never leaves the machine — is
identical at both layers. AIAR does not phone home. Errorta does not
phone home.
