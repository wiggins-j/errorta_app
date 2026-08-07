# Driving Errorta from an AI agent — a headless runbook

**Audience:** an autonomous agent (or a human) driving the `errorta` CLI
headlessly, with no desktop GUI. If you are an LLM operator pointed at this
repo and asked to "run the coding council," read this file **first** — it is
the correct sequence and the non-obvious gotchas, in order. Reading it up
front is one pass; discovering it by reverse-engineering the source is not.

`docs/CLI.md` is the exhaustive command reference. This file is the **runbook**:
the happy path, plus the handful of things that are easy to get wrong because
they live in config files or default off.

---

## The golden path (existing repo → shipped tasks)

Every mutating command in headless mode needs `--yes` (there is no interactive
prompt to accept). Run these from inside the repo you want the team to work on.

```sh
# 0. Sidecar + providers are up. `connected: yes` is what you want on your model.
errorta status
errorta connect                      # claude_cli "connected yes" needs no API key

# 1. Bind the repo. For an EXISTING codebase use `import local`, not `new`.
errorta import local . --id myproj --yes

# 2. *** SET AUTONOMY BEFORE THE FIRST RUN ***  (see "Gotcha 1" — this is THE step)
#    Edit ~/.errorta/council/coding-projects/<id>/autonomy.json:
#      "dev_repo_read": true, "reviewer_repo_read": true
#    Without this the team CANNOT read your existing code and will reinvent it.

# 3. North Star + Definition of Done. This is the durable steering — put the
#    current state and hard constraints here, not just a one-line vision.
errorta north-star set \
  --north-star "What the project is + what is ALREADY DONE + what remains" \
  --dod "The exact, checkable conditions for 'finished'" --yes

# 4. Assemble the team, one role per line, on the routes you want, then apply.
errorta models                       # list available routes (e.g. claude_cli.opus)
errorta team add --pm       claude_cli.opus
errorta team add --dev      claude_cli.opus --count 3
errorta team add --reviewer claude_cli.opus
errorta team add --tester   claude_cli.opus
errorta team apply --yes

# 5. Preflight, then launch. `run` streams the live view until the run ends,
#    but the run itself lives in the sidecar — background the client and the
#    run keeps going; re-attach with the watch commands below.
errorta setup                        # readiness gate ("run setup: confirmed")
errorta run --yes &                  # background the streaming client

# 6. Watch and steer (all read-only except interject/cancel/accept).
errorta status                       # sidecar + run state
errorta board                        # todo / doing / blocked / done
errorta log                          # narrative team log (add --watch to tail)
errorta decisions                    # the decision event stream
errorta tokens                       # spend by role / route
errorta prs ; errorta diff --full    # the delivered code (staged, not your files)
errorta interject "<directive>" --yes  # authoritative course-correction, mid-run
errorta cancel --yes                 # stop at the next turn boundary
```

For a **greenfield** project, replace steps 1–2 with `errorta new <id>` and
`errorta team add ... --default` (1 PM / 3 dev / 1 reviewer / 1 tester with
auto-chosen models); autonomy defaults are usually fine because there is no
existing code to read.

---

## Gotchas that will cost you an hour if you skip them

**1. `dev_repo_read` / `reviewer_repo_read` default to `false` and are NOT
exposed as CLI flags.** They are persisted fields in
`~/.errorta/council/coding-projects/<id>/autonomy.json` (normally toggled in
the desktop Settings). For an **existing repo this is the single most important
switch**: with it off, dev turns cannot `Read`/`Grep`/`Glob` the worktree, so
the PM plans as if the repo were empty and the team **reinvents and duplicates
work that already exists**. The tell in the logs is
`capability_ask (dev): repo_read` followed by the PM blocking the task. Set both
to `true` *before* the first `run`, then start the run so the fresh worker loads
the new policy. (See `docs/coding/PM_REFERENCE.md` "Spec 11 / Spec 14" and
`docs/specs/SPEC-15-capability-aware-planning.md`.)

**2. AIAR is optional; a stale remote pointer throws a scary error.** AIAR (the
retrieval/RAG layer) runs **in-process and local by default — nothing to set up**,
and the coding council runs fine **without any RAG at all**. If a leftover
`~/.errorta/remote-aiar.json` points at an unreachable server you will see
`remote AIAR unreachable: Connection refused` from `grounding build-from-project`.
That failure is a **no-op you can ignore** for a coding run — the North Star plus
`repo_read` are what steer the team. Don't chase it. (See `docs/AIAR_SETUP.md`.)

**3. `grounding build-from-project` needs a worktree that only exists after the
first run.** So either run first and build grounding after, or skip grounding
entirely — for continuing an existing repo, `repo_read` + a good North Star are
sufficient and simpler.

**4. Everything mutating needs `--yes` headlessly.** `import`, `north-star set`,
`team apply`, `interject`, `cancel`, `delete`, `governance settings` — all refuse
without it and print a one-line usage instead. That refusal is not an error to
debug; it's the confirmation gate.

**5. The team's work is staged, not applied to your files.** Errorta uses a
delivered-tree model: PRs and diffs live under `~/.errorta/council/apply-workspaces/`
and reach your real repo only when you `errorta accept` (merge-back) or
`errorta publish`. Your working tree and branches are safe until then — review
`errorta diff --full` and only accept what is correct.

**6. Model routing is per-role.** `errorta models` lists routes as
`<provider>.<model>` (e.g. `claude_cli.opus` = Opus via a Claude subscription,
no API key; `cursor_cli.composer-2.5`; a local `ollama.<model>`). Assign the
cheap/local tier to routine roles and the strong tier where it earns its keep —
`team set <role> <route>` overrides any single role.

---

## Minimal end-to-end (copy/paste, existing repo, all-Opus team)

```sh
cd /path/to/your/repo
errorta import local . --id myproj --yes
python3 - <<'PY'                      # flip repo_read on before the first run
import json, pathlib, os
p = pathlib.Path.home()/".errorta/council/coding-projects/myproj/autonomy.json"
d = json.loads(p.read_text()); d["dev_repo_read"] = True; d["reviewer_repo_read"] = True
p.write_text(json.dumps(d)); print("repo_read on")
PY
errorta north-star set --north-star "…state + what's done + what remains…" \
                       --dod "…checkable done conditions…" --yes
errorta team add --pm claude_cli.opus
errorta team add --dev claude_cli.opus --count 3
errorta team add --reviewer claude_cli.opus
errorta team add --tester claude_cli.opus
errorta team apply --yes
errorta setup && errorta run --yes
```

---

## If you are an AI operator watching a run

- Poll `status` / `board` / `log` / `decisions` / `tokens` on an interval.
- Verify the delivered diff (`errorta prs`, `errorta diff --full`) against the
  project's own spec/plan before you ever `accept`. Never auto-accept.
- Steer with `interject "<directive>" --yes`; the PM reads it on its next plan
  turn. Reserve `cancel` for a run that is clearly burning budget on wrong work.
- The North Star is durable and survives a cancel/re-run; use it to encode
  "what is already done" so a re-plan doesn't duplicate finished work.
