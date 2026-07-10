"""F102 — durable publish provenance for a Coding project.

Two ledger-local files under the project's ledger dir:

* ``publish-targets.json`` — the configured publish targets (manual export, or a
  GitHub repo/PR target), keyed by ``target_id``. Full-rewrite atomic.
* ``publish-events.jsonl`` — an append-only log of publish events (planned /
  scanned / committed / pushed / pr_opened / failed) carrying branch, commit,
  PR URL, and any error.

This module is COUNCIL-SIDE and PURE: stdlib ``json`` / ``dataclasses`` only. It
performs NO subprocess / network / keychain egress (Council invariant 3 — all
``gh`` / git / zip / keychain calls live in ``errorta_tools/runner``). Every
string field of an event is REDACTED (tokens, home path, username) before it is
written, so a stray token or absolute path can never land in the durable log /
diagnostics (D2 token hygiene).
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from errorta_diagnostics.redact import (
    redact_home_path,
    redact_tokens,
    redact_username,
)

_TARGET_KINDS = ("manual_export", "existing_repo_pr", "new_github_repo")
_EVENT_STATES = (
    "planned", "scanned", "committed", "pushed", "pr_opened", "failed",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: str | None) -> str | None:
    """Redact tokens + home path + username from one string field. None stays
    None. Defense-in-depth: every event/target string is scrubbed before write so
    the durable log can never carry a token or absolute home path."""
    if not value:
        return value
    text, _ = redact_tokens(value)
    text, _ = redact_home_path(text)
    text, _ = redact_username(text)
    return text


@dataclass
class PublishTarget:
    target_id: str
    kind: str  # manual_export | existing_repo_pr | new_github_repo
    repo_path: str | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    default_branch: str | None = None
    privacy: str | None = None  # "private" | "public"
    created_at: str = field(default_factory=_now)
    last_published_at: str | None = None
    _extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "target_id": self.target_id,
            "kind": self.kind,
            "repo_path": self.repo_path,
            "github_owner": self.github_owner,
            "github_repo": self.github_repo,
            "default_branch": self.default_branch,
            "privacy": self.privacy,
            "created_at": self.created_at,
            "last_published_at": self.last_published_at,
        }
        out.update(self._extras)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PublishTarget":
        known = {
            "target_id", "kind", "repo_path", "github_owner", "github_repo",
            "default_branch", "privacy", "created_at", "last_published_at",
        }
        extras = {k: v for k, v in raw.items() if k not in known}
        return cls(
            target_id=str(raw.get("target_id", "")),
            kind=str(raw.get("kind", "manual_export")),
            repo_path=raw.get("repo_path"),
            github_owner=raw.get("github_owner"),
            github_repo=raw.get("github_repo"),
            default_branch=raw.get("default_branch"),
            privacy=raw.get("privacy"),
            created_at=str(raw.get("created_at") or _now()),
            last_published_at=raw.get("last_published_at"),
            _extras=extras,
        )


@dataclass
class PublishEvent:
    event_id: str
    target_id: str
    kind: str
    state: str  # planned | scanned | committed | pushed | pr_opened | failed
    branch: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "target_id": self.target_id,
            "kind": self.kind,
            "state": self.state,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pr_url": self.pr_url,
            "error": self.error,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PublishEvent":
        return cls(
            event_id=str(raw.get("event_id", "")),
            target_id=str(raw.get("target_id", "")),
            kind=str(raw.get("kind", "")),
            state=str(raw.get("state", "")),
            branch=raw.get("branch"),
            commit_sha=raw.get("commit_sha"),
            pr_url=raw.get("pr_url"),
            error=raw.get("error"),
            created_at=str(raw.get("created_at") or _now()),
        )

    def redacted(self) -> "PublishEvent":
        """A copy with every free-text string field redacted. branch/state/kind
        are constrained shapes but redacting them too is harmless + uniform; the
        load-bearing scrub is on ``pr_url`` / ``commit_sha`` (never a token, but
        a URL could) and especially ``error`` (a git/gh failure message could
        echo a path or a token)."""
        return PublishEvent(
            event_id=self.event_id,
            target_id=self.target_id,
            kind=self.kind,
            state=self.state,
            branch=_redact(self.branch),
            commit_sha=self.commit_sha,
            pr_url=_redact(self.pr_url),
            error=_redact(self.error),
            created_at=self.created_at,
        )


class PublishLedger:
    """Read/write the publish provenance for one coding project."""

    def __init__(self, project_id: str, *, root: Path | None = None) -> None:
        from errorta_export.safe_path import UnsafePathError, safe_segment
        try:
            safe_segment(project_id)
        except UnsafePathError as exc:
            raise ValueError(f"invalid project_id: {project_id!r}") from exc
        if root is None:
            from errorta_app.paths import errorta_home
            root = errorta_home() / "council" / "coding-projects"
        root = Path(root)
        self.project_id = project_id
        self.dir = root / project_id
        if not self.dir.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"project_id escapes the ledger root: {project_id!r}")

    @property
    def _targets_path(self) -> Path:
        return self.dir / "publish-targets.json"

    @property
    def _events_path(self) -> Path:
        return self.dir / "publish-events.jsonl"

    # --- targets ---------------------------------------------------------- #
    def list_targets(self) -> list[PublishTarget]:
        path = self._targets_path
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        return [PublishTarget.from_dict(t) for t in raw if isinstance(t, dict)]

    def upsert_target(
        self,
        *,
        kind: str,
        target_id: str | None = None,
        repo_path: str | None = None,
        github_owner: str | None = None,
        github_repo: str | None = None,
        default_branch: str | None = None,
        privacy: str | None = None,
        last_published_at: str | None = None,
    ) -> PublishTarget:
        if kind not in _TARGET_KINDS:
            raise ValueError(f"unknown publish target kind: {kind!r}")
        targets = self.list_targets()
        tid = target_id or uuid.uuid4().hex
        existing = next((t for t in targets if t.target_id == tid), None)
        if existing is not None:
            existing.kind = kind
            if repo_path is not None:
                existing.repo_path = repo_path
            if github_owner is not None:
                existing.github_owner = github_owner
            if github_repo is not None:
                existing.github_repo = github_repo
            if default_branch is not None:
                existing.default_branch = default_branch
            if privacy is not None:
                existing.privacy = privacy
            if last_published_at is not None:
                existing.last_published_at = last_published_at
            target = existing
        else:
            target = PublishTarget(
                target_id=tid,
                kind=kind,
                repo_path=repo_path,
                github_owner=github_owner,
                github_repo=github_repo,
                default_branch=default_branch,
                privacy=privacy,
                last_published_at=last_published_at,
            )
            targets.append(target)
        self._write_targets(targets)
        return target

    def _write_targets(self, targets: list[PublishTarget]) -> None:
        path = self._targets_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [t.to_dict() for t in targets]
        fd, tmp = tempfile.mkstemp(prefix=".publish-targets-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # --- events ----------------------------------------------------------- #
    def list_events(self) -> list[PublishEvent]:
        path = self._events_path
        if not path.exists():
            return []
        out: list[PublishEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(PublishEvent.from_dict(json.loads(line)))
            except (ValueError, TypeError):
                continue
        return out

    def append_event(
        self,
        *,
        target_id: str,
        kind: str,
        state: str,
        branch: str | None = None,
        commit_sha: str | None = None,
        pr_url: str | None = None,
        error: str | None = None,
    ) -> PublishEvent:
        if state not in _EVENT_STATES:
            raise ValueError(f"unknown publish event state: {state!r}")
        event = PublishEvent(
            event_id=uuid.uuid4().hex,
            target_id=target_id,
            kind=kind,
            state=state,
            branch=branch,
            commit_sha=commit_sha,
            pr_url=pr_url,
            error=error,
        ).redacted()
        path = self._events_path
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return event


__all__ = ["PublishTarget", "PublishEvent", "PublishLedger"]
