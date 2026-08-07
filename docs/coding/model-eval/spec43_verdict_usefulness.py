#!/usr/bin/env python3
"""SPEC-43 — verdict *usefulness* under ``think:false``.

Everything measured before this scored SHAPE (does the JSON parse and match the
verdict schema). Nothing asked whether the verdict was RIGHT. This harness asks
that, against the hand-authored 32-item corpus in ``verdict_corpus/``.

    docs/specs/SPEC-43-verdict-usefulness-under-think-false.md   <- the authority
    docs/coding/model-eval/verdict_corpus/                       <- the corpus

Three arms (§2.1). The gate at ``gateway_local.py:376-380`` is one-directional:
only ``(think on, format on)`` is blocked, so ``(think off, format off)`` is
reachable and is the cell that separates "suppressing reasoning cost us quality"
from "constrained decoding cost us substance".

    T  think on,   no format     control; = F001 arm B
    U  think off,  no format     the missing cell
    S  think off,  format=json   what main ships; = F001 arm C

Each item x arm x 3 trials, ``/api/chat``, num_ctx 8192, num_predict 8192,
temperature 0.3 (production default, ``runner.py:8169``). ``done_reason`` and
``eval_count`` are recorded per call, as in ``f001_judge_retest.py``.

RUN ON senditai ONLY. Nothing in this module touches a model at import time; the
model boundary is three injectable callables (``call``, ``extract_claim``,
``score_claim``) so the whole pipeline is unit-testable with no network. See
``python/tests/coding/test_spec43_harness.py``.

--------------------------------------------------------------------------------
Production envelope (§2.4) — NOT approximated
--------------------------------------------------------------------------------
The reviewer prompt is assembled by calling the real
``errorta_council.coding.runner._review_pr_prompt``, which funnels through
``_review_pr_prompt_segments``. So the model sees the production
``coding_turn.v1`` envelope verbatim: nested ``intent`` with
``kind: "review_verdict"``, the ``reviewed_head`` echo, ``approved``, and
``findings[{severity,title,body}]`` — not the flat toy schema the F001 re-test
used. Verdicts are validated against the same tolerant reader the council uses
(``schemas.ReviewerVerdictIntent`` semantics, reimplemented here without the
pydantic dependency so the harness stays runnable on a bare box).

Three things are *synthesised* rather than taken from a live run, because the
corpus is isolated diffs and there is no council state behind it. Each is held
IDENTICAL across arms and across the twins of a minimal pair, so none of them can
be what an arm difference is measuring:

  1. ``reviewed_head`` — a deterministic ``sha1(item_id)[:12]``. Real heads are
     real hashes; the echo requirement (which §2.4 flags as the materially harder
     part of the envelope) is exercised exactly as in production, and whether the
     arm echoed it correctly is recorded per call as ``head_echo_ok``.
  2. ``project_context`` — a fixed neutral paragraph. Production injects merged
     surface / North Star / grounding; the corpus has none. Held constant.
  3. the reviewer ``Task`` scope — derived from the item's ``file`` only, which is
     identical for both twins of a pair, so the scope text cannot leak which twin
     is the seeded one.

``repo_read`` is False and ``gate_text`` empty: the corpus is isolated diffs
(§5's stated scope limit — this measures the 300-token-diff case, not the
SPEC-32 grounded-review case).

--------------------------------------------------------------------------------
Mechanically blind scoring (§2.3)
--------------------------------------------------------------------------------
Blindness cannot be asserted — arm T emits multi-hundred-token prose and arm S
emits ~91 tokens of flat JSON, so any scorer identifies the arm from the first
line. It is CONSTRUCTED here, in three stages:

  1. ``extract_claim`` reduces one verdict to a NORMALISED CLAIM: defect location
     + mechanism, <=20 words. The word cap is enforced mechanically *after* the
     model returns, so an arm cannot win on length. Findings are ordered by
     severity and only the top 3 are shown to the extractor: that caps the credit
     a shotgun reviewer earns by burying a true claim in a list, without
     punishing a two-finding verdict.
  2. ``build_blind_rows`` shuffles every claim across arms and items into ONE
     flat row list under a recorded seed. The arm/item labels are held
     out-of-band in a separate keymap and joined back only after every row is
     scored. ``BlindRow`` physically does not carry the arm.
  3. ``score_claim`` sees only (claim, ground-truth mechanism,
     accepted_finding_forms, plus the two explicit OTHER-TRUE-POSITIVE
     instructions ``removals.md`` gives for items 003 and 012) and returns
     CATCH / OTHER-TRUE-POSITIVE / MISS.

OTHER-TRUE-POSITIVE is excluded from numerator AND denominator, so a reviewer
that is more correct than the corpus is not scored as a MISS.

Two verdict states are scored MISS mechanically, with no model call, and both are
reported separately so the contamination is visible:
  * unparseable verdicts (§4's last bullet: scored MISS, never dropped, because
    dropping them hands arm T a survivorship bonus);
  * a parseable ``approved: true`` with no findings on a seeded item.

--------------------------------------------------------------------------------
Decision rule (§3), and how §4's branches are selected
--------------------------------------------------------------------------------
"Materially below" is arithmetic, not judgement: a DEEP-SUBSET detection
difference of >=3 items AND a one-sided exact-binomial p <= 0.10 on the
discordant pairs (McNemar, exact, stdlib ``math.comb`` only). ``outcome_branch``
is selected by that arithmetic and written into the results JSON BEFORE any human
writes prose.

Branch order (all §4 bullets; the first two are checked first because §4 says
"before concluding anything"):

  1. ``corpus_at_ceiling_rebuild_deeper``      deep detection > 0.9 on every arm
  2. ``corpus_at_floor_compare_reference``     deep detection < 0.5 on every arm
  3. ``reasoning_suppression_costs_quality``   S<T and U<T
  4. ``format_constraint_not_thinking``        S<U (so U carries the reasoning)
  5. ``per_class_collapse_governs``            parity overall, but a class where
                                               T is 2/2 and S or U is 0/2
  6. ``false_rejection_regression``            detection parity, clean-item
                                               rejection materially worse
  7. ``no_detectable_difference_at_this_power``  a nonzero deep difference below
                                               the §3 threshold
  8. ``think_false_is_free``                   deep counts identical on all three
                                               arms and no false-rejection gap

Two disclosures about this mapping, because inventing arithmetic where the spec
was ambiguous would defeat the point of pre-registering it:

  * §4's bullet 1 ("S ≈ U ≈ T → free") and bullet 5 ("below the threshold → NOT
    a proof of no difference") overlap: read literally, bullet 5 swallows bullet
    1 and ``think_false_is_free`` becomes unreachable. Resolved by reserving
    branch 8 for an EXACTLY zero deep-count difference across all three arms and
    routing every nonzero-but-sub-threshold difference to branch 7. The stricter
    reading is the one that cannot launder a null into a licence.
  * §4's bullets 6 and 7 say "both arms" from a two-arm draft. Generalised to
    three as ALL arms (ceiling = min over arms > 0.9; floor = max over arms
    < 0.5), which is the conservative direction: a corpus is only "too easy" if
    nothing struggles with it.

One branch is NOT in §4: ``unclassified_arm_regression``, emitted when an arm is
materially below control in a pattern §4 did not pre-state (e.g. S<T while U≈T
and S≈U). It is flagged ``in_spec_section_4: false`` so it forces a human read
rather than being silently mislabelled as one of the pre-stated branches.

--------------------------------------------------------------------------------
Corpus integrity gate (§6) — load-bearing
--------------------------------------------------------------------------------
The harness records ``git rev-parse HEAD:docs/coding/model-eval/verdict_corpus``
into the results JSON and REFUSES TO RUN if ``git status --porcelain`` reports
anything under that path. That is what makes "ground truth was authored before
the run" verifiable rather than promised. ``--i-know-the-corpus-is-dirty`` exists
only to make a deliberate override visible in the JSON; it stamps
``corpus_integrity.clean: false`` and forces ``outcome_branch`` to be advisory.

--------------------------------------------------------------------------------
Not implemented here (deliberate, with reasons)
--------------------------------------------------------------------------------
  * §2.3's human double-scoring and the kappa < 0.6 void gate. The harness emits
    the blind 25% re-score sample and provides ``cohens_kappa``; the second
    scorer is a human and runs out of band. ``rescore_triggered`` is computed by
    §3's symmetric trigger.
  * §6's SPEC-41 default-gate reconciliation (a second thinking-capable model).
    That is a spec amendment, not harness code.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from hashlib import sha1
from typing import Any, Callable, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
CORPUS_REL = "docs/coding/model-eval/verdict_corpus"
CORPUS_DIR = os.path.join(REPO_ROOT, CORPUS_REL)
RESULTS_DIR = os.path.join(_HERE, "results")
_PYTHON_PKG_ROOT = os.path.join(REPO_ROOT, "python")

OLLAMA = "http://127.0.0.1:11434"

MODEL_UNDER_TEST = "qwen3.5:9b"
SCORER_MODEL = "gemma3:27b"          # §2.3 — short-text semantic matching
REFERENCE_MODEL = "gemma3:27b"       # §2.3 — difficulty referent, reviewer seat

NUM_CTX = 8192
NUM_PREDICT = 8192
TEMPERATURE = 0.3                    # production default, runner.py:8169
TRIALS = 3                           # §2.4
DEFAULT_SEED = 43

# §3 decision-rule constants. Changing either of these changes what the run means;
# they are named so a diff to them is visible in review.
MIN_ITEM_DIFF = 3
ALPHA = 0.10
CEILING = 0.9
FLOOR = 0.5
RESCORE_FRACTION = 0.25
KAPPA_VOID_THRESHOLD = 0.6

CLAIM_WORD_CAP = 20                  # §2.3
MAX_FINDINGS_TO_EXTRACTOR = 3

_SEVERITY_RANK = {"blocking": 0, "critical": 0, "high": 0, "block": 0,
                  "major": 1, "medium": 1, "moderate": 1,
                  "minor": 2, "low": 2, "nit": 2, "info": 2, "trivial": 2}

# §2.3 / removals.md "Residual uncertainty": two places where the corpus itself
# says a reviewer may be RIGHT about something ground truth does not name. These
# are surfaced to the scorer verbatim so it does not score a correct reviewer as a
# MISS. Keyed by pair_id. `assert_otp_hints_match_removals` checks these are still
# what removals.md says.
OTHER_TRUE_POSITIVE_HINTS: dict[str, str] = {
    "003": ("removals.md: `self._snapshot` is rebound from the refresh thread "
            "without a lock. It is present in BOTH twins and is safe in CPython, "
            "but a reviewer may reasonably raise it. If the claim is about that "
            "unlocked rebind, score OTHER-TRUE-POSITIVE, not CATCH."),
    "012": ("removals.md: `TokenCache` has no lock at all, so two threads can "
            "refresh concurrently. This is inherited from the base file, "
            "unchanged by the diff, and present in BOTH twins. If the claim is "
            "about that missing lock, score OTHER-TRUE-POSITIVE, not CATCH."),
}

# Held constant across arms and across both twins of every pair (see docstring).
PROJECT_CONTEXT = (
    "Project context: a Python backend service. This PR is a single self-contained "
    "change to one module. There is no merged-surface summary and no acceptance "
    "gate for this change; judge it from the diff alone.\n"
)


# --------------------------------------------------------------------------- #
# Arms
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Arm:
    name: str
    think: bool
    format_json: bool
    note: str = ""
    # Empty means "the model under test". Only the §2.3 reference arm sets this;
    # it is a difficulty referent, never a comparison arm (see ARMS_UNDER_TEST).
    model: str = ""


ARMS: tuple[Arm, ...] = (
    Arm("T", think=True, format_json=False, note="control; = F001 arm B"),
    Arm("U", think=False, format_json=False, note="the missing cell (#84 gate is one-directional)"),
    Arm("S", think=False, format_json=True, note="what main ships; = F001 arm C"),
)
CONTROL_ARM = "T"
# §5: "not a benchmark of models against each other". The decision arithmetic
# only ever looks at these three; a reference arm is reported and never compared.
ARMS_UNDER_TEST = ("T", "U", "S")
REFERENCE_ARM = "REF"
ARMS_BY_NAME = {a.name: a for a in ARMS}


# --------------------------------------------------------------------------- #
# Corpus integrity gate (§6)
# --------------------------------------------------------------------------- #

class CorpusDirtyError(RuntimeError):
    """The corpus tree has uncommitted or untracked changes. §6 refuses to run."""


def _git(args: Sequence[str], *, cwd: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return out.stdout.decode().strip()


def corpus_integrity(repo_root: str = REPO_ROOT, rel: str = CORPUS_REL,
                     *, git: Callable[[Sequence[str], str], str] | None = None
                     ) -> dict[str, Any]:
    """The §6 gate's evidence: the corpus tree SHA plus whether it is dirty.

    ``git`` is injectable so the gate is unit-testable without a scratch repo.
    It is called as ``git(args, cwd)`` and must return the command's stdout.
    """
    run = git if git is not None else (lambda a, cwd: _git(a, cwd=cwd))
    tree_sha = run(["rev-parse", f"HEAD:{rel}"], repo_root).strip()
    porcelain = run(["status", "--porcelain", "--", rel], repo_root)
    dirty = [line for line in porcelain.splitlines() if line.strip()]
    return {
        "path": rel,
        "tree_sha": tree_sha,
        "dirty_entries": dirty,
        "clean": not dirty,
    }


def require_clean_corpus(integrity: Mapping[str, Any], *, override: bool = False) -> None:
    """Raise unless the corpus tree is clean. This is the whole point of §6:
    without it, "ground truth was authored before the run" is a promise instead
    of a checkable fact."""
    if integrity.get("clean"):
        return
    if override:
        return
    entries = "\n  ".join(integrity.get("dirty_entries") or []) or "<unknown>"
    raise CorpusDirtyError(
        f"SPEC-43 §6: refusing to run against a dirty corpus at "
        f"{integrity.get('path')!r}.\n  {entries}\n"
        "Commit or stash the corpus first. Results produced against an "
        "uncommitted corpus cannot show that ground truth predates the run.")


def assert_otp_hints_match_removals(corpus_dir: str = CORPUS_DIR) -> None:
    """Cheap tamper check: every pair_id we hard-code an OTHER-TRUE-POSITIVE hint
    for must still be named in ``removals.md``. If the corpus stops saying it, the
    harness must stop asserting it."""
    path = os.path.join(corpus_dir, "removals.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    missing = [pid for pid in OTHER_TRUE_POSITIVE_HINTS if f"**{pid}**" not in text]
    if missing:
        raise RuntimeError(
            f"OTHER_TRUE_POSITIVE_HINTS names pair(s) {missing} that removals.md "
            f"no longer flags. Reconcile {path} with this module before running.")


# --------------------------------------------------------------------------- #
# Corpus loading
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CorpusItem:
    id: str
    kind: str                 # "seeded" | "clean"
    pair_id: str
    defect_class: str
    depth: str                # "shallow" | "deep"
    file: str
    diff: str
    ground_truth: dict[str, Any]

    @property
    def seeded(self) -> bool:
        return self.kind == "seeded"

    @property
    def expected_approved(self) -> bool:
        return self.ground_truth.get("expected_verdict") == "approve"

    @property
    def mechanism(self) -> str:
        return str(self.ground_truth.get("mechanism") or "")

    @property
    def accepted_finding_forms(self) -> list[str]:
        return list(self.ground_truth.get("accepted_finding_forms") or [])

    @property
    def head(self) -> str:
        """Deterministic synthetic PR head. Same shape as a real one, so the
        ``reviewed_head`` echo requirement is exercised as in production."""
        return sha1(self.id.encode()).hexdigest()[:12]


def load_corpus(corpus_dir: str = CORPUS_DIR) -> list[CorpusItem]:
    with open(os.path.join(corpus_dir, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    items: list[CorpusItem] = []
    for entry in manifest["items"]:
        with open(os.path.join(corpus_dir, entry["diff"]), encoding="utf-8") as fh:
            diff = fh.read()
        with open(os.path.join(corpus_dir, entry["ground_truth"]), encoding="utf-8") as fh:
            gt = json.load(fh)
        items.append(CorpusItem(
            id=entry["id"], kind=entry["kind"], pair_id=entry["pair_id"],
            defect_class=entry["defect_class"], depth=entry["depth"],
            file=entry["file"], diff=diff, ground_truth=gt))
    return items


# --------------------------------------------------------------------------- #
# Production prompt assembly (§2.4)
# --------------------------------------------------------------------------- #

def review_prompt(item: CorpusItem) -> str:
    """The REAL production reviewer prompt, via ``runner._review_pr_prompt`` ->
    ``_review_pr_prompt_segments``. Imported lazily so this module stays
    importable (and unit-testable) on a box without the package's deps."""
    if _PYTHON_PKG_ROOT not in sys.path:
        sys.path.insert(0, _PYTHON_PKG_ROOT)
    from errorta_council.coding.ledger import Task          # noqa: PLC0415
    from errorta_council.coding.runner import _review_pr_prompt  # noqa: PLC0415

    # Scope text derives ONLY from `file`, which both twins of a pair share, so
    # nothing here can tell a reviewer which twin it is looking at.
    task = Task(
        task_id="spec43-review",
        title=f"review PR: change to {item.file}",
        role="reviewer",
        detail=f"Review the change to {item.file} shown in the diff below.",
    )
    pr = {
        "pr_id": f"pr-{item.id}",
        "branch": f"spec43/{item.pair_id}",
        "head": item.head,
        "task_id": "spec43-dev",
    }
    return _review_pr_prompt(task, pr, item.diff, PROJECT_CONTEXT,
                             repo_read=False, gate=False)


# --------------------------------------------------------------------------- #
# Model boundary — the ONLY place that touches the network
# --------------------------------------------------------------------------- #

@dataclass
class ModelReply:
    text: str
    done_reason: str
    eval_count: int
    wall_s: float
    error: str = ""


def ollama_chat(model: str, prompt: str, *, think: bool = True,
                format_json: bool = False, num_predict: int = NUM_PREDICT,
                num_ctx: int = NUM_CTX, temperature: float = TEMPERATURE,
                timeout: int = 900, endpoint: str = OLLAMA) -> ModelReply:
    """One ``/api/chat`` call on the council's real egress path. Mirrors
    ``f001_judge_retest.call`` including the ``done_reason`` / ``eval_count``
    capture, and adds wall-clock (§2.2 reports it)."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": num_ctx, "num_predict": num_predict,
                    "temperature": temperature},
    }
    if not think:
        body["think"] = False
    if format_json:
        body["format"] = "json"
    req = urllib.request.Request(
        f"{endpoint}/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 — a dead call is data, not a crash
        return ModelReply("", "", 0, time.monotonic() - started,
                          error=f"{type(exc).__name__}: {exc}")
    return ModelReply(
        text=(data.get("message") or {}).get("content") or "",
        done_reason=data.get("done_reason") or "",
        eval_count=int(data.get("eval_count") or 0),
        wall_s=time.monotonic() - started,
    )


# --------------------------------------------------------------------------- #
# Verdict parsing — the production coding_turn.v1 envelope
# --------------------------------------------------------------------------- #

def extract_json(text: str) -> tuple[Any, str]:
    """Council-style tolerant parse: whole body, then fenced block, then the
    first ``{...}``. Same ladder as ``f001_judge_retest.extract``."""
    try:
        return json.loads(text.strip()), "direct"
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1)), "fenced"
        except Exception:  # noqa: BLE001
            pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0)), "embedded"
        except Exception:  # noqa: BLE001
            pass
    return None, "unparseable"


@dataclass
class Verdict:
    parsed: bool
    parse_mode: str
    approved: bool | None
    findings: list[dict[str, str]]
    reviewed_head: str
    head_echo_ok: bool
    reason: str = ""


def parse_verdict(text: str, expected_head: str) -> Verdict:
    """Read a ``coding_turn.v1`` review_verdict with the same tolerance the
    council's ``Finding`` / ``ReviewerVerdictIntent`` readers apply (nested
    ``location``, ``description``/``message``/``detail`` aliases, string
    findings). Reimplemented rather than imported so the harness has no pydantic
    dependency; the tolerance, not the type, is what matters for scoring."""
    obj, mode = extract_json(text)
    if not isinstance(obj, dict):
        return Verdict(False, mode, None, [], "", False, "unparseable")
    intent = obj.get("intent")
    if not isinstance(intent, dict):
        # A model that emitted a bare intent (no envelope) is still readable.
        intent = obj if obj.get("kind") == "review_verdict" else None
    if not isinstance(intent, dict):
        return Verdict(False, mode, None, [], "", False, "no_intent")
    if intent.get("kind") != "review_verdict":
        return Verdict(False, mode, None, [], "", False,
                       f"bad_kind:{intent.get('kind')!r}")
    approved = intent.get("approved")
    if not isinstance(approved, bool):
        return Verdict(False, mode, None, [], "", False,
                       f"bad_approved:{approved!r}")
    head = str(intent.get("reviewed_head") or "")
    findings: list[dict[str, str]] = []
    raw_findings = intent.get("findings")
    if isinstance(raw_findings, list):
        for raw in raw_findings:
            findings.append(_normalise_finding(raw))
    return Verdict(True, mode, approved, findings, head,
                   head_echo_ok=(head == expected_head), reason="ok")


def _normalise_finding(raw: Any) -> dict[str, str]:
    if isinstance(raw, str):
        text = raw.strip()
        return {"severity": "major", "path": "", "title": text, "body": text}
    if not isinstance(raw, dict):
        return {"severity": "major", "path": "", "title": "", "body": str(raw)}

    def _text(*keys: str) -> str:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    loc = raw.get("location")
    path = _text("path", "file")
    if not path and isinstance(loc, dict):
        path = str(loc.get("path") or loc.get("file") or "").strip()
    elif not path and isinstance(loc, str):
        path = loc.strip()
    alias = _text("description", "message", "detail", "text", "comment")
    title = _text("title") or _text("summary") or alias
    body = _text("body") or alias or _text("summary") or title
    severity = str(raw.get("severity") or "major").strip().lower()
    return {"severity": severity, "path": path, "title": title, "body": body}


def findings_for_extraction(findings: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    """Severity-ordered, capped at ``MAX_FINDINGS_TO_EXTRACTOR``. The cap is the
    anti-shotgun measure described in the module docstring; the sort is stable so
    a reviewer's own ordering breaks ties."""
    indexed = list(enumerate(findings))
    indexed.sort(key=lambda pair: (_SEVERITY_RANK.get(
        str(pair[1].get("severity", "")).lower(), 1), pair[0]))
    return [dict(f) for _, f in indexed[:MAX_FINDINGS_TO_EXTRACTOR]]


# --------------------------------------------------------------------------- #
# Stage 1 — claim extraction (§2.3.1)
# --------------------------------------------------------------------------- #

NO_CLAIM = ""   # sentinel: nothing to score; a seeded item scores MISS

_EXTRACT_PROMPT = (
    "You are normalising a code-review verdict for scoring. Below are the "
    "blocking findings from one review, in severity order.\n\n"
    "Write ONE sentence of at most {cap} words stating the PRIMARY defect the "
    "review claims: WHERE it is and WHAT goes wrong. Use the reviewer's own "
    "substance; do not add, correct, or evaluate anything. If the findings claim "
    "no concrete defect, reply exactly NONE.\n\n"
    "Reply with the sentence only — no preamble, no quotes, no JSON.\n\n"
    "FINDINGS:\n{findings}\n"
)


def cap_words(text: str, cap: int = CLAIM_WORD_CAP) -> str:
    """Enforce the <=20-word normalisation MECHANICALLY, after the model returns.
    This is load-bearing for blindness: without it arm T's 1455-token verdicts
    would keep the length advantage that makes a lenient semantic match likelier
    than in arm S's 91."""
    words = str(text or "").split()
    return " ".join(words[:cap])


def extract_claim(verdict: Verdict, *, model: str = SCORER_MODEL,
                  call: Callable[..., ModelReply] = ollama_chat) -> str:
    """Reduce one verdict to a normalised claim. The single model call lives
    behind this function so the whole downstream pipeline is stubbable.

    Returns ``NO_CLAIM`` when there is nothing to score — an unparseable verdict,
    an approval, or an approval-shaped verdict with no findings. Those cases cost
    no model call and are deterministic.
    """
    if not verdict.parsed:
        return NO_CLAIM
    if verdict.approved:
        return NO_CLAIM
    if not verdict.findings:
        return NO_CLAIM
    shown = findings_for_extraction(verdict.findings)
    rendered = "\n\n".join(
        f"- severity: {f.get('severity', '')}\n  path: {f.get('path', '')}\n"
        f"  title: {f.get('title', '')}\n  body: {f.get('body', '')}"
        for f in shown)
    prompt = _EXTRACT_PROMPT.format(cap=CLAIM_WORD_CAP, findings=rendered)
    reply = call(model, prompt, think=False, format_json=False, num_predict=256)
    text = (reply.text or "").strip()
    if not text or text.strip().upper().startswith("NONE"):
        return NO_CLAIM
    return cap_words(text)


# --------------------------------------------------------------------------- #
# Stage 2 — the blind shuffle (§2.3.2)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BlindRow:
    """What the scorer sees. Deliberately carries NO arm, NO item id, NO depth,
    NO defect class — only the row uid and the semantic matching problem."""
    row_uid: str
    claim: str
    mechanism: str
    accepted_finding_forms: tuple[str, ...]
    other_true_positive_hint: str = ""


def build_blind_rows(records: Sequence[Mapping[str, Any]], *, seed: int = DEFAULT_SEED
                     ) -> tuple[list[BlindRow], dict[str, dict[str, Any]]]:
    """Flatten every (item, arm, trial) claim into ONE shuffled row list, with
    the arm label held OUT-OF-BAND in ``keymap`` and joined back only after
    scoring.

    ``records`` are seeded-item records carrying ``item_id``, ``arm``, ``trial``,
    ``claim``, plus the ground truth needed to score. The shuffle is seeded and
    the seed is recorded in the results JSON, so the ordering is reproducible.
    """
    rows: list[BlindRow] = []
    keymap: dict[str, dict[str, Any]] = {}
    for rec in records:
        uid = _row_uid(rec)
        rows.append(BlindRow(
            row_uid=uid,
            claim=str(rec.get("claim") or ""),
            mechanism=str(rec.get("mechanism") or ""),
            accepted_finding_forms=tuple(rec.get("accepted_finding_forms") or ()),
            other_true_positive_hint=str(rec.get("other_true_positive_hint") or ""),
        ))
        keymap[uid] = {
            "item_id": rec["item_id"], "arm": rec["arm"], "trial": rec["trial"],
            "defect_class": rec.get("defect_class", ""),
            "depth": rec.get("depth", ""), "pair_id": rec.get("pair_id", ""),
        }
    random.Random(seed).shuffle(rows)
    return rows, keymap


def _row_uid(rec: Mapping[str, Any]) -> str:
    """An opaque, stable row id. Deliberately a hash, not
    ``f"{item}-{arm}-{trial}"``: a readable uid would re-attach the arm label to
    the very rows the shuffle exists to detach it from."""
    raw = f"{rec['item_id']}|{rec['arm']}|{rec['trial']}"
    return sha1(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Stage 3 — scoring (§2.3.3)
# --------------------------------------------------------------------------- #

CATCH = "CATCH"
OTHER_TRUE_POSITIVE = "OTHER-TRUE-POSITIVE"
MISS = "MISS"
OUTCOMES = (CATCH, OTHER_TRUE_POSITIVE, MISS)

_SCORE_PROMPT = (
    "You are scoring one code-review claim against hand-authored ground truth. "
    "You cannot see the model, the configuration, or the code. Judge only the "
    "text below.\n\n"
    "GROUND-TRUTH MECHANISM (what actually goes wrong):\n{mechanism}\n\n"
    "ACCEPTED FINDING FORMS (phrasings authored in advance that COUNT as "
    "correct):\n{forms}\n\n"
    "{otp_hint}"
    "REVIEWER CLAIM:\n{claim}\n\n"
    "Answer with exactly one of these three tokens and nothing else:\n"
    "  CATCH                 - the claim names the ground-truth defect. Match on "
    "MECHANISM AND CONSEQUENCE, not on location: a claim that describes the same "
    "thing going wrong is a CATCH even if it names no line, and several accepted "
    "forms are deliberately location-vague.\n"
    "  OTHER-TRUE-POSITIVE   - the claim describes a DIFFERENT but GENUINE defect, "
    "not the ground-truth one.\n"
    "  MISS                  - the claim describes neither.\n"
)


def score_claim(row: BlindRow, *, model: str = SCORER_MODEL,
                call: Callable[..., ModelReply] = ollama_chat
                ) -> tuple[str, bool]:
    """Score one blind row. Returns ``(outcome, scorer_parse_failed)``.

    Sees only ``(claim, mechanism, accepted forms, OTP hint)`` — the arm is not
    reachable from ``BlindRow``. An empty claim is a MISS with no model call.
    """
    if not row.claim.strip():
        return MISS, False
    forms = "\n".join(f"  - {f}" for f in row.accepted_finding_forms) or "  (none)"
    otp_hint = (f"CORPUS NOTE — read before scoring:\n{row.other_true_positive_hint}\n\n"
                if row.other_true_positive_hint else "")
    prompt = _SCORE_PROMPT.format(mechanism=row.mechanism, forms=forms,
                                  otp_hint=otp_hint, claim=row.claim)
    reply = call(model, prompt, think=False, format_json=False, num_predict=32)
    return parse_outcome(reply.text)


def parse_outcome(text: str) -> tuple[str, bool]:
    """Tolerant read of the scorer's token. OTHER-TRUE-POSITIVE is checked first
    so its substring cannot be mistaken for anything else. An unreadable reply is
    scored MISS but FLAGGED, and the flagged count is reported — defaulting to
    MISS silently would be a quiet bias against whichever arm confuses the
    scorer."""
    up = str(text or "").upper()
    if "OTHER-TRUE-POSITIVE" in up or "OTHER TRUE POSITIVE" in up:
        return OTHER_TRUE_POSITIVE, False
    if "CATCH" in up:
        return CATCH, False
    if "MISS" in up:
        return MISS, False
    return MISS, True


# --------------------------------------------------------------------------- #
# Trial collapse — §2.4: 3 trials estimate variance, they do NOT multiply N
# --------------------------------------------------------------------------- #

def collapse_trials(outcomes: Sequence[str]) -> tuple[str, float]:
    """Collapse one item's trials to ONE item-level outcome, plus the raw
    per-trial catch rate (which is what the 3 trials are actually for).

    OTHER-TRUE-POSITIVE trials are removed first, per §2.3: excluded from
    numerator AND denominator. If every trial was OTHER-TRUE-POSITIVE the ITEM is
    excluded. Otherwise a strict majority of the remaining trials decides.
    """
    kept = [o for o in outcomes if o != OTHER_TRUE_POSITIVE]
    if not kept:
        return OTHER_TRUE_POSITIVE, 0.0
    catches = sum(1 for o in kept if o == CATCH)
    rate = catches / len(kept)
    return (CATCH if catches * 2 > len(kept) else MISS), rate


# --------------------------------------------------------------------------- #
# Statistics — stdlib only (§3 says so explicitly)
# --------------------------------------------------------------------------- #

def binom_cdf_le(k: int, n: int, p: float = 0.5) -> float:
    """P(X <= k) for X ~ Binomial(n, p). Exact, via ``math.comb``."""
    if n <= 0:
        return 1.0
    k = max(0, min(k, n))
    return sum(math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i)) for i in range(k + 1))


def paired_comparison(better: Mapping[str, bool], worse: Mapping[str, bool],
                      *, min_item_diff: int = MIN_ITEM_DIFF, alpha: float = ALPHA
                      ) -> dict[str, Any]:
    """§3's decision rule as arithmetic. ``better``/``worse`` map item_id -> the
    arm succeeded on that item (caught the defect / did not falsely reject).

    *Materially below* = an item-count difference of >= ``min_item_diff`` AND a
    one-sided exact-binomial p <= ``alpha`` on the DISCORDANT pairs (exact
    McNemar): under H0 each discordant pair is a fair coin, so
    p = P(X <= c) with n = b + c.
    """
    ids = sorted(set(better) & set(worse))
    n_better = sum(1 for i in ids if better[i])
    n_worse = sum(1 for i in ids if worse[i])
    b = sum(1 for i in ids if better[i] and not worse[i])
    c = sum(1 for i in ids if worse[i] and not better[i])
    p = binom_cdf_le(c, b + c, 0.5)
    diff = n_better - n_worse
    return {
        "n_items": len(ids),
        "n_better": n_better,
        "n_worse": n_worse,
        "item_diff": diff,
        "discordant_better_only": b,
        "discordant_worse_only": c,
        "p_one_sided": round(p, 6),
        "materially_below": bool(diff >= min_item_diff and p <= alpha),
        "threshold": {"min_item_diff": min_item_diff, "alpha": alpha},
    }


def cohens_kappa(a: Sequence[str], b: Sequence[str],
                 categories: Sequence[str] = OUTCOMES) -> float:
    """Cohen's kappa for the two-scorer agreement gate (§2.3: kappa < 0.6 voids
    the run). The second scorer is a human, so this is applied out of band."""
    if len(a) != len(b) or not a:
        raise ValueError("kappa needs two equal, non-empty label sequences")
    n = len(a)
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    expected = sum((a.count(cat) / n) * (b.count(cat) / n) for cat in categories)
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def select_rescore_sample(row_uids: Sequence[str], *, seed: int = DEFAULT_SEED,
                          fraction: float = RESCORE_FRACTION) -> list[str]:
    """The blind sample the human second scorer re-scores (§2.3). Drawn with its
    own derived seed so it is reproducible and independent of the row shuffle."""
    ordered = sorted(row_uids)
    k = max(1, round(len(ordered) * fraction)) if ordered else 0
    return sorted(random.Random(seed + 1).sample(ordered, k)) if k else []


# --------------------------------------------------------------------------- #
# Metrics (§2.2, §4)
# --------------------------------------------------------------------------- #

def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _bucket(item_ids: Iterable[str], per_item: Mapping[str, str]) -> dict[str, Any]:
    ids = [i for i in item_ids if i in per_item]
    excluded = [i for i in ids if per_item[i] == OTHER_TRUE_POSITIVE]
    scored = [i for i in ids if per_item[i] != OTHER_TRUE_POSITIVE]
    catches = [i for i in scored if per_item[i] == CATCH]
    return {
        "catches": len(catches),
        "n": len(scored),                 # OTP excluded from the DENOMINATOR
        "excluded_other_true_positive": len(excluded),
        "rate": _rate(len(catches), len(scored)),
    }


def compute_metrics(items: Sequence[CorpusItem],
                    per_item_outcome: Mapping[str, Mapping[str, str]],
                    per_item_approved: Mapping[str, Mapping[str, bool]],
                    call_stats: Mapping[str, Mapping[str, Any]],
                    arms: Sequence[Arm] = ARMS) -> dict[str, Any]:
    """Per-arm metrics. ``per_item_outcome`` / ``per_item_approved`` are
    ``{arm: {item_id: value}}``.

    Detection is reported SEPARATELY for shallow and deep, per class and
    aggregate. False approval is measured on seeded items, false rejection on the
    clean twins. OTHER-TRUE-POSITIVE items are out of both the numerator and the
    denominator of every detection figure.
    """
    seeded = [i for i in items if i.seeded]
    clean = [i for i in items if not i.seeded]
    classes = sorted({i.defect_class for i in seeded})
    out: dict[str, Any] = {}
    for arm in arms:
        outcomes = per_item_outcome.get(arm.name, {})
        approved = per_item_approved.get(arm.name, {})
        by_depth = {
            depth: _bucket([i.id for i in seeded if i.depth == depth], outcomes)
            for depth in ("shallow", "deep")
        }
        by_class = {
            cls: {
                "aggregate": _bucket([i.id for i in seeded if i.defect_class == cls],
                                     outcomes),
                **{depth: _bucket(
                    [i.id for i in seeded
                     if i.defect_class == cls and i.depth == depth], outcomes)
                   for depth in ("shallow", "deep")},
            }
            for cls in classes
        }
        false_approvals = [i.id for i in seeded if approved.get(i.id) is True]
        false_rejections = [i.id for i in clean if approved.get(i.id) is False]
        clean_answered = [i.id for i in clean if i.id in approved]
        seeded_answered = [i.id for i in seeded if i.id in approved]
        out[arm.name] = {
            "arm": arm.name,
            "think": arm.think,
            "format_json": arm.format_json,
            "note": arm.note,
            "detection": {
                "shallow": by_depth["shallow"],
                "deep": by_depth["deep"],
                "aggregate": _bucket([i.id for i in seeded], outcomes),
                "by_class": by_class,
            },
            "false_approval": {
                "count": len(false_approvals),
                "n": len(seeded_answered),
                "rate": _rate(len(false_approvals), len(seeded_answered)),
                "items": false_approvals,
            },
            "false_rejection": {
                "count": len(false_rejections),
                "n": len(clean_answered),
                "rate": _rate(len(false_rejections), len(clean_answered)),
                "items": false_rejections,
            },
            "cost": dict(call_stats.get(arm.name, {})),
        }
    return out


# --------------------------------------------------------------------------- #
# The pre-registered decision rule (§3 -> §4 branches)
# --------------------------------------------------------------------------- #

def _deep_success(items: Sequence[CorpusItem],
                  outcomes: Mapping[str, str]) -> dict[str, bool]:
    """Item -> caught, deep seeded items only, OTHER-TRUE-POSITIVE items dropped
    entirely (so they are absent from both sides of the paired test)."""
    return {i.id: outcomes[i.id] == CATCH
            for i in items
            if i.seeded and i.depth == "deep" and i.id in outcomes
            and outcomes[i.id] != OTHER_TRUE_POSITIVE}


def _clean_success(items: Sequence[CorpusItem],
                   approved: Mapping[str, bool]) -> dict[str, bool]:
    """Item -> did NOT falsely reject, clean items only. Phrased as a success so
    the same paired test applies unchanged."""
    return {i.id: approved[i.id] is True
            for i in items if not i.seeded and i.id in approved}


def select_outcome_branch(items: Sequence[CorpusItem],
                          per_item_outcome: Mapping[str, Mapping[str, str]],
                          per_item_approved: Mapping[str, Mapping[str, bool]],
                          metrics: Mapping[str, Any],
                          *, control: str = CONTROL_ARM,
                          arms_under_test: Sequence[str] = ARMS_UNDER_TEST
                          ) -> dict[str, Any]:
    """Pick §4's branch MECHANICALLY, before any human writes prose.

    Only ``arms_under_test`` enter the arithmetic — a §2.3 reference arm is a
    difficulty referent and §5 forbids treating it as a comparison arm.

    Returns the branch name, whether it is one §4 pre-stated, and the full
    arithmetic it was selected from — so a human who disagrees in the writeup has
    to disagree with numbers that are already on record.
    """
    deep = {arm: _deep_success(items, per_item_outcome.get(arm, {}))
            for arm in arms_under_test}
    clean = {arm: _clean_success(items, per_item_approved.get(arm, {}))
             for arm in arms_under_test}
    deep_rates = {
        arm: (metrics.get(arm, {}).get("detection", {}).get("deep", {}) or {}).get("rate")
        for arm in arms_under_test
    }
    known = {a: r for a, r in deep_rates.items() if r is not None}

    basis: dict[str, Any] = {
        "deep_detection_rate": deep_rates,
        "deep_catch_counts": {a: sum(1 for v in d.values() if v) for a, d in deep.items()},
        "comparisons": {},
        "branch_order": [
            "corpus_at_ceiling_rebuild_deeper", "corpus_at_floor_compare_reference",
            "reasoning_suppression_costs_quality", "format_constraint_not_thinking",
            "per_class_collapse_governs", "false_rejection_regression",
            "no_detectable_difference_at_this_power", "think_false_is_free",
        ],
    }

    def compare(better: str, worse: str, table: Mapping[str, Mapping[str, bool]]
                ) -> dict[str, Any]:
        return paired_comparison(table.get(better, {}), table.get(worse, {}))

    s_vs_t = compare(control, "S", deep)
    u_vs_t = compare(control, "U", deep)
    s_vs_u = compare("U", "S", deep)
    fr_s_vs_t = compare(control, "S", clean)
    fr_u_vs_t = compare(control, "U", clean)
    basis["comparisons"] = {
        # The branch logic reads only these five (control-favouring) directions.
        "deep_detection_S_below_T": s_vs_t,
        "deep_detection_U_below_T": u_vs_t,
        "deep_detection_S_below_U": s_vs_u,
        "false_rejection_S_worse_than_T": fr_s_vs_t,
        "false_rejection_U_worse_than_T": fr_u_vs_t,
        # ...and these are the MIRROR directions, computed and recorded solely so
        # §3's re-score trigger is symmetric. Without them the harness would be
        # sceptical only when the result went against shipping — which is exactly
        # the laundered prior §3 calls out. They never select a branch.
        "deep_detection_T_below_S": compare("S", control, deep),
        "deep_detection_T_below_U": compare("U", control, deep),
        "deep_detection_U_below_S": compare("S", "U", deep),
        "false_rejection_T_worse_than_S": compare("S", control, clean),
        "false_rejection_T_worse_than_U": compare("U", control, clean),
    }
    collapse = _per_class_collapse(metrics, control=control)
    basis["per_class_collapse"] = collapse

    def branch(name: str, in_spec: bool, why: str) -> dict[str, Any]:
        return {"outcome_branch": name, "in_spec_section_4": in_spec,
                "rationale": why, "basis": basis}

    # 1-2: §4 says these settle "before concluding anything", so they go first.
    if known and len(known) == len(deep_rates) and min(known.values()) > CEILING:
        return branch("corpus_at_ceiling_rebuild_deeper", True,
                      f"every arm's deep detection is above {CEILING}; the corpus "
                      "is too easy to conclude anything from. Rebuild deeper.")
    if known and len(known) == len(deep_rates) and max(known.values()) < FLOOR:
        return branch("corpus_at_floor_compare_reference", True,
                      f"every arm's deep detection is below {FLOOR}; compare "
                      "against the gemma3:27b reference pass before concluding "
                      "anything about seat fitness.")

    # 3-4: the arm comparisons.
    if s_vs_t["materially_below"] and u_vs_t["materially_below"]:
        return branch("reasoning_suppression_costs_quality", True,
                      "S and U are both materially below T on the deep subset: "
                      "suppressing the reasoning cost verdict quality. Remedy: "
                      "default local_think_false OFF for REVIEWER seats.")
    if s_vs_u["materially_below"]:
        return branch("format_constraint_not_thinking", True,
                      "S is materially below U while U carries the reasoning "
                      "suppression too: it is the format constraint, not the "
                      "thinking suppression. Remedy: drop local_structured_format.")
    if s_vs_t["materially_below"] or u_vs_t["materially_below"]:
        return branch("unclassified_arm_regression", False,
                      "an arm is materially below control in a pattern §4 did "
                      "not pre-state (e.g. S<T with U≈T and S≈U). Not silently "
                      "mapped onto a pre-stated branch; needs a human read.")

    # 5-6: conditioned on detection parity.
    if collapse["collapsed"]:
        return branch("per_class_collapse_governs", True,
                      "aggregate parity, but at least one defect class where the "
                      "control catches everything and an arm catches nothing. A "
                      "reviewer blind to one class is unfit regardless of its mean.")
    if fr_s_vs_t["materially_below"] or fr_u_vs_t["materially_below"]:
        return branch("false_rejection_regression", True,
                      "detection ties but false rejection on the clean twins is "
                      "materially worse: the council stalls on clean code. "
                      "Treated as S < T for remedy purposes.")

    # 7-8: the null. See the docstring for why zero-difference is split out.
    if s_vs_t["item_diff"] == 0 and u_vs_t["item_diff"] == 0:
        return branch("think_false_is_free", True,
                      "identical deep catch counts on all three arms and no "
                      "false-rejection gap. Per §1 this licenses the COST "
                      "argument only.")
    return branch("no_detectable_difference_at_this_power", True,
                  "a nonzero deep-subset difference that does not clear the §3 "
                  "threshold. Report as 'no detectable difference at this power' "
                  "— explicitly NOT as 'no difference'.")


def _per_class_collapse(metrics: Mapping[str, Any], *, control: str = CONTROL_ARM
                        ) -> dict[str, Any]:
    """§4: a class where the control catches everything and a test arm catches
    nothing. At 2 seeded items per class this is the only collapse the corpus can
    show mechanically; that limit is recorded rather than hidden."""
    ctrl = (metrics.get(control, {}).get("detection", {}) or {}).get("by_class", {})
    hits: list[dict[str, Any]] = []
    for arm, m in metrics.items():
        if arm == control or arm not in ARMS_UNDER_TEST:
            continue
        by_class = (m.get("detection", {}) or {}).get("by_class", {})
        for cls, agg in by_class.items():
            arm_rate = (agg.get("aggregate") or {}).get("rate")
            ctrl_rate = ((ctrl.get(cls) or {}).get("aggregate") or {}).get("rate")
            if ctrl_rate == 1.0 and arm_rate == 0.0:
                hits.append({"arm": arm, "defect_class": cls,
                             "control_rate": ctrl_rate, "arm_rate": arm_rate})
    return {"collapsed": bool(hits), "classes": hits,
            "note": "2 seeded items per class; this detects only a total collapse."}


def rescore_triggered(basis: Mapping[str, Any]) -> bool:
    """§3's symmetric re-score trigger: ANY arm difference beyond the threshold,
    in EITHER direction. Being sceptical only when the result favours shipping
    would launder a prior, so the trigger does not look at which way it went."""
    comparisons = basis.get("comparisons", {}) or {}
    return any(bool(c.get("materially_below")) for c in comparisons.values())


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

@dataclass
class CallRecord:
    item_id: str
    arm: str
    trial: int
    parsed: bool
    parse_mode: str
    approved: bool | None
    n_findings: int
    head_echo_ok: bool
    done_reason: str
    eval_count: int
    wall_s: float
    error: str = ""
    reason: str = ""
    claim: str = ""
    raw_text: str = ""


@dataclass
class RunConfig:
    model: str = MODEL_UNDER_TEST
    scorer_model: str = SCORER_MODEL
    extractor_model: str = SCORER_MODEL
    reference_model: str = ""
    trials: int = TRIALS
    seed: int = DEFAULT_SEED
    arms: tuple[Arm, ...] = ARMS
    endpoint: str = OLLAMA
    limit: int = 0
    corpus_override: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


def run_arms(items: Sequence[CorpusItem], config: RunConfig,
             *, call: Callable[..., ModelReply] = ollama_chat,
             progress: Callable[[str], None] = lambda _m: None
             ) -> list[CallRecord]:
    """Every (item, arm, trial) review call. No scoring here."""
    records: list[CallRecord] = []
    for item in items:
        prompt = review_prompt(item)
        for arm in config.arms:
            for trial in range(config.trials):
                reply = call(arm.model or config.model, prompt, think=arm.think,
                             format_json=arm.format_json,
                             num_predict=NUM_PREDICT, num_ctx=NUM_CTX,
                             temperature=TEMPERATURE, endpoint=config.endpoint)
                verdict = parse_verdict(reply.text, item.head)
                records.append(CallRecord(
                    item_id=item.id, arm=arm.name, trial=trial,
                    parsed=verdict.parsed, parse_mode=verdict.parse_mode,
                    approved=verdict.approved, n_findings=len(verdict.findings),
                    head_echo_ok=verdict.head_echo_ok,
                    done_reason=reply.done_reason, eval_count=reply.eval_count,
                    wall_s=round(reply.wall_s, 3), error=reply.error,
                    reason=verdict.reason, raw_text=reply.text))
                records[-1].claim = extract_claim(
                    verdict, model=config.extractor_model, call=call)
                progress(f"{item.id:38} {arm.name} t{trial} "
                         f"parsed={verdict.parsed} approved={verdict.approved} "
                         f"tok={reply.eval_count}")
    return records


def call_stats(records: Sequence[CallRecord], arms: Sequence[Arm] = ARMS
               ) -> dict[str, dict[str, Any]]:
    """§2.2's cost axis, plus the contamination counters §4 wants reported
    separately: unparseable verdicts and failed head echoes."""
    stats: dict[str, dict[str, Any]] = {}
    for arm in arms:
        rows = [r for r in records if r.arm == arm.name]
        tokens = [r.eval_count for r in rows if r.eval_count]
        walls = [r.wall_s for r in rows if r.wall_s]
        done: dict[str, int] = {}
        for r in rows:
            key = r.done_reason or "(none)"
            done[key] = done.get(key, 0) + 1
        stats[arm.name] = {
            "calls": len(rows),
            "mean_eval_tokens": round(sum(tokens) / len(tokens), 1) if tokens else 0,
            "mean_wall_s": round(sum(walls) / len(walls), 2) if walls else 0,
            "total_wall_s": round(sum(walls), 1),
            "unparseable": sum(1 for r in rows if not r.parsed),
            "head_echo_ok": sum(1 for r in rows if r.head_echo_ok),
            "http_errors": sum(1 for r in rows if r.error),
            "done_reason_counts": done,
        }
    return stats


def blind_records(items: Sequence[CorpusItem], records: Sequence[CallRecord]
                  ) -> list[dict[str, Any]]:
    """Seeded-item call records, joined to their ground truth, ready to shuffle."""
    by_id = {i.id: i for i in items}
    out: list[dict[str, Any]] = []
    for r in records:
        item = by_id.get(r.item_id)
        if item is None or not item.seeded:
            continue
        out.append({
            "item_id": r.item_id, "arm": r.arm, "trial": r.trial,
            "claim": r.claim, "mechanism": item.mechanism,
            "accepted_finding_forms": item.accepted_finding_forms,
            "other_true_positive_hint": OTHER_TRUE_POSITIVE_HINTS.get(item.pair_id, ""),
            "defect_class": item.defect_class, "depth": item.depth,
            "pair_id": item.pair_id,
        })
    return out


def score_rows(rows: Sequence[BlindRow], config: RunConfig,
               *, call: Callable[..., ModelReply] = ollama_chat
               ) -> tuple[dict[str, str], int]:
    """Score every blind row IN SHUFFLED ORDER. Returns row_uid -> outcome plus
    the count of scorer replies that could not be read."""
    scored: dict[str, str] = {}
    failures = 0
    for row in rows:
        outcome, failed = score_claim(row, model=config.scorer_model, call=call)
        scored[row.row_uid] = outcome
        failures += int(failed)
    return scored, failures


def join_and_collapse(scored: Mapping[str, str], keymap: Mapping[str, Mapping[str, Any]]
                      ) -> dict[str, dict[str, Any]]:
    """The join the blindness exists for: attach arm/item labels to the scored
    rows ONLY NOW, then collapse trials to item-level outcomes (§2.4)."""
    trials: dict[str, dict[str, list[str]]] = {}
    for uid, outcome in scored.items():
        key = keymap.get(uid)
        if key is None:
            continue
        trials.setdefault(key["arm"], {}).setdefault(key["item_id"], []).append(outcome)
    collapsed: dict[str, dict[str, Any]] = {}
    for arm, per_item in trials.items():
        collapsed[arm] = {"outcomes": {}, "trial_catch_rate": {}}
        for item_id, outcomes in per_item.items():
            outcome, rate = collapse_trials(outcomes)
            collapsed[arm]["outcomes"][item_id] = outcome
            collapsed[arm]["trial_catch_rate"][item_id] = round(rate, 3)
    return collapsed


def collapse_approvals(records: Sequence[CallRecord]) -> dict[str, dict[str, bool]]:
    """Item-level approve/reject per arm, by majority over trials. An unparseable
    verdict is NOT an approval and NOT a rejection — it is dropped from this axis
    only (it is still a MISS on detection, per §4)."""
    buckets: dict[str, dict[str, list[bool]]] = {}
    for r in records:
        if r.approved is None:
            continue
        buckets.setdefault(r.arm, {}).setdefault(r.item_id, []).append(r.approved)
    out: dict[str, dict[str, bool]] = {}
    for arm, per_item in buckets.items():
        out[arm] = {}
        for item_id, votes in per_item.items():
            out[arm][item_id] = sum(votes) * 2 > len(votes)
    return out


def run(config: RunConfig, *, call: Callable[..., ModelReply] = ollama_chat,
        corpus_dir: str = CORPUS_DIR, repo_root: str = REPO_ROOT,
        git: Callable[[Sequence[str], str], str] | None = None,
        progress: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """The whole pipeline, gated on §6 and emitting ``outcome_branch``."""
    integrity = corpus_integrity(repo_root, git=git)
    require_clean_corpus(integrity, override=config.corpus_override)
    assert_otp_hints_match_removals(corpus_dir)

    items = load_corpus(corpus_dir)
    if config.limit:
        keep = {i.pair_id for i in items[: config.limit * 2]}
        items = [i for i in items if i.pair_id in keep]

    started = time.time()
    records = run_arms(items, config, call=call, progress=progress)
    rows, keymap = build_blind_rows(blind_records(items, records), seed=config.seed)
    scored, scorer_failures = score_rows(rows, config, call=call)
    collapsed = join_and_collapse(scored, keymap)

    per_item_outcome = {arm: data["outcomes"] for arm, data in collapsed.items()}
    per_item_approved = collapse_approvals(records)
    stats = call_stats(records, config.arms)
    metrics = compute_metrics(items, per_item_outcome, per_item_approved, stats,
                              config.arms)
    branch = select_outcome_branch(items, per_item_outcome, per_item_approved, metrics)

    return {
        "spec": "SPEC-43",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_s": round(time.time() - started, 1),
        "corpus_integrity": integrity,
        "config": {
            "model": config.model, "scorer_model": config.scorer_model,
            "extractor_model": config.extractor_model,
            "reference_model": config.reference_model,
            "trials": config.trials, "shuffle_seed": config.seed,
            "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT,
            "temperature": TEMPERATURE, "endpoint": config.endpoint,
            "arms": [asdict(a) for a in config.arms],
            "claim_word_cap": CLAIM_WORD_CAP,
            "decision_rule": {"min_item_diff": MIN_ITEM_DIFF, "alpha": ALPHA,
                              "ceiling": CEILING, "floor": FLOOR},
            **config.extras,
        },
        "counts": {
            "items": len(items),
            "seeded": sum(1 for i in items if i.seeded),
            "clean": sum(1 for i in items if not i.seeded),
            "review_calls": len(records),
            "blind_rows": len(rows),
            "scorer_unreadable_replies": scorer_failures,
        },
        "metrics": metrics,
        "outcome_branch": branch["outcome_branch"],
        "outcome_branch_in_spec_section_4": branch["in_spec_section_4"],
        "outcome_branch_rationale": branch["rationale"],
        "decision_basis": branch["basis"],
        "rescore_triggered": rescore_triggered(branch["basis"]),
        "rescore_sample_row_uids": select_rescore_sample(
            [r.row_uid for r in rows], seed=config.seed),
        "kappa_void_threshold": KAPPA_VOID_THRESHOLD,
        "per_item": {arm: data for arm, data in collapsed.items()},
        "blind_row_order": [r.row_uid for r in rows],
        "row_keymap": {uid: dict(key) for uid, key in keymap.items()},
        "scored_rows": scored,
        "calls": [asdict(r) for r in records],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=MODEL_UNDER_TEST)
    ap.add_argument("--scorer-model", default=SCORER_MODEL)
    ap.add_argument("--extractor-model", default=SCORER_MODEL)
    ap.add_argument("--reference-model", default="",
                    help=f"§2.3 difficulty referent, e.g. {REFERENCE_MODEL}. Runs "
                         "the corpus as a fourth reviewer arm (think on, no format).")
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--endpoint", default=OLLAMA)
    ap.add_argument("--limit", type=int, default=0,
                    help="smoke-test only: run the first N PAIRS. A limited run "
                         "is not a SPEC-43 result (§3: full corpus, no peeking).")
    ap.add_argument("--out", default="")
    ap.add_argument("--i-know-the-corpus-is-dirty", action="store_true",
                    dest="corpus_override",
                    help="override the §6 gate. Stamps corpus_integrity.clean=false "
                         "into the JSON so the override stays visible.")
    args = ap.parse_args(argv)

    arms = ARMS
    extras: dict[str, Any] = {}
    if args.reference_model:
        arms = (*ARMS, Arm(REFERENCE_ARM, think=True, format_json=False,
                           note=f"§2.3 difficulty referent ({args.reference_model})",
                           model=args.reference_model))
        extras["reference_arm"] = REFERENCE_ARM

    config = RunConfig(
        model=args.model, scorer_model=args.scorer_model,
        extractor_model=args.extractor_model,
        reference_model=args.reference_model, trials=args.trials,
        seed=args.seed, arms=arms, endpoint=args.endpoint, limit=args.limit,
        corpus_override=args.corpus_override, extras=extras)

    try:
        results = run(config, progress=lambda m: print(m, flush=True))
    except CorpusDirtyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out = args.out or os.path.join(
        RESULTS_DIR, f"spec43-verdict-usefulness-{time.strftime('%Y-%m-%d')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
        fh.write("\n")
    print(f"\noutcome_branch: {results['outcome_branch']} "
          f"(in §4: {results['outcome_branch_in_spec_section_4']})")
    print(f"rescore_triggered: {results['rescore_triggered']}")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
