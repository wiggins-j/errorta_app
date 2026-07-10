// F102 P1 + P2 + P3/P4 — Publish panel.
//
// Frontend-only handoff surface. P1 (manual export: zip / patch / git apply /
// open-folder) is always available with no GitHub auth. P2 is a read-only
// auth-status line (gh present? logged-in login?).
//
// P3/P4 (GitHub push paths) are enabled ONLY when the user's `gh` CLI is present
// AND logged in AND the project is accepted/delivered (with no open tasks).
// Otherwise the GitHub actions stay disabled with a precise hint. Every push is
// an explicit, confirmed click; a secret-scan hit (409) surfaces the redacted
// findings + an explicit "Publish anyway (override)"; a clobber refusal (409)
// surfaces the unrelated dirty paths.
//
// Security: the backend redacts events and NEVER returns a GitHub token. This
// panel never renders a token either — auth-status carries only a boolean and a
// login, and there is no code path that prints a credential.
import { useCallback, useEffect, useState } from "react";

import * as api from "../../lib/api/coding";
import type {
  ManualExportKind,
  ManualExportResult,
  PublishAuthStatus,
  PublishEvent,
  PublishPrResult,
  PublishRepoResult,
  PublishScanFinding,
} from "../../lib/api/coding";
import { PublishBlocked } from "../../lib/api/coding";

export interface PublishPanelProps {
  projectId: string;
  // F102-01: the accept/delivered gate is prop-driven from the parent's live
  // project state (refreshed on every accept via load()). The panel used to fetch
  // this itself once on mount, which left the GitHub actions stuck-disabled after
  // an in-session accept. Required (single caller) so tsc enforces a live value.
  delivered: boolean;
}

function isNotFoundError(err: unknown): boolean {
  return err instanceof Error && /\(404\)/.test(err.message);
}

async function openExternalFolder(path: string): Promise<void> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_path", { path });
  } catch {
    throw new Error(`Open this path: ${path}`);
  }
}

function authLine(status: PublishAuthStatus | null): string {
  if (!status) return "GitHub: checking…";
  if (!status.ghPresent) return "GitHub: not connected";
  if (status.login) return `GitHub: gh detected — logged in as ${status.login}`;
  return "GitHub: gh detected — not logged in";
}

function navigateToSettings(): void {
  window.dispatchEvent(new CustomEvent("errorta:navigate", { detail: { view: "settings" } }));
}

// Human-readable text for each merge-gate blocker code the publish routes can
// return. Falls back to the raw code so a new backend code still shows something.
const GATE_BLOCKER_LABELS: Record<string, string> = {
  open_tasks: "some tasks aren't done yet",
  open_blockers: "some tasks are still blocked",
  preview_unavailable: "the delivered changes couldn't be read (missing or corrupt worktree)",
  unreviewed_changes: "the changes haven't been reviewed yet",
  review_rejected: "the reviewer rejected the latest changes",
  pm_unreviewed_changes: "the PM hasn't reviewed the changes yet",
  pm_review_rejected: "the PM rejected the latest changes",
  tests_missing: "tests haven't run for the delivered changes yet",
  tests_failing: "the latest test run failed",
  file_conflicts: "there are unresolved file conflicts",
  definition_of_done: "the definition of done isn't met yet",
  implementer_not_grounded: "the work wasn't grounded in the bound corpus",
  publish_gate_blocked: "the publish checks didn't pass",
};

function describeBlocker(code: string): string {
  return GATE_BLOCKER_LABELS[code] ?? code;
}

// Compose the user-facing message for a gate refusal. Prefers the full blocker
// list (each mapped to friendly text); falls back to the single reason code.
function publishBlockedMessage(err: PublishBlocked): string {
  const codes = err.blockers && err.blockers.length > 0 ? err.blockers : [err.reason];
  return `Publish blocked: ${codes.map(describeBlocker).join("; ")}.`;
}

function ScanFindings({ findings }: { findings: PublishScanFinding[] }) {
  return (
    <ul className="coding-publish-findings" aria-label="Secret scan findings">
      {findings.map((f, i) => (
        <li key={`${f.path}-${f.kind}-${i}`} className="coding-publish-finding">
          <span className="coding-publish-finding-path">{f.path}</span>
          <span className="coding-publish-finding-kind">{f.kind}</span>
          {f.line != null ? (
            <span className="coding-publish-finding-line">line {f.line}</span>
          ) : null}
          {f.redactedExcerpt ? (
            <code className="coding-publish-finding-excerpt">{f.redactedExcerpt}</code>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export default function PublishPanel({ projectId, delivered }: PublishPanelProps) {
  const [available, setAvailable] = useState(true);
  const [busy, setBusy] = useState<ManualExportKind | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [result, setResult] = useState<ManualExportResult | null>(null);
  const [auth, setAuth] = useState<PublishAuthStatus | null>(null);
  const [events, setEvents] = useState<PublishEvent[]>([]);

  // ── P3 (existing-repo PR) state ──
  const [prBusy, setPrBusy] = useState(false);
  const [prResult, setPrResult] = useState<PublishPrResult | null>(null);
  const [prError, setPrError] = useState<string | null>(null);
  const [prScanFindings, setPrScanFindings] = useState<PublishScanFinding[] | null>(null);
  const [prClobberPaths, setPrClobberPaths] = useState<string[] | null>(null);

  // ── P4 (new GitHub repo) state ──
  const [repoName, setRepoName] = useState("");
  const [repoPrivate, setRepoPrivate] = useState(true);
  const [repoLocalOnly, setRepoLocalOnly] = useState(false);
  const [repoBusy, setRepoBusy] = useState(false);
  const [repoResult, setRepoResult] = useState<PublishRepoResult | null>(null);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [repoScanFindings, setRepoScanFindings] = useState<PublishScanFinding[] | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [a, e] = await Promise.all([
        api.getPublishAuthStatus(projectId),
        api.getPublishEvents(projectId),
      ]);
      setAvailable(true);
      setAuth(a);
      setEvents(e);
    } catch (err) {
      if (isNotFoundError(err)) {
        setAvailable(false);
        return;
      }
      setError(err instanceof Error ? err.message : "Could not load publish status.");
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runExport = (kind: ManualExportKind, after?: (r: ManualExportResult) => void | Promise<void>) => {
    void (async () => {
      setBusy(kind);
      setError(null);
      setInfo(null);
      try {
        const r = await api.manualExport(projectId, kind);
        setResult(r);
        if (after) await after(r);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(null);
      }
    })();
  };

  const exportZip = () => runExport("zip", (r) => setInfo(r.path ? `Exported zip: ${r.path}` : "Zip exported."));

  const copyGitApply = () =>
    runExport("git_apply", async (r) => {
      if (r.command) {
        try {
          await navigator.clipboard?.writeText(r.command);
          setInfo(`Copied: ${r.command}`);
        } catch {
          setInfo(`Run: ${r.command}`);
        }
      } else {
        setInfo("No git apply command was returned.");
      }
    });

  const openFolder = () =>
    runExport("open_folder", async (r) => {
      if (r.path) {
        try {
          await openExternalFolder(r.path);
          setInfo(`Opened: ${r.path}`);
        } catch (err) {
          setInfo(err instanceof Error ? err.message : `Folder: ${r.path}`);
        }
      }
    });

  const viewPatch = () => runExport("patch", (r) => setInfo(r.diff ? "Patch ready below." : "No changes to export."));

  const copyPatch = () => {
    if (!result?.diff) return;
    void (async () => {
      try {
        await navigator.clipboard?.writeText(result.diff ?? "");
        setInfo("Patch copied.");
      } catch {
        setInfo("Could not copy the patch.");
      }
    })();
  };

  const ghReady = Boolean(auth?.ghPresent && auth?.login);
  const githubEnabled = ghReady && delivered;
  const localRepoEnabled = delivered;
  const repoActionEnabled = repoLocalOnly ? localRepoEnabled : githubEnabled;
  const anyBusy = busy !== null || prBusy || repoBusy;

  const githubHint = !delivered
    ? "Accept the project first to publish to GitHub"
    : !ghReady
      ? "Connect GitHub to push, or create a local git repo only."
      : null;

  // ── P3: open PR on existing repo ──
  const openPr = (override: boolean) => {
    void (async () => {
      setPrBusy(true);
      setPrError(null);
      setPrScanFindings(null);
      setPrClobberPaths(null);
      if (!override) setPrResult(null);
      try {
        const r = await api.publishExistingRepoPr(projectId, { override });
        setPrResult(r);
        await refresh();
      } catch (err) {
        if (err instanceof PublishBlocked) {
          if (err.reason === "secret_scan_hit") {
            setPrScanFindings(err.findings ?? []);
            setPrError("Secret scan found sensitive content in the files to push.");
          } else if (err.reason === "clobber_unrelated_changes") {
            setPrClobberPaths(err.dirtyPaths ?? []);
            setPrError("Refused: unrelated local changes would be clobbered.");
          } else {
            setPrError(publishBlockedMessage(err));
          }
        } else {
          setPrError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        setPrBusy(false);
      }
    })();
  };

  // ── P4: create new GitHub repo (or local-only) ──
  const createRepo = (override: boolean) => {
    void (async () => {
      setRepoBusy(true);
      setRepoError(null);
      setRepoScanFindings(null);
      if (!override) setRepoResult(null);
      try {
        const r = await api.publishNewGithubRepo(projectId, {
          repoName,
          private: repoPrivate,
          localOnly: repoLocalOnly,
          override,
        });
        setRepoResult(r);
        await refresh();
      } catch (err) {
        if (err instanceof PublishBlocked) {
          if (err.reason === "secret_scan_hit") {
            setRepoScanFindings(err.findings ?? []);
            setRepoError("Secret scan found sensitive content in the files to commit.");
          } else if (err.reason === "invalid_repo_name") {
            setRepoError("That repo name is not valid. Use letters, numbers, '-', '_', or '.'.");
          } else if (err.reason === "local_dest_exists") {
            setRepoError("A local repo for this project already exists. Remove or rename it first.");
          } else {
            setRepoError(publishBlockedMessage(err));
          }
        } else {
          setRepoError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        setRepoBusy(false);
      }
    })();
  };

  if (!available) {
    return (
      <details className="coding-panel coding-publish">
        <summary>
          <span>Publish</span>
        </summary>
        <section aria-label="Publish">
          <div className="coding-publish-empty">
            <h4>Publishing unavailable</h4>
            <p>This sidecar build does not expose publishing routes yet.</p>
          </div>
        </section>
      </details>
    );
  }

  return (
    <details className="coding-panel coding-publish">
      <summary>
        <span>Publish</span>
        <span className="coding-count">{events.length}</span>
      </summary>
      <section aria-label="Publish">
        {error ? (
          <p className="coding-error" role="alert">
            {error}
          </p>
        ) : null}
        {info ? (
          <p className="coding-publish-info" role="status">
            {info}
          </p>
        ) : null}

        <div className="coding-publish-section" aria-label="Manual export">
          <h4>Manual export</h4>
          <p className="coding-file-note">No GitHub account required — works offline.</p>
          <div className="coding-publish-actions">
            <button
              type="button"
              className="coding-btn coding-btn-small"
              onClick={exportZip}
              disabled={busy !== null}
            >
              Export .zip
            </button>
            <button
              type="button"
              className="coding-btn coding-btn-small"
              onClick={copyGitApply}
              disabled={busy !== null}
            >
              Copy <code>git apply</code> command
            </button>
            <button
              type="button"
              className="coding-btn coding-btn-small"
              onClick={openFolder}
              disabled={busy !== null}
            >
              Open delivered folder
            </button>
            <button
              type="button"
              className="coding-btn coding-btn-small"
              onClick={viewPatch}
              disabled={busy !== null}
            >
              View patch
            </button>
          </div>

          {result?.kind === "git_apply" && result.command ? (
            <pre className="coding-publish-command" aria-label="git apply command">
              {result.command}
            </pre>
          ) : null}

          {result?.kind === "patch" ? (
            result.diff ? (
              <div className="coding-publish-patch">
                <div className="coding-publish-actions">
                  <button
                    type="button"
                    className="coding-btn coding-btn-small"
                    onClick={copyPatch}
                  >
                    Copy patch
                  </button>
                </div>
                <pre className="coding-diff" aria-label="Patch diff">
                  {result.diff}
                </pre>
              </div>
            ) : (
              <p className="coding-empty">No changes to export.</p>
            )
          ) : null}
        </div>

        <div className="coding-publish-section" aria-label="GitHub">
          <h4>GitHub</h4>
          <p className="coding-publish-auth" role="status">
            {authLine(auth)}
          </p>
          {githubHint ? <p className="coding-file-note">{githubHint}</p> : null}
          {!ghReady ? (
            <div className="coding-publish-actions">
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={navigateToSettings}
              >
                Connect GitHub
              </button>
            </div>
          ) : null}

          {/* P3 — Open PR on an existing repo */}
          <div className="coding-publish-subsection" aria-label="Open PR on existing repo">
            <h5>Open PR on existing repo</h5>
            <p className="coding-file-note">
              Pushes branch <code>errorta/{projectId}</code> to the repo&apos;s{" "}
              <code>origin</code> and opens a PR into its default branch. Never
              commits to the default branch directly.
            </p>
            <div className="coding-publish-actions">
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={() => openPr(false)}
                disabled={!githubEnabled || anyBusy}
              >
                {prBusy ? "Opening PR…" : "Open PR"}
              </button>
            </div>
            {prError ? (
              <p className="coding-error" role="alert">
                {prError}
              </p>
            ) : null}
            {prClobberPaths ? (
              <div className="coding-publish-clobber">
                <p className="coding-file-note">
                  These local changes are not part of the accepted work and would be
                  clobbered:
                </p>
                <ul className="coding-publish-dirty" aria-label="Unrelated changed paths">
                  {prClobberPaths.map((p) => (
                    <li key={p}>
                      <code>{p}</code>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {prScanFindings ? (
              <div className="coding-publish-scan" aria-label="PR secret scan">
                <p className="coding-error" role="alert">
                  The secret scan flagged the following. Review carefully — pushing
                  could leak credentials.
                </p>
                <ScanFindings findings={prScanFindings} />
                <div className="coding-publish-actions">
                  <button
                    type="button"
                    className="coding-btn coding-btn-small coding-btn-override"
                    onClick={() => openPr(true)}
                    disabled={anyBusy}
                  >
                    Publish anyway (override)
                  </button>
                </div>
              </div>
            ) : null}
            {prResult?.prUrl ? (
              <p className="coding-publish-info" role="status">
                PR opened:{" "}
                <a href={prResult.prUrl} target="_blank" rel="noopener noreferrer">
                  {prResult.prUrl}
                </a>
              </p>
            ) : null}
          </div>

          {/* P4 — Create a new GitHub repo */}
          <div className="coding-publish-subsection" aria-label="Create new GitHub repo">
            <h5>Create new GitHub repo</h5>
            <label className="coding-publish-field">
              <span>Repo name</span>
              <input
                type="text"
                value={repoName}
                onChange={(e) => setRepoName(e.target.value)}
                placeholder="my-project"
                disabled={!repoActionEnabled || anyBusy}
                aria-label="New repo name"
              />
            </label>
            <label className="coding-publish-toggle">
              <input
                type="checkbox"
                checked={repoPrivate}
                onChange={(e) => setRepoPrivate(e.target.checked)}
                disabled={!githubEnabled || anyBusy || repoLocalOnly}
                aria-label="Private repo"
              />
              <span>Private</span>
            </label>
            <label className="coding-publish-toggle">
              <input
                type="checkbox"
                checked={repoLocalOnly}
                onChange={(e) => setRepoLocalOnly(e.target.checked)}
                disabled={!localRepoEnabled || anyBusy}
                aria-label="Create local git repo only"
              />
              <span>Create local git repo only (no GitHub)</span>
            </label>
            <div className="coding-publish-actions">
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={() => createRepo(false)}
                disabled={!repoActionEnabled || anyBusy || repoName.trim() === ""}
              >
                {repoBusy ? "Creating…" : "Create repo"}
              </button>
            </div>
            {repoError ? (
              <p className="coding-error" role="alert">
                {repoError}
              </p>
            ) : null}
            {repoScanFindings ? (
              <div className="coding-publish-scan" aria-label="Repo secret scan">
                <p className="coding-error" role="alert">
                  The secret scan flagged the following. Review carefully — pushing
                  could leak credentials.
                </p>
                <ScanFindings findings={repoScanFindings} />
                <div className="coding-publish-actions">
                  <button
                    type="button"
                    className="coding-btn coding-btn-small coding-btn-override"
                    onClick={() => createRepo(true)}
                    disabled={anyBusy}
                  >
                    Publish anyway (override)
                  </button>
                </div>
              </div>
            ) : null}
            {repoResult ? (
              <div className="coding-publish-repo-result">
                {repoResult.repoUrl ? (
                  <p className="coding-publish-info" role="status">
                    Repo created:{" "}
                    <a href={repoResult.repoUrl} target="_blank" rel="noopener noreferrer">
                      {repoResult.repoUrl}
                    </a>
                  </p>
                ) : repoResult.localPath ? (
                  <p className="coding-publish-info" role="status">
                    Local git repo created: <code>{repoResult.localPath}</code>
                  </p>
                ) : null}
                {repoResult.fileList.length > 0 ? (
                  <>
                    <p className="coding-file-note">Initial commit files:</p>
                    <ul className="coding-publish-filelist" aria-label="Initial commit files">
                      {repoResult.fileList.map((f) => (
                        <li key={f}>
                          <code>{f}</code>
                        </li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>

        <div className="coding-publish-section" aria-label="Publish history">
          <h4>Publish history</h4>
          {events.length === 0 ? (
            <p className="coding-empty">No publish activity yet.</p>
          ) : (
            <ul className="coding-publish-log" aria-label="Publish events">
              {[...events].reverse().map((ev) => (
                <li key={ev.eventId} className={`coding-publish-event coding-publish-state-${ev.state}`}>
                  <span className="coding-publish-event-kind">{ev.kind}</span>
                  <span className="coding-publish-event-state">{ev.state}</span>
                  {ev.branch ? <span className="coding-publish-event-branch">{ev.branch}</span> : null}
                  {ev.prUrl ? (
                    <a href={ev.prUrl} target="_blank" rel="noopener noreferrer">
                      PR
                    </a>
                  ) : null}
                  {ev.error ? <span className="coding-publish-event-error">{ev.error}</span> : null}
                  <span className="coding-publish-event-time">{ev.createdAt}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
    </details>
  );
}
