# Contributing to Errorta

Thanks for your interest in helping. Errorta is a focused product;
the contribution surface is intentionally narrow but real.

## Dev setup

See [`DEVELOPING.md`](DEVELOPING.md) for the one-stop setup guide
(toolchain prerequisites, the two-terminal local-dev flow,
sidecar lifecycle).

## Verification

Errorta does not run CI on push. **Every contributor runs the
full verification sequence locally before opening a PR.**
GitHub Actions is intentionally OFF in this repo (locked
decision — verification is your responsibility, not the
runner's).

The full local sequence:

```bash
npm run lint
npm run build
( cd src-tauri && cargo check )
( cd python && pytest )
```

If any one of those exits non-zero, the PR is not ready. Don't
suppress, don't `xfail`, don't `// @ts-ignore` — fix the underlying
issue or open an issue first to discuss.

For React component work, also run:

```bash
npm test -- --run
```

## Commit style

Follow the conventions visible in `git log`. One example per type:

```
feat(F001): inline SVG pass-rate chart in MetricsDashboard
fix(F008-bundle): JSON body for /briefs/{id}/export-bundle
chore(a11y): judge feature aria + keyboard nav pass
docs(F009): service API SDK spec (third wedge, post-v1.0)
test(F010): ExportWizard schema verification + happy-path
refactor(F001-SEAM): isolate AIAR boundary behind Pipeline protocol
```

The `Co-Authored-By:` trailer is optional. Use it when an AI agent
or another contributor materially helped on the commit.

## Scope discipline — AIAR vs. Errorta

**Errorta and AIAR are separate projects.** Framework-level
features — the RAG pipeline, the LLM-judge, the grounding store,
hybrid retrieval, the service-API substrate — belong in the AIAR
repo at <https://github.com/wiggins-j/aiar>. Product-level
features — the Tauri shell, hardware scan, drag-and-drop UX, the
polished correction-review surface, the brief-collection UI —
belong here.

A PR that adds an AIAR feature inside Errorta gets redirected to
the AIAR repo. If you're not sure which side a change belongs on,
open an issue here and ask — that's the right move.

## Where to start

The curated entry points carry the
[`good first issue`](https://github.com/wiggins-j/errorta_app/labels/good%20first%20issue)
label. Each one names the exact file you'll touch, a small,
self-contained scope, and a verification command.

If a label-listed issue still looks intimidating, comment on it
and ask — that's the right move, not a sign you shouldn't pick it
up.

## License of contribution

By opening a pull request you agree your contribution is licensed
under the project [LICENSE](LICENSE) (Apache-2.0). No CLA
required.
