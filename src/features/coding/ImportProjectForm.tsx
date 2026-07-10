// F135 — import an existing project from a local folder or a GitHub repo.
// Sits alongside the greenfield Create form. Both sources converge on a normal
// target="existing" project; GitHub also connects the repo for the PR handoff.
import { useCallback, useEffect, useRef, useState } from "react";

import * as api from "../../lib/api/coding";
import { pickPaths } from "../shell/FilePickerDialog";
import { PROJECT_ID_HINT, validateProjectId } from "./projectId";

type Source = "local" | "github";

export default function ImportProjectForm({
  onCreated,
  onError,
}: {
  onCreated: (id: string) => void;
  onError: (msg: string) => void;
}) {
  // Guard the clone poll loop so it doesn't setState / navigate after unmount.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [source, setSource] = useState<Source>("local");
  const [id, setId] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  // local
  const [folderPath, setFolderPath] = useState("");
  const [gitInit, setGitInit] = useState(false);

  // github
  const [repoUrl, setRepoUrl] = useState("");
  const [ref, setRef] = useState("");
  const [destinationRoot, setDestinationRoot] = useState("");
  const [shallow, setShallow] = useState(true);
  const [auth, setAuth] = useState<api.GithubAuthStatus | null>(null);

  // WS-C branch discovery: populated from the remote once a GitHub URL is typed.
  const [branches, setBranches] = useState<string[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(false);
  const [branchesFailed, setBranchesFailed] = useState(false);
  // Monotonic token so a stale (superseded) branches response is ignored.
  const branchReq = useRef(0);

  useEffect(() => {
    if (source !== "github") return;
    api.importGithubAuthStatus().then(setAuth).catch(() => setAuth(null));
  }, [source]);

  const looksLikeGithubUrl = (u: string) =>
    /^https:\/\/github\.com\/[^/\s]+\/[^/\s]+/.test(u) ||
    /^git@github\.com:[^/\s]+\/[^/\s]+/.test(u) ||
    /^ssh:\/\/git@github\.com\/[^/\s]+\/[^/\s]+/.test(u);

  // Debounced remote-branch lookup. On success populate the dropdown and
  // preselect the default branch; on any failure fall back to the free-text
  // field (branchesFailed) — never block the import.
  useEffect(() => {
    if (source !== "github") return;
    const url = repoUrl.trim();
    if (!looksLikeGithubUrl(url)) {
      setBranches([]);
      setBranchesFailed(false);
      setBranchesLoading(false);
      return;
    }
    const token = ++branchReq.current;
    setBranchesLoading(true);
    setBranchesFailed(false);
    const timer = setTimeout(() => {
      api
        .importGithubBranches(url)
        .then((res) => {
          if (token !== branchReq.current) return; // stale
          if (res.ok && res.branches.length > 0) {
            setBranches(res.branches);
            setBranchesFailed(false);
            // Always leave `ref` on a branch that exists in the dropdown so the
            // controlled <select> value matches an <option> (no phantom
            // first-option selection / submitted-value divergence).
            setRef((cur) =>
              cur && res.branches.includes(cur)
                ? cur
                : res.defaultBranch && res.branches.includes(res.defaultBranch)
                  ? res.defaultBranch
                  : res.branches[0],
            );
          } else {
            setBranches([]);
            setBranchesFailed(true);
          }
        })
        .catch(() => {
          if (token !== branchReq.current) return;
          setBranches([]);
          setBranchesFailed(true);
        })
        .finally(() => {
          if (token === branchReq.current) setBranchesLoading(false);
        });
    }, 400);
    return () => clearTimeout(timer);
  }, [source, repoUrl]);

  const browse = useCallback(async (apply: (p: string) => void) => {
    setNote(null);
    try {
      const picked = await pickPaths({ directory: true, multiple: false, requireAbsolutePath: true });
      if (picked.length > 0 && picked[0]) apply(picked[0]);
      else setNote("Folder picker is unavailable here — paste an absolute path instead.");
    } catch {
      setNote("Folder picker is unavailable here — paste an absolute path instead.");
    }
  }, []);

  const ghConnected = Boolean(auth?.ghPresent && auth?.login);

  const submitLocal = async () => {
    try {
      await api.importLocalProject({
        projectId: id.trim(),
        folderPath: folderPath.trim(),
        gitInit,
        confirm: gitInit, // git init mutates the user's folder — treat toggling it as the confirm
      });
      onCreated(id.trim());
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    }
  };

  const submitGithub = async () => {
    try {
      const job = await api.importGithubClone({
        projectId: id.trim(),
        repoUrl: repoUrl.trim(),
        ref: ref.trim() || null,
        destinationRoot: destinationRoot.trim() || null,
        shallow,
      });
      // poll the clone job to completion
      setNote("Cloning…");
      let status = job;
      for (let i = 0; i < 600 && status.status !== "done" && status.status !== "error"; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        if (!mounted.current) return;
        status = await api.importGithubCloneStatus(job.jobId);
      }
      if (!mounted.current) return;
      if (status.status === "done" && status.projectId) {
        setNote(null);
        onCreated(status.projectId);
      } else {
        onError(`Clone failed: ${status.message ?? status.status}`);
        setNote(null);
      }
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
      setNote(null);
    }
  };

  const idError = validateProjectId(id);
  const canImport =
    !busy &&
    id.trim() !== "" &&
    idError === null &&
    !(source === "github" && !ghConnected);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canImport) return;
    setBusy(true);
    try {
      if (source === "local") await submitLocal();
      else await submitGithub();
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="coding-create coding-import" aria-label="Import project" onSubmit={onSubmit}>
      <div className="coding-field">
        <label className="coding-field-label" htmlFor="coding-import-source">
          Import from
        </label>
        <select
          id="coding-import-source"
          value={source}
          onChange={(e) => setSource(e.target.value as Source)}
          aria-label="Import source"
        >
          <option value="local">Local folder</option>
          <option value="github">GitHub repository</option>
        </select>
      </div>

      <div className="coding-field">
        <label className="coding-field-label" htmlFor="coding-import-id">
          Project ID
          <span className="coding-field-hint">{PROJECT_ID_HINT}</span>
        </label>
        <input
          id="coding-import-id"
          value={id}
          onChange={(e) => setId(e.target.value)}
          placeholder="my-imported-project"
          aria-label="Import project id"
          aria-invalid={idError !== null}
          aria-describedby={idError !== null ? "coding-import-id-error" : undefined}
        />
        {idError !== null ? (
          <p id="coding-import-id-error" className="coding-field-error" role="alert">
            {idError}
          </p>
        ) : null}
      </div>

      {source === "local" ? (
        <div className="coding-field coding-field-wide">
          <label className="coding-field-label" htmlFor="coding-import-folder">
            Folder
          </label>
          <div className="coding-location-row">
            <input
              id="coding-import-folder"
              value={folderPath}
              onChange={(e) => setFolderPath(e.target.value)}
              placeholder="/path/to/your/project"
              aria-label="Folder path"
            />
            <button
              type="button"
              className="coding-btn coding-btn-ghost"
              onClick={() => void browse(setFolderPath)}
              aria-label="Browse for folder"
            >
              Browse…
            </button>
          </div>
          <label className="coding-import-check">
            <input
              type="checkbox"
              checked={gitInit}
              onChange={(e) => setGitInit(e.target.checked)}
            />{" "}
            Initialize git if this folder isn't a repo yet (writes a .git to the folder)
          </label>
        </div>
      ) : (
        <>
          {!ghConnected ? (
            <p className="coding-location-note" role="status">
              {auth?.ghPresent
                ? "GitHub CLI is present but not logged in. Run `gh auth login`, then reopen this."
                : "Connect GitHub: install the GitHub CLI (`gh`) and run `gh auth login`."}
            </p>
          ) : (
            <p className="coding-location-help">Connected as {auth?.login}.</p>
          )}
          <div className="coding-field coding-field-wide">
            <label className="coding-field-label" htmlFor="coding-import-url">
              Repository URL
            </label>
            <input
              id="coding-import-url"
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              placeholder="https://github.com/owner/repo"
              aria-label="Repository URL"
            />
          </div>
          <div className="coding-field">
            <label className="coding-field-label" htmlFor="coding-import-ref">
              Branch <span className="coding-field-hint">Optional</span>
            </label>
            {branches.length > 0 ? (
              <select
                id="coding-import-ref"
                value={ref}
                onChange={(e) => setRef(e.target.value)}
                aria-label="Branch"
              >
                {branches.map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id="coding-import-ref"
                value={ref}
                onChange={(e) => setRef(e.target.value)}
                placeholder="main"
                aria-label="Branch"
              />
            )}
            {branchesLoading ? (
              <span className="coding-field-hint" role="status">
                Loading branches…
              </span>
            ) : branchesFailed ? (
              <span className="coding-field-hint" role="status">
                Couldn't list branches — type a branch name, or leave blank for
                the default.
              </span>
            ) : null}
          </div>
          <div className="coding-field coding-field-wide">
            <label className="coding-field-label" htmlFor="coding-import-dest">
              Clone into <span className="coding-field-hint">Optional</span>
            </label>
            <div className="coding-location-row">
              <input
                id="coding-import-dest"
                value={destinationRoot}
                onChange={(e) => setDestinationRoot(e.target.value)}
                placeholder="Default: ~/Errorta Projects/_repos"
                aria-label="Clone destination"
              />
              <button
                type="button"
                className="coding-btn coding-btn-ghost"
                onClick={() => void browse(setDestinationRoot)}
                aria-label="Browse for clone destination"
              >
                Browse…
              </button>
            </div>
            <label className="coding-import-check">
              <input
                type="checkbox"
                checked={shallow}
                onChange={(e) => setShallow(e.target.checked)}
              />{" "}
              Shallow clone (faster for large repos)
            </label>
          </div>
        </>
      )}

      {note ? (
        <p className="coding-location-note" role="status">
          {note}
        </p>
      ) : null}

      <button
        type="submit"
        className="coding-btn coding-btn-primary"
        disabled={!canImport}
      >
        {busy ? "Importing…" : "Import project"}
      </button>
    </form>
  );
}
