# Fix-loop convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a live-run fix cycle on an `existing`-target project able to converge: an opus dev seat with an 80-turn / 1500 s repository-read turn, triage keyed on probe kind, a self-seeding worktree, and an honest `window_shot` diagnostic.

**Architecture:** All changes are additive seams on the existing `errorta_liverun` state machines (`FixDeps` callables with lazy production defaults; `EvidenceBundle` gains one field) plus two operator-tunable constants in the Claude CLI provider. Nothing touches the merge gate, guarded paths, caps direction, or human-only verbs.

**Tech Stack:** Python 3.12, pytest (`cd python && ./.venv/bin/python -m pytest`), dataclasses, no new runtime deps except `pyobjc-framework-Quartz` on macOS.

**Spec:** `docs/superpowers/specs/2026-08-23-fixloop-convergence-design.md`

## Global Constraints

- Repo is PUBLIC: no tokens, hostnames-with-secrets, PII, or personal absolute paths in committed code/tests (use `/r/...` style fakes as the existing tests do).
- Caps and timeouts in profiles may only be **lowered** below shipped defaults; defaults live in code.
- New pause codes are not introduced; new failures reuse `fix_run_failed` with a specific `detail`.
- Run tests from `python/`: `./.venv/bin/python -m pytest tests/liverun -q` (and the named files below). All must pass before each commit.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Do not edit `~/.errorta/**` (operator files) from a task; the plan notes where the operator profile changes.

---

### Task 1: Triage classifies by probe kind

**Files:**
- Modify: `python/errorta_liverun/brief.py:51-61` (`EvidenceBundle`)
- Modify: `python/errorta_liverun/supervisor.py:520-530` (`_fix_bundle`)
- Modify: `python/errorta_liverun/triage.py:30-90`
- Test: `python/tests/liverun/test_triage.py`, `python/tests/liverun/test_supervisor.py`

**Interfaces:**
- Produces: `EvidenceBundle.stalled_probe_kind: str | None` (default `None`); `triage._stall_kind(bundle) -> str | None`.

- [ ] **Step 1: Write the failing triage tests** — append to `python/tests/liverun/test_triage.py`:

```python
def test_kind_classifies_a_renamed_journal_probe() -> None:
    res = classify(_bundle(stop_reason="stall:j", stalled_probe_id="j",
                           stalled_probe_kind="remote_stdout_advancing"), _profile())
    assert res.repo_id == "brain" and "journal_stall" in res.classes


@pytest.mark.parametrize("kind,cls", [
    ("remote_pid_alive", "brain_pid_dead"),
    ("remote_file_mtime_advancing", "brain_log_stall"),
    ("remote_stdout_advancing", "journal_stall"),
    ("remote_stdout_matches", "journal_stall"),
    ("http", "client_port_dead"),
])
def test_each_probe_kind_maps_to_one_stall_class(kind: str, cls: str) -> None:
    res = classify(_bundle(stop_reason="stall:x", stalled_probe_id="x",
                           stalled_probe_kind=kind), _profile())
    assert cls in res.classes


def test_session_clock_kind_is_not_a_defect() -> None:
    res = classify(_bundle(stop_reason="stall:clock", stalled_probe_id="clock",
                           stalled_probe_kind="elapsed_lt_s"), _profile())
    assert res.classes == () and res.repo_id is None


def test_kind_wins_over_a_misleading_legacy_id() -> None:
    # The id says brain-log; the probe is an http probe on the client port.
    res = classify(_bundle(stop_reason="stall:brain-log", stalled_probe_id="brain-log",
                           stalled_probe_kind="http"), _profile())
    assert "client_port_dead" in res.classes and "brain_log_stall" not in res.classes


def test_legacy_ids_still_classify_when_no_kind_is_known() -> None:
    res = classify(_bundle(stop_reason="stall:journal-seq", stalled_probe_id="journal-seq"),
                   _profile())
    assert "journal_stall" in res.classes
```

- [ ] **Step 2: Run them** — `./.venv/bin/python -m pytest tests/liverun/test_triage.py -q`. Expected: FAIL with `TypeError: ... unexpected keyword argument 'stalled_probe_kind'`.

- [ ] **Step 3: Add the field** — in `python/errorta_liverun/brief.py`, inside `EvidenceBundle` after `stalled_probe_id: str | None = None`:

```python
    #: The stalled probe's `Probe.kind` (profile.PROBE_KINDS), resolved by the
    #: supervisor from the profile. Triage keys on this, never on the id an
    #: operator happened to choose; None when the bundle has no profile context.
    stalled_probe_kind: str | None = None
```

- [ ] **Step 4: Classify by kind** — in `python/errorta_liverun/triage.py` replace the `_JOURNAL_REASONS` line and the stop-reason-based signatures:

```python
# Legacy ids -> kinds, used ONLY when a bundle carries no `stalled_probe_kind`
# (profiles from before triage keyed on kind, and bundles built without one).
_LEGACY_ID_KINDS = {
    "brain-alive": "remote_pid_alive",
    "brain-log": "remote_file_mtime_advancing",
    "journal-seq": "remote_stdout_advancing",
    "feed-live": "remote_stdout_matches",
    "client-state": "http",
}
_JOURNAL_KINDS = ("remote_stdout_advancing", "remote_stdout_matches")


def _stall_kind(bundle: EvidenceBundle) -> str | None:
    """The kind of the probe that stalled: the bundle's own, else the kind the
    legacy id implies, else None. `elapsed_lt_s` is a kind too -- and maps to
    no class below, because a session clock running out is not a defect."""
    if not str(bundle.stop_reason or "").startswith("stall:"):
        return None
    if bundle.stalled_probe_kind:
        return str(bundle.stalled_probe_kind)
    probe_id = bundle.stalled_probe_id or bundle.stop_reason.split(":", 1)[1]
    return _LEGACY_ID_KINDS.get(probe_id)


def _brain_pid_dead(bundle: EvidenceBundle) -> bool:
    return _stall_kind(bundle) == "remote_pid_alive"
```

and in `_SIGNATURES`:

```python
    "brain_log_stall": lambda b: _stall_kind(b) == "remote_file_mtime_advancing",
    "journal_stall": lambda b: _stall_kind(b) in _JOURNAL_KINDS,
    "brain_pid_dead": _brain_pid_dead,
    ...
    "client_port_dead": lambda b: (_stall_kind(b) == "http" and not _brain_pid_dead(b)),
```

Update the module docstring's sentence about signatures to say they key on probe **kind**.

- [ ] **Step 5: Run triage tests** — `./.venv/bin/python -m pytest tests/liverun/test_triage.py -q`. Expected: all PASS (including the pre-existing id-based ones, via the legacy table).

- [ ] **Step 6: Write the failing supervisor test** — append to `python/tests/liverun/test_supervisor.py`:

```python
def test_fix_bundle_carries_the_stalled_probe_kind() -> None:
    clock = FakeClock()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True)
    sup.start(blocking=False)
    assert sup._fix_bundle("stall:alive").stalled_probe_kind == "http"
    assert sup._fix_bundle("stall:clock").stalled_probe_kind == "elapsed_lt_s"
    assert sup._fix_bundle("stall:no-such-probe").stalled_probe_kind is None
    assert sup._fix_bundle("launch_step_failed:one").stalled_probe_kind is None
```

(If `sup.state` is only created on the first tick in this test file's pattern, call `sup._tick()` once after `start` — look at `test_happy_launch_then_stall_tears_down_with_literal` for the established drive.)

- [ ] **Step 7: Run it** — expected FAIL: `stalled_probe_kind` is `None` for `"stall:alive"`.

- [ ] **Step 8: Resolve the kind in `_fix_bundle`** — `python/errorta_liverun/supervisor.py`:

```python
        probe_id = reason.split(":", 1)[1] if reason.startswith("stall:") else None
        probe_kind = next((w.probe.kind for w in self.profile.watch if w.id == probe_id), None) \
            if probe_id else None
        ...
            stalled_probe_id=probe_id, stalled_probe_kind=probe_kind, stalled_s=self._stalled_s,
```

- [ ] **Step 9: Run the liverun suite** — `./.venv/bin/python -m pytest tests/liverun -q`. Expected: PASS.

- [ ] **Step 10: Docs** — in `docs/liverun/README.md` rewrite item 8 of "what the first live runs settled" to: "**Probe kinds drive triage.** The deterministic classifier keys on the stalled probe's *kind* (`remote_pid_alive` → `brain_pid_dead`, `remote_file_mtime_advancing` → `brain_log_stall`, `remote_stdout_advancing`/`remote_stdout_matches` → `journal_stall`, `http` → `client_port_dead`, `elapsed_lt_s` → nothing); name probes however you like." Delete the "Triage should classify by probe kind" follow-up bullet.

- [ ] **Step 11: Commit**

```bash
git add python/errorta_liverun/brief.py python/errorta_liverun/supervisor.py python/errorta_liverun/triage.py python/tests/liverun/test_triage.py python/tests/liverun/test_supervisor.py docs/liverun/README.md
git commit -m "feat(liverun): triage classifies by probe kind, not probe id

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Repository-read budget 80 turns and a 1500 s read turn

**Files:**
- Modify: `python/errorta_model_gateway/providers/async_claude_cli.py:85-115, 310-345`
- Test: `python/tests/test_async_claude_cli.py`

**Interfaces:**
- Produces: `_repo_read_timeout_s() -> int`, module constant `_REPO_READ_TIMEOUT_S`; env `ERRORTA_REPO_READ_TIMEOUT_S` (default 1500, clamp 600..3000); `_repo_read_max_turns()` default 80.

- [ ] **Step 1: Update and add tests** — in `python/tests/test_async_claude_cli.py` change both `== 48` assertions (`test_dev_repo_read_turn_budget_is_raised`, `test_retrieval_argv_carries_the_raised_budget`) to `== 80`, change `test_repo_read_budget_is_operator_tunable`'s `"garbage"` expectation to `80`, and append:

```python
def test_repo_read_timeout_is_operator_tunable(monkeypatch):
    from errorta_model_gateway.providers import async_claude_cli as mod
    monkeypatch.delenv("ERRORTA_REPO_READ_TIMEOUT_S", raising=False)
    assert mod._repo_read_timeout_s() == 1500
    monkeypatch.setenv("ERRORTA_REPO_READ_TIMEOUT_S", "2000")
    assert mod._repo_read_timeout_s() == 2000
    monkeypatch.setenv("ERRORTA_REPO_READ_TIMEOUT_S", "10")
    assert mod._repo_read_timeout_s() == 600
    monkeypatch.setenv("ERRORTA_REPO_READ_TIMEOUT_S", "garbage")
    assert mod._repo_read_timeout_s() == 1500
    monkeypatch.setenv("ERRORTA_REPO_READ_TIMEOUT_S", "99999")
    assert mod._repo_read_timeout_s() == 3000


@pytest.mark.asyncio
async def test_only_the_retrieval_attempt_gets_the_raised_timeout(monkeypatch, tmp_path):
    """The read-only worktree turn is where the budget is spent; the plain
    fallback has no repository to read and keeps the request's own timeout."""
    import errorta_model_gateway.providers.async_claude_cli as mod
    seen: list[tuple[str | None, int]] = []

    async def fake_run(*, argv, prompt, timeout_seconds, semaphore, error_prefix,
                       cwd_prefix, cwd_override=None):
        seen.append((cwd_override, timeout_seconds))
        return (_empty_json() if cwd_override else _ok_json()), "", 0

    monkeypatch.setattr(mod, "run_cli_subprocess", fake_run)
    await ClaudeCliHandler().call(_req_with_worktree(tmp_path), api_key=None)
    assert len(seen) == 2
    assert seen[0][0] == str(tmp_path) and seen[0][1] == mod._REPO_READ_TIMEOUT_S == 1500
    assert seen[1][0] is None and seen[1][1] == 30
```

(`_req_with_worktree` builds a request with `timeout_seconds=30`; `_empty_json()`/`_ok_json()` are the file's existing helpers. Check the exact keyword names `run_cli_subprocess` is called with at lines ~335-343 and mirror them in `fake_run`.)

- [ ] **Step 2: Run** — `./.venv/bin/python -m pytest tests/test_async_claude_cli.py -q`. Expected: the changed/new tests FAIL (48 ≠ 80; no `_repo_read_timeout_s`).

- [ ] **Step 3: Implement** — in `async_claude_cli.py`: change the two `48` literals in `_repo_read_max_turns` to `80` and extend the comment block: "Live 2026-08-23: 48 was still not enough for an opus dev on the same repo; 80." Add after `_DEV_REPO_READ_MAX_TURNS`:

```python
# The read-only worktree turn is the one that spends the budget above, and the
# default 600 s per-turn timeout (reasoning_budget.py) was sized for a turn with
# no retrieval. ``ERRORTA_REPO_READ_TIMEOUT_S`` raises the RETRIEVAL attempt
# only; the plain fallback has no repository to read. Snapshotted at import like
# the turn budget: set it on the invocation that spawns the sidecar.
def _repo_read_timeout_s() -> int:
    raw = os.environ.get("ERRORTA_REPO_READ_TIMEOUT_S", "").strip()
    try:
        v = int(raw) if raw else 1500
    except ValueError:
        v = 1500
    return max(600, min(v, 3000))


_REPO_READ_TIMEOUT_S = _repo_read_timeout_s()
```

and in the attempt loop pass `timeout_seconds=(max(int(request.timeout_seconds), _REPO_READ_TIMEOUT_S) if cwd_override is not None else request.timeout_seconds)`.

- [ ] **Step 4: Run** — `./.venv/bin/python -m pytest tests/test_async_claude_cli.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_model_gateway/providers/async_claude_cli.py python/tests/test_async_claude_cli.py
git commit -m "feat(gateway): repo-read turn budget 80 and a 1500 s retrieval timeout (ERRORTA_REPO_READ_TIMEOUT_S)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Profile `fix_loop.dev_route` and the idle budget that follows the read turn

**Files:**
- Modify: `python/errorta_liverun/profile.py:70-76, 157-163, 499-532`
- Modify: `python/errorta_liverun/fixloop.py:51`
- Test: `python/tests/liverun/test_profile.py`

**Interfaces:**
- Produces: `FixLoop.dev_route: str = "claude_cli.opus"`; `FIX_CAP_DEFAULTS["idle_timeout_s"] == 2400`; `MIN_IDLE_TIMEOUT_S == 1500`; `fixloop.DEFAULT_IDLE_TIMEOUT_S == 2400.0`.

- [ ] **Step 1: Write failing tests** — append to `python/tests/liverun/test_profile.py` (use the file's existing helper that loads a fix-loop-enabled profile dict — grep `fix_loop` in that file for the helper name; below it is called `_fix_profile_dict()` and `_load(d)`; adapt the names to the file's):

```python
def test_fix_loop_dev_route_defaults_to_opus() -> None:
    prof = _load(_fix_profile_dict())
    assert prof.fix_loop.dev_route == "claude_cli.opus"


def test_fix_loop_dev_route_is_declarable_and_validated() -> None:
    d = _fix_profile_dict()
    d["fix_loop"]["dev_route"] = "claude_cli.sonnet"
    assert _load(d).fix_loop.dev_route == "claude_cli.sonnet"
    for bad in ("", "opus", "claude_cli.", "Claude_CLI.opus", "a.b;c", 7):
        d["fix_loop"]["dev_route"] = bad
        with pytest.raises(P.ProfileError) as ei:
            _load(d)
        assert ei.value.code == "bad_dev_route"


def test_fix_loop_idle_floor_follows_the_repo_read_turn() -> None:
    assert P.FIX_CAP_DEFAULTS["idle_timeout_s"] == 2400
    assert P.MIN_IDLE_TIMEOUT_S == 1500
    d = _fix_profile_dict()
    d["fix_loop"]["idle_timeout_s"] = 1500
    with pytest.raises(P.ProfileError) as ei:
        _load(d)
    assert ei.value.code == "idle_below_turn_timeout"
    d["fix_loop"]["idle_timeout_s"] = 2400
    assert _load(d).fix_loop.idle_timeout_s == 2400
```

If `ProfileError` exposes the code differently (check `class ProfileError` in `profile.py`), match its attribute.

- [ ] **Step 2: Run** — `./.venv/bin/python -m pytest tests/liverun/test_profile.py -q`. Expected: FAIL (`unknown_key` for `dev_route`; `1200 != 2400`).

- [ ] **Step 3: Implement** in `profile.py`:

```python
FIX_CAP_DEFAULTS = {"max_fix_cycles_per_day": 3, "idle_timeout_s": 2400,
                    "accept_timeout_s": 1800}
# A single repository-read dev turn may run for ERRORTA_REPO_READ_TIMEOUT_S
# (async_claude_cli.py, default 1500 s); the idle detector must outlast it.
MIN_IDLE_TIMEOUT_S = 1500
...
FIX_LOOP_KEYS = {"enabled", "triage_route", "dev_route"} | set(FIX_CAP_DEFAULTS)
_DEV_ROUTE_RE = re.compile(r"^[a-z_]+\.[a-z0-9][a-z0-9_.-]*$")
```

`FixLoop`: `idle_timeout_s: int = 2400` and add `dev_route: str = "claude_cli.opus"`. In `_fix_loop`, after the triage-route check:

```python
    dev_route = raw.get("dev_route", "claude_cli.opus")
    if not isinstance(dev_route, str) or not _DEV_ROUTE_RE.match(dev_route):
        raise ProfileError("bad_dev_route", repr(dev_route)[:80])
    ...
    return FixLoop(enabled=enabled, triage_route=str(route), dev_route=dev_route, **values)
```

In `fixloop.py`: `DEFAULT_IDLE_TIMEOUT_S = 2400.0` and update its comment to cite the 1500 s read turn. Fix any existing test in `tests/liverun/` that asserted the old 1200/600 literals (grep `1200` and `600` in `test_profile.py`/`test_fixloop.py`; keep their intent, update the numbers).

- [ ] **Step 4: Run** — `./.venv/bin/python -m pytest tests/liverun -q`. Expected: PASS.

- [ ] **Step 5: Docs** — `docs/liverun/README.md`, fix-loop section "Caps:" paragraph: mention `idle_timeout_s` default 2400 (floor 1500, the repository-read turn), and add a sentence under "Operator setup": "`fix_loop.dev_route` (default `claude_cli.opus`) is the route the cycle seats the dev role on before it files the task; it must be an available gateway route." Note for the operator (not a task action): `~/.errorta/liverun/profiles/osrs.yaml` must be edited `idle_timeout_s: 1200` → `2400` or it will fail `idle_below_turn_timeout`.

- [ ] **Step 6: Commit**

```bash
git add python/errorta_liverun/profile.py python/errorta_liverun/fixloop.py python/tests/liverun docs/liverun/README.md
git commit -m "feat(liverun): fix_loop.dev_route; idle budget outlasts the repo-read turn

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: The cycle seats the dev on `dev_route` before filing the task

**Files:**
- Modify: `python/errorta_liverun/fixloop.py` (`FixDeps` ~255-320; `_do_triage` ~476-528)
- Test: `python/tests/liverun/test_fixloop.py`

**Interfaces:**
- Consumes: `profile.fix_loop.dev_route` (Task 3).
- Produces: `FixDeps.assign_dev_route_fn: Callable[[str, str], list[str]] | None`, `FixDeps.assign_dev_route(project_id, route) -> list[str]` (prior dev routes; empty list when nothing changed), event `fix_team_model {project_id, role, from, to}`.

- [ ] **Step 1: Extend the test fake and write failing tests** — in `python/tests/liverun/test_fixloop.py`, add to `Fake.__init__`: `self.dev_routes: list[str] = ["claude_cli.sonnet"]`, `self.assigned: list[tuple[str, str]] = []`, `self.assign_raises: Exception | None = None`; add the method:

```python
    def _assign_dev_route(self, project_id: str, route: str) -> list[str]:
        if self.assign_raises is not None:
            raise self.assign_raises
        prior = [r for r in self.dev_routes if r != route]
        self.assigned.append((project_id, route))
        self.dev_routes = [route for _ in self.dev_routes]
        return prior
```

and `assign_dev_route_fn=self._assign_dev_route,` in `Fake.deps()`. Then append tests:

```python
def test_dev_seat_is_moved_to_the_profile_route_before_the_task_is_filed() -> None:
    fake = Fake()
    out = _drive(_cycle(fake), fake)
    assert fake.assigned == [("senditai-ng", "claude_cli.opus")]
    kinds = [k for k, _ in out.events]
    assert kinds.index("fix_team_model") < kinds.index("fix_task")
    ev = dict(out.events)["fix_team_model"]
    assert ev == {"project_id": "senditai-ng", "role": "dev",
                  "from": ["claude_cli.sonnet"], "to": "claude_cli.opus"}


def test_dev_seat_already_on_the_route_is_left_alone() -> None:
    fake = Fake()
    fake.dev_routes = ["claude_cli.opus", "claude_cli.opus"]
    out = _drive(_cycle(fake), fake)
    assert fake.assigned == [("senditai-ng", "claude_cli.opus")]  # asked once
    assert "fix_team_model" not in [k for k, _ in out.events]


def test_unavailable_dev_route_pauses_before_any_run() -> None:
    fake = Fake()
    fake.assign_raises = RuntimeError("model_not_found")
    out = _drive(_cycle(fake), fake)
    assert out.kind == "paused" and out.code == "fix_run_failed"
    assert out.detail.startswith("dev_route_unavailable:claude_cli.opus")
    assert fake.started == [] and fake.store.tasks == []
```

(The profile's dev `errorta_project` in `_profile()` is `senditai-ng`.) Check how existing tests read `out.code`/`out.detail` on a paused `FixOutcome` and match that.

- [ ] **Step 2: Run** — `./.venv/bin/python -m pytest tests/liverun/test_fixloop.py -q -k dev_seat_or_dev_route`. Expected: FAIL (`FixDeps` has no `assign_dev_route_fn`).

- [ ] **Step 3: Implement the seam** in `fixloop.py` — `FixDeps` field + method:

```python
    assign_dev_route_fn: Callable[[str, str], list[str]] | None = None
    ...
    def assign_dev_route(self, project_id: str, route: str) -> list[str]:
        """Seat every `dev` member on `route`. Returns the routes it replaced
        (empty when every dev seat already sat there)."""
        return list((self.assign_dev_route_fn or _default_assign_dev_route)(project_id, route) or [])
```

Production default (next to the other `_default_*` functions):

```python
def _default_assign_dev_route(project_id: str, route: str) -> list[str]:
    from errorta_council.coding import control_actions, pm_reference
    from errorta_council.coding.ledger import LedgerStore
    store = LedgerStore(project_id)
    members = [m for m in (store.get_run_config().get("members") or []) if isinstance(m, dict)]
    prior = [str(m.get("gateway_route_id") or "") for m in members
             if control_actions.coding_role_of(m) == "dev"
             and str(m.get("gateway_route_id") or "") != route]
    if not prior:
        return []
    control_actions.assign_models_by_role(
        store, {"dev": route}, available=pm_reference.list_available_routes(),
        surface="liverun")
    return prior
```

(`coding_role_of` is imported in `control_actions`; if it is not re-exported, import it from where `control_actions` does.)

- [ ] **Step 4: Wire `_do_triage`** — after the `gate_available` check and before the `status == "running"` check:

```python
        route = str(getattr(getattr(self.profile, "fix_loop", None), "dev_route", "") or "")
        if route:
            try:
                prior = self.deps.assign_dev_route(project_id, route)
            except Exception as exc:  # noqa: BLE001 - a seat that cannot be filled is a pause
                _LOG.exception("liverun %s could not seat the dev on %s", self.run_id, route)
                return self._pause("fix_run_failed", failed=False,
                                   detail=f"dev_route_unavailable:{route}:{type(exc).__name__}")
            if prior:
                self._event("fix_team_model", {"project_id": project_id, "role": "dev",
                                               "from": prior, "to": route})
```

- [ ] **Step 5: Run** — `./.venv/bin/python -m pytest tests/liverun -q`. Expected: PASS (existing tests construct `FixLoop(enabled=True)` whose `dev_route` defaults to opus; the fake's default `dev_routes` makes the happy path emit one extra event — if any existing test asserts an exact event list, add `fix_team_model` to it).

- [ ] **Step 6: Slack narration** — grep `errorta_slack` for where fix-loop event kinds are rendered (e.g. `fix_task`, `fix_run` in a line-formatter table). Add `fix_team_model` → `"dev seat → {to} (was {from})"`. Add one test beside the existing formatter tests asserting that line.

- [ ] **Step 7: Commit**

```bash
git add python/errorta_liverun/fixloop.py python/tests/liverun/test_fixloop.py python/errorta_slack python/tests
git commit -m "feat(liverun): fix cycle seats the dev role on fix_loop.dev_route

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: The cycle seeds a missing worktree and keeps the failure reason

**Files:**
- Modify: `python/errorta_liverun/fixloop.py` (`FixDeps`; `_do_triage` workspace block)
- Test: `python/tests/liverun/test_fixloop.py`

**Interfaces:**
- Produces: `FixDeps.seed_workspace_fn: Callable[[str], bool] | None`, `FixDeps.seed_workspace(project_id) -> bool`, event `fix_workspace_seeded {project_id, repo_path}`.

- [ ] **Step 1: Extend the fake and write failing tests** — `Fake.__init__`: `self.seed_result = False`, `self.seeded: list[str] = []`, `self.seed_raises: Exception | None = None`, `self.ws_raises: Exception | None = None`; methods:

```python
    def _seed(self, project_id: str) -> bool:
        if self.seed_raises is not None:
            raise self.seed_raises
        self.seeded.append(project_id)
        return self.seed_result

    def _workspace(self, project_id: str):
        if self.ws_raises is not None:
            raise self.ws_raises
        return self.ws
```

In `deps()`: `workspace_factory=self._workspace,` and `seed_workspace_fn=self._seed,`. Tests:

```python
def test_missing_worktree_is_seeded_before_the_workspace_opens() -> None:
    fake = Fake()
    fake.seed_result = True
    out = _drive(_cycle(fake), fake)
    assert fake.seeded == ["senditai-ng"]
    assert dict(out.events)["fix_workspace_seeded"] == {
        "project_id": "senditai-ng", "repo_path": "/r/senditai-ng"}


def test_existing_worktree_is_not_reseeded_and_not_announced() -> None:
    fake = Fake()
    out = _drive(_cycle(fake), fake)
    assert fake.seeded == ["senditai-ng"]
    assert "fix_workspace_seeded" not in [k for k, _ in out.events]


def test_seed_failure_pauses_with_the_exception_named() -> None:
    fake = Fake()
    fake.seed_raises = ValueError("existing target needs a valid repo_path")
    out = _drive(_cycle(fake), fake)
    assert out.kind == "paused" and out.code == "fix_run_failed"
    assert out.detail == "seed:ValueError"
    assert fake.started == []


def test_workspace_failure_keeps_its_message() -> None:
    fake = Fake()
    fake.ws_raises = RuntimeError("no worktree for this project yet")
    out = _drive(_cycle(fake), fake)
    assert out.kind == "paused" and out.code == "fix_run_failed"
    assert out.detail == "RuntimeError:no worktree for this project yet"
```

- [ ] **Step 2: Run** — expected FAIL (`seed_workspace_fn` unknown).

- [ ] **Step 3: Implement** — `FixDeps`:

```python
    seed_workspace_fn: Callable[[str], bool] | None = None
    ...
    def seed_workspace(self, project_id: str) -> bool:
        """Create the project's worktree if there is none. True when it did."""
        return bool((self.seed_workspace_fn or _default_seed_workspace)(project_id))
```

default:

```python
def _default_seed_workspace(project_id: str) -> bool:
    # `adopt_project` never seeds; the first `errorta run` does, via
    # CodingRunner.__init__. A fix cycle that arrives first must not pause on
    # a missing worktree it could have created (live 2026-08-22, README item 10).
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore(project_id)
    proj = store.get_project()
    ws = CodingWorkspace(project_id, store)
    ws.set_target(proj.target)
    if ws.exists():
        return False          # never re-stamp seed_head on a worked tree
    ws.setup(target=proj.target, repo_path=proj.repo_path)
    return True
```

`_do_triage`, replacing the workspace block:

```python
        try:
            if self.deps.seed_workspace(project_id):
                repo_path = self._safe(
                    lambda: str(getattr(self._store.get_project(), "repo_path", "") or ""),
                    default="")
                self._event("fix_workspace_seeded", {"project_id": project_id,
                                                     "repo_path": repo_path})
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("liverun %s could not seed workspace for %s", self.run_id, project_id)
            return self._pause("fix_run_failed", failed=False, detail=f"seed:{type(exc).__name__}")
        try:
            self._ws = self.deps.workspace(project_id)
            self._head_before = str(self._ws.head() or "")
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("liverun %s could not open workspace for %s", self.run_id, project_id)
            return self._pause("fix_run_failed", failed=False,
                               detail=f"{type(exc).__name__}:{str(exc)[:80]}")
```

Check that `CodingWorkspace.set_target` exists with that name (the route `_workspace` calls it at `routes/coding.py:3883-3894`); if `setup` already sets the target, drop the `set_target` line.

- [ ] **Step 4: Run** — `./.venv/bin/python -m pytest tests/liverun -q`. Expected: PASS. Fix any existing test that asserted `detail == "HTTPException"`-style names by matching the new `Type:message` shape.

- [ ] **Step 5: Slack narration** — add `fix_workspace_seeded` → `"seeded the project worktree from {repo_path}"` in the same formatter as Task 4, with a test.

- [ ] **Step 6: Docs** — README: remove follow-up "The fix cycle should seed a missing project worktree itself (item 10)"; change item 10 to say the cycle seeds it, and that `errorta run` still does too.

- [ ] **Step 7: Commit**

```bash
git add python/errorta_liverun/fixloop.py python/tests/liverun/test_fixloop.py python/errorta_slack python/tests docs/liverun/README.md
git commit -m "feat(liverun): fix cycle seeds a missing worktree; workspace failures keep their reason

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: `window_shot` says which of three things failed, and Quartz is a declared dependency

**Files:**
- Modify: `python/pyproject.toml` dependencies
- Modify: `python/errorta_tools/runner/preview.py:159-184`
- Modify: `python/errorta_liverun/steps.py:428-435`
- Test: `python/tests/liverun/test_steps.py`

**Interfaces:**
- Produces: `preview.quartz_available() -> bool`; `_run_window_shot` details `"quartz_unavailable"`, `"no process matched"`, `"no window captured"`.

- [ ] **Step 1: Write failing tests** — append to `python/tests/liverun/test_steps.py` (find how that file builds a `Ctx` with an `evidence_dir`; below it is `_ctx(tmp_path)` — adapt):

```python
def test_window_shot_names_the_missing_quartz(monkeypatch, tmp_path) -> None:
    from errorta_liverun import steps as S
    monkeypatch.setattr(S, "_pgrep", lambda pattern: [4242])
    monkeypatch.setattr(S, "quartz_available", lambda: False)
    monkeypatch.setattr(S.sys, "platform", "darwin")
    res = S._run_window_shot({"pgrep": "x"}, _ctx(tmp_path), 5)
    assert res.ok is False and res.detail == "quartz_unavailable"


def test_window_shot_names_a_missing_process(monkeypatch, tmp_path) -> None:
    from errorta_liverun import steps as S
    monkeypatch.setattr(S, "_pgrep", lambda pattern: [])
    res = S._run_window_shot({"pgrep": "x"}, _ctx(tmp_path), 5)
    assert res.ok is False and res.detail == "no process matched"


def test_window_shot_reports_an_uncapturable_window(monkeypatch, tmp_path) -> None:
    from errorta_liverun import steps as S
    monkeypatch.setattr(S, "_pgrep", lambda pattern: [4242])
    monkeypatch.setattr(S, "quartz_available", lambda: True)
    monkeypatch.setattr(S, "capture_app_window", lambda *, pids, out_path: False)
    res = S._run_window_shot({"pgrep": "x"}, _ctx(tmp_path), 5)
    assert res.ok is False and res.detail == "no window captured"
```

- [ ] **Step 2: Run** — expected FAIL (`quartz_available` missing on `steps`; details all `"no window captured"`).

- [ ] **Step 3: Implement** — `preview.py`:

```python
_QUARTZ: bool | None = None


def quartz_available() -> bool:
    """Can this process resolve a window id at all? macOS needs pyobjc's
    Quartz; its absence was misread as 'no window' for every live run until
    2026-08-23. Cached: the answer does not change within a process."""
    global _QUARTZ
    if _QUARTZ is None:
        try:
            import Quartz  # type: ignore  # noqa: F401
            _QUARTZ = True
        except Exception:
            _QUARTZ = False
    return _QUARTZ
```

`steps.py`: import `quartz_available` beside `capture_app_window` (`import sys` if absent) and:

```python
def _run_window_shot(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    started_at = now_iso()
    pids = _pgrep(params["pgrep"])
    if not pids:
        return StepResult(False, started_at, now_iso(), detail="no process matched")
    if sys.platform == "darwin" and not quartz_available():
        return StepResult(False, started_at, now_iso(), detail="quartz_unavailable")
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    out = ctx.evidence_dir / f"window-{time.time_ns()}.png"
    ok = capture_app_window(pids=set(pids), out_path=out)
    return StepResult(ok, started_at, now_iso(), evidence_refs=[str(out)] if ok else [],
                      detail="" if ok else "no window captured")
```

`pyproject.toml` dependencies, after `"cryptography>=42",`:

```toml
    # Live-run `window_shot` evidence: resolving an app's own window id on
    # macOS needs Quartz. Without it capture_app_window honestly reports no
    # window -- for every run (live 2026-08-23).
    "pyobjc-framework-Quartz>=10; sys_platform=='darwin'",
```

Then `./.venv/bin/pip install 'pyobjc-framework-Quartz>=10'` in `python/` so the local sidecar has it, and run `./.venv/bin/python -c "import Quartz; print('ok')"`.

- [ ] **Step 4: Run** — `./.venv/bin/python -m pytest tests/liverun/test_steps.py tests/coding/test_f101_03_desktop.py -q`. Expected: PASS.

- [ ] **Step 5: Docs** — README: replace the `window_shot` follow-up bullet with, under "what the first live runs settled": "11. **`window_shot` needs Quartz.** The `pgrep` pattern `RuneLite.app/Contents/MacOS/RuneLite` matches; what was missing was `pyobjc-framework-Quartz` in the sidecar's environment. The step now reports `quartz_unavailable`, `no process matched`, or `no window captured` — three different failures."

- [ ] **Step 6: Commit**

```bash
git add python/pyproject.toml python/errorta_tools/runner/preview.py python/errorta_liverun/steps.py python/tests/liverun/test_steps.py docs/liverun/README.md
git commit -m "fix(liverun): window_shot distinguishes no-Quartz / no-process / no-window; declare pyobjc Quartz on macOS

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: README status

**Files:**
- Modify: `docs/liverun/README.md` "Status (2026-08-22)" section.

- [ ] **Step 1:** Retitle to "Status (2026-08-23)". Add a paragraph after the "Not yet demonstrated" one: the 2026-08-23 run `916bbd` showed the launch chain and teardown again, showed that senditai-ng `9373014` is necessary but not sufficient (the preflight reads a `StateManager` nothing pumps between the feed gate and the loop), and that the journal is legitimately silent for up to 120 + 180 s of preflight and up to 1800 s of a driven tutorial — the operator profile's `journal-seq` threshold is now 2100 s. State that the fix loop is being pointed at that exact stall next and the result (run id, triage, outcome) will be recorded here.
- [ ] **Step 2:** Remove the first follow-up bullet (dev capacity) — it is this slice — and keep the tunnel-registry and `fullmatch` bullets.
- [ ] **Step 3:** Commit: `git commit -am "docs(liverun): status after the 2026-08-23 run and the convergence slice"` (with the Co-Authored-By trailer).

---

## Self-review (done while writing)

- Spec §1a → Tasks 3+4; §1b → Task 2; §1c → Task 3; §2 → Task 1; §3 → Task 5; §4 → Task 6; docs → Tasks 1, 3, 5, 6, 7. Live measurement is run by the session after merge, not a task.
- Names used across tasks: `stalled_probe_kind`, `_stall_kind`, `_repo_read_timeout_s`/`_REPO_READ_TIMEOUT_S`, `FixLoop.dev_route`, `assign_dev_route(_fn)`, `seed_workspace(_fn)`, `quartz_available`, events `fix_team_model`/`fix_workspace_seeded` — consistent.
- No placeholders; where a helper name in an existing test file is uncertain the step says what to grep for.
