// F087-06 — Coding Mode shell/container. Lists projects, creates them, and
// renders the live project view wired to the coding API + the autonomous run.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, RefObject } from "react";

import "./coding.css";
import BrainstormViewer from "./BrainstormViewer";
import CodingProjectView from "./CodingProjectView";
import AttentionFeed, {
  PreflightBlockedBanner,
  type PreflightUnhealthy,
} from "./AttentionFeed";
import GovernancePanel from "./GovernancePanel";
import GovernanceStatusPanel from "./GovernanceStatusPanel";
import GroundingPanel from "./GroundingPanel";
import PublishPanel from "./PublishPanel";
import RunPreviewPanel from "./RunPreviewPanel";
import TeamLog from "./TeamLog";
import TokenUsagePanel from "./TokenUsagePanel";
import * as api from "../../lib/api/coding";
import { deriveRunPhase, type RunIntent } from "./runPhase";
import RunSetupGate from "./RunSetupGate";
import { CAREFUL_PRESET } from "./runSetupPresets";
import { pickPaths } from "../shell/FilePickerDialog";
import ImportProjectForm from "./ImportProjectForm";
import AiWizard from "./AiWizard";
import PmControlPanel from "./PmControlPanel";
import { PROJECT_ID_HINT, validateProjectId } from "./projectId";
import OnboardingPanel from "./OnboardingPanel";
import { SidecarUnreachableError } from "../../lib/api";
import { listRooms } from "../../lib/api/council";
import { getRoomFull } from "../../lib/api/councilRoom";
import { APP_NAVIGATION_EVENT } from "../../lib/featureNavigation";
import type { CouncilRoomSummary } from "../council/types";

type ProjectSummary = api.CodingProjectSummary;
type CodingTeamMember = {
  id: string;
  role: string;
  name: string;
  model: string;
  modelTitle?: string;
};

const AUTO_ROLE_ORDER = ["pm", "dev", "reviewer", "tester"] as const;

// F146 Slice A: merge-gate blockers that fire because the *integrated* delivered
// head was never reviewed/tested as a unit — each PR was reviewed at its own head
// during the run, but the final merge produced new commits nothing signed off on.
// When any of these are present the modal shows an explainer so the message isn't
// baffling on a finished project.
const HEAD_BINDING_BLOCKERS = new Set([
  "unreviewed_changes",
  "pm_unreviewed_changes",
  "tests_missing",
]);

// F121 Part A: how long the optimistic "Starting workers…" state holds before
// it falls back to an error if the run never reaches `running`. Sized to a few
// poll ticks (2.5s each) so a slow spin-up isn't misreported as a failure.
const START_TIMEOUT_MS = 12_000;

function codingRoleLabel(role: string): string {
  const normalized = role.toLowerCase();
  if (normalized === "pm") return "PM";
  if (normalized === "dev") return "DEV";
  if (normalized === "reviewer") return "REV";
  if (normalized === "tester") return "TEST";
  return normalized ? normalized.toUpperCase() : "DEV";
}

function projectDisplayName(project: { id: string; displayName?: string }): string {
  return project.displayName?.trim() || project.id;
}

function statusClassName(status: string): string {
  const token = status.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `coding-status coding-status-${token || "unknown"}`;
}

function projectLocation(
  project: api.CodingProject,
  delivery: api.Delivery | null,
): { label: string; value: string } | null {
  if (project.target === "existing") {
    const repoPath = project.repoPath?.trim();
    return repoPath ? { label: "Repo", value: repoPath } : null;
  }

  const deliveryPath = (delivery?.deliveredTo ?? project.plannedDeliveryDir ?? "").trim();
  return deliveryPath ? { label: "Delivery", value: deliveryPath } : null;
}

function stringField(raw: Record<string, unknown>, key: string): string {
  const value = raw[key];
  return typeof value === "string" ? value : "";
}

function teamMembersFromRoom(room: Record<string, unknown>): CodingTeamMember[] {
  const rawMembers = Array.isArray(room.members)
    ? (room.members as Array<Record<string, unknown>>)
    : [];
  const enabled = rawMembers.filter((m) => m.enabled !== false);
  const hasExplicitRole = enabled.some((m) => {
    const metadata = (m.metadata ?? {}) as Record<string, unknown>;
    return typeof metadata.coding_role === "string" && metadata.coding_role;
  });
  return enabled.map((m, idx) => {
    const metadata = (m.metadata ?? {}) as Record<string, unknown>;
    const role =
      hasExplicitRole && typeof metadata.coding_role === "string" && metadata.coding_role
        ? metadata.coding_role
        : AUTO_ROLE_ORDER[idx] ?? "dev";
    return {
      id: stringField(m, "id") || `member-${idx + 1}`,
      role: codingRoleLabel(role),
      name: stringField(m, "name") || stringField(m, "id") || `Member ${idx + 1}`,
      model:
        stringField(m, "model_mode") === "multi"
          ? `multi-model · ${Array.isArray(m.model_pool) ? m.model_pool.length : 0} routes`
          : stringField(m, "model_display") || stringField(m, "gateway_route_id") || "model unset",
      modelTitle:
        stringField(m, "model_mode") === "multi" && Array.isArray(m.model_pool)
          ? `Pool: ${m.model_pool.map(String).join(", ")}`
          : undefined,
    };
  });
}

function CodingProjectHeader({
  project,
  northStarButtonRef,
  onBack,
  onOpenNorthStar,
}: {
  project: api.CodingProject;
  northStarButtonRef: RefObject<HTMLButtonElement>;
  onBack: () => void;
  onOpenNorthStar: () => void;
}) {
  const title = projectDisplayName(project);
  return (
    <header className="coding-project-topbar" aria-label="Project navigation">
      <button type="button" className="coding-btn coding-back" onClick={onBack}>
        ← All projects
      </button>
      <div className="coding-project-title-cluster">
        <h2>{title}</h2>
        <button
          type="button"
          className="coding-btn coding-btn-small"
          ref={northStarButtonRef}
          onClick={onOpenNorthStar}
        >
          North Star
        </button>
      </div>
      <span className={statusClassName(project.status)}>{project.status}</span>
    </header>
  );
}

function NorthStarDialog({
  projectTitle,
  northStar,
  onClose,
  onSave,
}: {
  projectTitle: string;
  northStar: string;
  onClose: () => void;
  onSave: (nextNorthStar: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(northStar);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    setDraft(northStar);
    setSaving(false);
    setError(null);
    window.setTimeout(() => textareaRef.current?.focus(), 0);
  }, [northStar]);

  const close = () => {
    if (!saving) onClose();
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
      return;
    }
    if (e.key !== "Tab") return;
    const panel = panelRef.current;
    if (!panel) return;
    const focusable = panel.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === panel)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const save = async () => {
    if (draft === northStar || saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(draft);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="coding-northstar-dialog"
      role="dialog"
      aria-modal="true"
      aria-label={`North Star for ${projectTitle}`}
      onKeyDown={onKeyDown}
    >
      <div className="coding-northstar-panel" ref={panelRef} tabIndex={-1}>
        <div className="coding-northstar-head">
          <h3>North Star</h3>
          <button type="button" className="coding-btn coding-btn-small" onClick={close}>
            Cancel
          </button>
        </div>
        <label className="coding-field-label" htmlFor="coding-northstar-editor">
          {projectTitle}
        </label>
        <textarea
          id="coding-northstar-editor"
          ref={textareaRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={6}
          aria-label="North Star text"
        />
        {error ? (
          <p className="coding-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="coding-northstar-actions">
          <button type="button" className="coding-btn" onClick={close} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="coding-btn coding-btn-accept"
            onClick={() => void save()}
            disabled={saving || draft === northStar}
          >
            {saving ? "Saving…" : "Save North Star"}
          </button>
        </div>
      </div>
    </div>
  );
}

function AutoOpenRunSetup({
  enabled,
  confirmed,
  onOpen,
  onConsumed,
}: {
  enabled: boolean;
  confirmed: boolean;
  onOpen: () => Promise<void>;
  onConsumed: () => void;
}) {
  const opened = useRef(false);
  useEffect(() => {
    if (!enabled || opened.current) return;
    opened.current = true;
    onConsumed();
    if (!confirmed) void onOpen();
  }, [confirmed, enabled, onConsumed, onOpen]);
  return null;
}

export default function CodingShell() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [setupOnLoadProjectId, setSetupOnLoadProjectId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  // F140: the create/import forms live in collapsible panels. Default open state is
  // decided ONCE from the first settled fetch (open when 0 projects for onboarding,
  // collapsed when >=1). Pre-load default is collapsed so a returning user never sees
  // an open-then-collapse flash; a first-run user's create form opens once loaded.
  const [listLoaded, setListLoaded] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const panelsSeeded = useRef(false);

  const refreshList = useCallback(async () => {
    try {
      setProjects(await api.listProjects());
      setListLoaded(true); // a real list settled — safe to seed the panel defaults
      setError(null); // healthy fetch -> clear any prior (possibly transient) error
    } catch (e) {
      // A transport failure is the sidecar still booting / mid-respawn — transient.
      // Don't latch the scary banner; the next call heals it.
      if (e instanceof SidecarUnreachableError) return;
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  // F140: seed the panel defaults exactly once, after the first settled load. The
  // `panelsSeeded` guard is what makes background refreshes (post-create/delete) never
  // re-collapse a panel the user opened — after this runs, the panels are user-owned.
  useEffect(() => {
    if (!listLoaded || panelsSeeded.current) return;
    panelsSeeded.current = true;
    const shouldOpen = projects.length === 0;
    setCreateOpen(shouldOpen);
    setImportOpen(shouldOpen);
  }, [listLoaded, projects.length]);

  if (selected) {
    return (
      <CodingProjectContainer
        projectId={selected}
        openSetupOnLoad={setupOnLoadProjectId === selected}
        onInitialSetupOpened={() => setSetupOnLoadProjectId(null)}
        onBack={() => {
          setSelected(null);
          void refreshList();
        }}
      />
    );
  }

  return (
    <div className="coding-shell" aria-label="Coding Team">
      <header className="coding-shell-head">
        <h2>Coding Team</h2>
        <p>An autonomous coding team: give them a North Star, they will build.</p>
      </header>
      {error ? <p className="coding-error" role="alert">{error}</p> : null}
      <details
        className="coding-panel coding-create-panel"
        open={createOpen}
        onToggle={(e) => setCreateOpen(e.currentTarget.open)}
      >
        <summary>Create a project</summary>
        <section>
          <div className="coding-wizard-cta">
            <button
              type="button"
              className="coding-btn coding-wizard-launch"
              onClick={() => setWizardOpen(true)}
            >
              AI Wizard Setup
            </button>
            <span className="coding-field-hint">
              Talk it through with the PM — it sets up a runnable project for you.
            </span>
          </div>
          <CreateProjectForm
            onCreated={async (id) => {
              await refreshList();
              setSetupOnLoadProjectId(id);
              setSelected(id);
            }}
            onError={setError}
          />
        </section>
      </details>
      {wizardOpen ? (
        <AiWizard
          onClose={() => setWizardOpen(false)}
          onCreated={async (id) => {
            setWizardOpen(false);
            await refreshList();
            setSetupOnLoadProjectId(id);
            setSelected(id);
          }}
        />
      ) : null}
      <details
        className="coding-panel coding-import-panel"
        open={importOpen}
        onToggle={(e) => setImportOpen(e.currentTarget.open)}
      >
        <summary>Import a project</summary>
        <section>
          <ImportProjectForm
            onCreated={async (id) => {
              await refreshList();
              setSetupOnLoadProjectId(id);
              setSelected(id);
            }}
            onError={setError}
          />
        </section>
      </details>
      <ul className="coding-project-list" aria-label="Projects">
        {projects.length === 0 ? (
          <li className="coding-empty">No coding projects yet.</li>
        ) : (
          projects.map((p) => (
            <li key={p.id} className="coding-project-row">
              <button
                type="button"
                className="coding-project-pick"
                onClick={() => setSelected(p.id)}
                title={p.northStar || undefined}
                aria-label={`Open project ${p.id}`}
              >
                <span className="coding-project-id">{projectDisplayName(p)}</span>
                <span className={statusClassName(p.listStatus)}>{p.listStatus}</span>
              </button>
              <button
                type="button"
                className="coding-project-delete"
                disabled={deleting === p.id}
                aria-label={`Delete project ${p.id}`}
                onClick={async () => {
                  const ok = window.confirm(
                    `Delete coding project "${p.id}"? This removes its Errorta run history and worktree, but not any external repo.`,
                  );
                  if (!ok) return;
                  setDeleting(p.id);
                  setError(null);
                  try {
                    await api.deleteProject(p.id);
                    if (selected === p.id) setSelected(null);
                    await refreshList();
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setDeleting(null);
                  }
                }}
              >
                {deleting === p.id ? "Deleting…" : "Delete"}
              </button>
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

function CreateProjectForm({
  onCreated,
  onError,
}: {
  onCreated: (id: string) => void;
  onError: (msg: string) => void;
}) {
  const [id, setId] = useState("");
  const [northStar, setNorthStar] = useState("");
  const [target, setTarget] = useState("new");
  const [repoPath, setRepoPath] = useState("");
  const [deliveryRoot, setDeliveryRoot] = useState("");
  const [pickerNote, setPickerNote] = useState<string | null>(null);
  const [corpusMode, setCorpusMode] = useState<"none" | "existing" | "build_from_repo">("none");
  const [corpusId, setCorpusId] = useState("");
  const [corpora, setCorpora] = useState<api.GroundingCorpusSummary[]>([]);

  useEffect(() => {
    api.listGroundingCorpora().then(setCorpora).catch(() => setCorpora([]));
  }, []);

  const canBuildFromRepo = target === "existing" && Boolean(repoPath.trim());

  // F105: native directory picker. In Tauri this opens the OS folder dialog (the
  // Windows Explorer picker on Windows); in browser-dev it returns [] (no path
  // access) and we surface an inline "paste an absolute path" note instead of
  // injecting a useless file name.
  const browseDirectory = useCallback(
    async (apply: (path: string) => void) => {
      setPickerNote(null);
      try {
        const picked = await pickPaths({
          directory: true,
          multiple: false,
          requireAbsolutePath: true,
        });
        if (picked.length > 0 && picked[0]) {
          apply(picked[0]);
        } else {
          setPickerNote(
            "Folder picker is unavailable here — paste an absolute path instead.",
          );
        }
      } catch {
        setPickerNote(
          "Folder picker is unavailable here — paste an absolute path instead.",
        );
      }
    },
    [],
  );

  const trimmedId = id.trim();
  const idError = useMemo(() => validateProjectId(id), [id]);
  const canCreate = trimmedId !== "" && idError === null;
  const trimmedRoot = deliveryRoot.trim();
  const deliveryHelper =
    trimmedRoot === ""
      ? `Default: ~/Errorta Projects/${trimmedId || "<project_id>"}`
      : `Will deliver to ${trimmedRoot}/${trimmedId || "<project_id>"}`;

  return (
    <form
      className="coding-create"
      aria-label="Create project"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!canCreate) return;
        try {
          const selectedCorpus = corpusMode === "existing" ? corpusId.trim() : "";
          await api.createProject({
            projectId: id.trim(),
            northStar: northStar.trim(),
            target,
            repoPath: repoPath.trim() || null,
            // F105: blank delivery root submits null (default); only meaningful
            // for greenfield ("new") targets.
            deliveryRoot: target === "new" ? deliveryRoot.trim() || null : null,
            grounding:
              corpusMode === "none"
                ? { mode: "none" }
                : corpusMode === "existing"
                  ? { mode: "existing", corpusId: selectedCorpus || null }
                  : {
                      mode: "build_from_repo",
                      corpusId: corpusId.trim() || `${id.trim()}-project`,
                      sourceRoot: repoPath.trim() || null,
                    },
          });
          onCreated(id.trim());
        } catch (err) {
          onError(err instanceof Error ? err.message : String(err));
        }
      }}
    >
      <div className="coding-create-grid">
        <div className="coding-field">
          <label className="coding-field-label" htmlFor="coding-create-id">
            Project ID
            <span className="coding-field-hint">{PROJECT_ID_HINT}</span>
          </label>
          <input
            id="coding-create-id"
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="my-project"
            aria-label="Project id"
            aria-invalid={idError !== null}
            aria-describedby={idError !== null ? "coding-create-id-error" : undefined}
          />
          {idError !== null ? (
            <p id="coding-create-id-error" className="coding-field-error" role="alert">
              {idError}
            </p>
          ) : null}
        </div>

        <div className="coding-field">
          <label className="coding-field-label" htmlFor="coding-create-target">
            Project type
          </label>
          <select
            id="coding-create-target"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            aria-label="Target"
          >
            <option value="new">New project</option>
            <option value="existing">Existing repo</option>
          </select>
        </div>

        <div className="coding-field coding-field-wide">
          <label className="coding-field-label" htmlFor="coding-create-northstar">
            North Star
            <span className="coding-field-hint">The goal the team builds toward</span>
          </label>
          <textarea
            id="coding-create-northstar"
            value={northStar}
            onChange={(e) => setNorthStar(e.target.value)}
            placeholder="Describe what you want built — the more specific, the better."
            rows={3}
            aria-label="North Star"
          />
        </div>

        <div className="coding-field coding-field-wide">
          <label className="coding-field-label" htmlFor="coding-create-location">
            {target === "existing" ? "Repo path" : "Project location"}
            {target === "existing" ? null : (
              <span className="coding-field-hint">Optional</span>
            )}
          </label>
          <div className="coding-location-row">
            {target === "existing" ? (
              <input
                id="coding-create-location"
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
                placeholder="/path/to/repo"
                aria-label="Repo path"
              />
            ) : (
              <input
                id="coding-create-location"
                value={deliveryRoot}
                onChange={(e) => setDeliveryRoot(e.target.value)}
                placeholder="Project location (optional)"
                aria-label="Project location"
              />
            )}
            <button
              type="button"
              className="coding-btn coding-btn-ghost"
              onClick={() =>
                void browseDirectory(target === "existing" ? setRepoPath : setDeliveryRoot)
              }
              aria-label={target === "existing" ? "Browse for repo path" : "Browse for project location"}
            >
              Browse…
            </button>
          </div>
          {target === "existing" ? null : (
            <p className="coding-location-help">{deliveryHelper}</p>
          )}
          {pickerNote ? (
            <p className="coding-location-note" role="status">
              {pickerNote}
            </p>
          ) : null}
        </div>

        <div className="coding-field coding-field-wide">
          <label className="coding-field-label" htmlFor="coding-create-corpus">
            Project corpus
            <span className="coding-field-hint">Ground the team in existing knowledge</span>
          </label>
          <div className="coding-corpus-row">
            <select
              id="coding-create-corpus"
              value={corpusMode}
              onChange={(e) =>
                setCorpusMode(e.target.value as "none" | "existing" | "build_from_repo")
              }
              aria-label="Project corpus mode"
            >
              <option value="none">No project corpus</option>
              <option value="existing">Use existing corpus</option>
              <option value="build_from_repo" disabled={!canBuildFromRepo}>
                Build corpus from repo
              </option>
            </select>
            {corpusMode === "existing" ? (
              <select
                value={corpusId}
                onChange={(e) => setCorpusId(e.target.value)}
                aria-label="Existing corpus"
              >
                <option value="">Select corpus</option>
                {corpora.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name} ({c.readyCount}/{c.fileCount} ready)
                  </option>
                ))}
              </select>
            ) : null}
            {corpusMode === "build_from_repo" ? (
              <input
                value={corpusId}
                onChange={(e) => setCorpusId(e.target.value)}
                placeholder={`${id.trim() || "project"}-project`}
                aria-label="New corpus id"
              />
            ) : null}
          </div>
        </div>
      </div>

      <div className="coding-create-footer">
        <button type="submit" className="coding-btn" disabled={!canCreate}>
          Create project
        </button>
      </div>
    </form>
  );
}

function CodingProjectContainer({
  projectId,
  openSetupOnLoad,
  onInitialSetupOpened,
  onBack,
}: {
  projectId: string;
  openSetupOnLoad: boolean;
  onInitialSetupOpened: () => void;
  onBack: () => void;
}) {
  const [project, setProject] = useState<api.CodingProject | null>(null);
  const [tasks, setTasks] = useState<api.CodingTask[]>([]);
  const [decisions, setDecisions] = useState<api.CodingDecision[]>([]);
  const [artifacts, setArtifacts] = useState<api.CodingArtifact[]>([]);
  const [toolEvents, setToolEvents] = useState<api.CodingToolEvent[]>([]);
  const [testCommands, setTestCommands] = useState<Record<string, api.TestCommand>>({});
  const [testRuns, setTestRuns] = useState<api.TestRun[]>([]);
  const [requireSandbox, setRequireSandbox] = useState(false);
  const [turns, setTurns] = useState<api.CodingTurn[]>([]);
  const [prs, setPrs] = useState<api.CodingPr[]>([]);
  const [governance, setGovernance] = useState<api.GovernanceSummary | null>(null);
  // F100-01: the plain-language governance status projection for the strip.
  const [govStatus, setGovStatus] = useState<api.GovernanceStatus | null>(null);
  const govPanelRef = useRef<HTMLDivElement | null>(null);
  // F100-02: brainstorm viewer open state + whether to focus the comment box.
  const [brainstormOpen, setBrainstormOpen] = useState(false);
  const [brainstormCommenting, setBrainstormCommenting] = useState(false);
  const [brainstormStage, setBrainstormStage] =
    useState<api.GovernanceStage>("brainstorm");
  const [guardrail, setGuardrail] = useState(true);
  const [autonomy, setAutonomy] = useState<api.AutonomyPolicy>({
    maxIterations: 200,
    maxModelCalls: null,
    checkpointCadence: "per_milestone",
    checkpointN: 5,
  });
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState<api.RunStatus | null>(null);
  // F121 Part A: optimistic run-control intent set the instant the user clicks
  // Start/Stop, plus the bounded "starting" timeout fallback. `runIntent` is
  // cleared by the poll once the backend confirms the new state.
  const [runIntent, setRunIntent] = useState<RunIntent>("none");
  const [startTimedOut, setStartTimedOut] = useState(false);
  // F121 Part A: the prior run's `started_at` captured the instant Start is
  // clicked, so a stale terminal status (from the previous run, still reported
  // while the new start is in preflight) can't suppress the optimistic
  // "Starting…" — see deriveRunPhase.
  const [startBaseline, setStartBaseline] = useState<string | null>(null);
  const startTimer = useRef<number | null>(null);
  // F121 Part B: the readiness gate. `gateOpen` toggles the modal; `gateSeed`
  // is the pre-fill config (project live config + sticky defaults seed). The
  // gate is opened on a first unconfirmed Start, or via the re-openable
  // "Run setup" affordance.
  const [gateOpen, setGateOpen] = useState(false);
  const [gateSeed, setGateSeed] = useState<api.RunSetupConfig | null>(null);
  const [northStarOpen, setNorthStarOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preflightUnhealthy, setPreflightUnhealthy] = useState<PreflightUnhealthy[]>([]);
  const [rooms, setRooms] = useState<CouncilRoomSummary[]>([]);
  const [teamRoom, setTeamRoom] = useState<string>("");
  const [teamMembers, setTeamMembers] = useState<CodingTeamMember[]>([]);
  const [mergePreview, setMergePreview] = useState<api.WorktreePreview | null>(null);
  const [delivered, setDelivered] = useState<api.Delivery | null>(null);
  const mergePanelRef = useRef<HTMLDivElement | null>(null);
  const mergeOpenerRef = useRef<HTMLElement | null>(null);
  const northStarButtonRef = useRef<HTMLButtonElement | null>(null);
  const memberNameById = useMemo(() => {
    const out: Record<string, string> = {};
    for (const member of teamMembers) {
      if (member.id) out[member.id] = member.name || member.id;
    }
    return out;
  }, [teamMembers]);

  // F087-14 WS-6: merge dialog focus management — focus the panel on open,
  // restore focus to the opener on close (reuses the app's modal pattern).
  const mergeOpen = mergePreview !== null;
  useEffect(() => {
    if (mergeOpen) {
      mergePanelRef.current?.focus();
    } else {
      mergeOpenerRef.current?.focus();
      mergeOpenerRef.current = null;
    }
  }, [mergeOpen]);

  const onMergeKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      setMergePreview(null);
      return;
    }
    if (e.key !== "Tab") return;
    const panel = mergePanelRef.current;
    if (!panel) return;
    const focusable = panel.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === panel)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };
  const timer = useRef<number | null>(null);

  useEffect(() => {
    listRooms()
      .then((rs) => {
        setRooms(rs);
        if (rs.length && !teamRoom) setTeamRoom(rs[0].id);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!teamRoom) {
      setTeamMembers([]);
      return;
    }
    let cancelled = false;
    getRoomFull(teamRoom)
      .then(({ room }) => {
        if (!cancelled) setTeamMembers(teamMembersFromRoom(room));
      })
      .catch(() => {
        if (!cancelled) setTeamMembers([]);
      });
    return () => {
      cancelled = true;
    };
  }, [teamRoom]);

  const load = useCallback(async () => {
    try {
      const [p, b, d, a, te, tc, tr, ts, tn, pr, gvFull, g, au, rs] = await Promise.all([
        api.getProject(projectId),
        api.getBacklog(projectId),
        api.getDecisions(projectId),
        api.getArtifacts(projectId),
        api.getToolEvents(projectId),
        api.getTestCommands(projectId),
        api.getTestRuns(projectId),
        api.getTestSettings(projectId),
        api.getTurns(projectId),
        api.getPrs(projectId),
        api.getGovernanceFull(projectId),
        api.getGuardrail(projectId),
        api.getAutonomy(projectId),
        api.getRunStatus(projectId),
      ]);
      setProject(p);
      setTasks(b);
      setDecisions(d);
      setArtifacts(a);
      setToolEvents(te);
      setTestCommands(tc);
      setTestRuns(tr);
      setRequireSandbox(ts.requireSandbox);
      setTurns(tn);
      setPrs(pr);
      setGovernance(gvFull.summary);
      setGovStatus(gvFull.status);
      setGuardrail(g);
      setAutonomy(au);
      setRunning(rs.running);
      setRunStatus(rs);
      // F121 Part A: a confirming poll clears the optimistic intent + start
      // timeout. "starting" clears once the run is actually running; "stopping"
      // clears once it leaves running (so the terminal stop_reason shows).
      setRunIntent((intent) => {
        if (intent === "starting" && rs.running) {
          if (startTimer.current) {
            window.clearTimeout(startTimer.current);
            startTimer.current = null;
          }
          setStartTimedOut(false);
          return "none";
        }
        if (intent === "stopping" && !rs.running) return "none";
        return intent;
      });
      setError(null); // a healthy poll clears any prior banner (recovered backend)
    } catch (e) {
      // The sidecar binds an ephemeral port at launch and can briefly drop on a
      // respawn; that surfaces as a transport failure. It is ALWAYS transient —
      // the 2.5s poll re-resolves and heals it — so never latch the
      // "sidecar unreachable" banner for it. Real API errors still show.
      if (e instanceof SidecarUnreachableError) return;
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void load();
    timer.current = window.setInterval(() => void load(), 2500);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
      // F121 Part A: don't leak the "starting" timeout across unmounts.
      if (startTimer.current) window.clearTimeout(startTimer.current);
    };
  }, [load]);

  if (!project) {
    return (
      <div className="coding-shell">
        <button type="button" className="coding-btn" onClick={onBack}>
          ← Back
        </button>
        {error ? <p className="coding-error" role="alert">{error}</p> : <p>Loading…</p>}
      </div>
    );
  }

  const wrap = (fn: () => Promise<unknown>) => async () => {
    try {
      await fn();
      setPreflightUnhealthy([]);
      await load();
    } catch (e) {
      if (e instanceof api.RunPreflightBlocked) {
        setPreflightUnhealthy(e.unhealthy);
        setError(null);
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // F121 Part B: open the readiness gate, seeding its pre-fill from the project's
  // live config plus the user-level sticky defaults. A never-confirmed project
  // seeds from the sticky defaults (or the built-in Careful preset on a first-ever
  // project); a re-open shows the live config. Stale/unknown keys in the saved
  // defaults are dropped server-side, so the seed is always a valid pre-fill.
  const openRunSetup = async () => {
    try {
      const s = await api.getRunSetup(projectId);
      const gov = s.governance as Record<string, unknown>;
      const au = s.autonomy as Record<string, unknown>;
      const live: api.RunSetupConfig = {
        governanceMode:
          typeof gov.mode === "string" ? (gov.mode as api.GovernanceMode) : undefined,
        blockOnProblems:
          typeof gov.block_on_problems === "boolean" ? gov.block_on_problems : undefined,
        humanCodeApproval:
          typeof gov.human_code_approval === "string"
            ? (gov.human_code_approval as api.HumanCodeApproval)
            : undefined,
        maxReviewRounds:
          typeof gov.max_review_rounds === "number" ? gov.max_review_rounds : undefined,
        checkpointCadence:
          typeof au.checkpoint_cadence === "string"
            ? (au.checkpoint_cadence as api.CheckpointCadence)
            : undefined,
        checkpointN: typeof au.checkpoint_n === "number" ? au.checkpoint_n : undefined,
        guardrailEnabled: s.guardrailEnabled,
        maxIterations: typeof au.max_iterations === "number" ? au.max_iterations : undefined,
        maxModelCalls:
          au.max_model_calls === null
            ? null
            : typeof au.max_model_calls === "number"
              ? au.max_model_calls
              : undefined,
        maxParallelWorkers:
          au.max_parallel_workers === null
            ? null
            : typeof au.max_parallel_workers === "number"
              ? au.max_parallel_workers
              : undefined,
        memberFailureLimit:
          typeof au.member_failure_limit === "number" ? au.member_failure_limit : undefined,
        preflightEnabled: s.memberHealthPreflight,
      };
      // First-ever project (no sticky defaults, never confirmed) -> Careful.
      const seed: api.RunSetupConfig = s.runSetupConfirmed
        ? live
        : Object.keys(s.defaults).length
          ? { ...live, ...s.defaults }
          : { ...live, ...CAREFUL_PRESET };
      if (s.defaults.teamRoomId && !teamRoom) setTeamRoom(s.defaults.teamRoomId);
      setGateSeed(seed);
      setGateOpen(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // F121 Part A: optimistic Start. Flip to "Starting workers…" within one frame,
  // arm a bounded timeout so we never hang on "Starting…", then call startRun.
  // A preflight refusal or any start error clears the intent immediately (the
  // existing `wrap` surfaces the banner). The confirming poll clears it on
  // success.
  //
  // F121 Part B: the FIRST start on an unconfirmed project opens the readiness
  // gate instead of starting — no run thread. The gate's "Ready to run" confirms
  // setup then calls this same path (now confirmed -> it proceeds).
  const beginStartRun = (options: { skipSetupGate?: boolean } = {}): boolean => {
    if (!options.skipSetupGate && project && project.runSetupConfirmed === false) {
      void openRunSetup();
      return false;
    }
    if (!teamRoom) {
      setError("Pick a Council room as the team first (create one in the Council tab).");
      return false;
    }
    setStartTimedOut(false);
    // Capture the run identity NOW so deriveRunPhase can tell this prior-run
    // terminal state apart from the new run once it actually starts.
    setStartBaseline(
      (typeof runStatus?.state?.["started_at"] === "string"
        ? (runStatus.state["started_at"] as string)
        : null) || null,
    );
    setRunIntent("starting");
    if (startTimer.current) window.clearTimeout(startTimer.current);
    startTimer.current = window.setTimeout(() => {
      // Timed out with no `running`: resolve away from "Starting…" and surface
      // an honest error instead of an infinite spinner.
      setStartTimedOut(true);
      setRunIntent("none");
      setError(
        "The run didn't start. The team may have failed to spin up — check provider settings and try again.",
      );
    }, START_TIMEOUT_MS);
    void (async () => {
      try {
        await api.startRun(projectId, undefined, teamRoom);
        setPreflightUnhealthy([]);
        await load();
      } catch (e) {
        if (startTimer.current) {
          window.clearTimeout(startTimer.current);
          startTimer.current = null;
        }
        setRunIntent("none");
        setStartTimedOut(false);
        if (e instanceof api.RunPreflightBlocked) {
          setPreflightUnhealthy(e.unhealthy);
          setError(null);
          return;
        }
        if (e instanceof api.RunSetupRequired) {
          // A stale client raced the gate — open it instead of erroring.
          void openRunSetup();
          return;
        }
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return true;
  };

  // F121 Part A: optimistic Resume — same immediate "Starting…" feedback as
  // Start (resume also spins workers up + runs integrity checks, which take a
  // few seconds). Capture the baseline so the interrupted run's terminal state
  // can't suppress the optimistic phase. resume recovers the saved team
  // server-side, so no setup gate / room selection.
  const beginResumeRun = (): boolean => {
    setStartTimedOut(false);
    setStartBaseline(
      (typeof runStatus?.state?.["started_at"] === "string"
        ? (runStatus.state["started_at"] as string)
        : null) || null,
    );
    setRunIntent("starting");
    if (startTimer.current) window.clearTimeout(startTimer.current);
    startTimer.current = window.setTimeout(() => {
      setStartTimedOut(true);
      setRunIntent("none");
      setError(
        "The run didn't resume. The team may have failed to spin up — check provider settings and try again.",
      );
    }, START_TIMEOUT_MS);
    void (async () => {
      try {
        await api.resumeRun(projectId, undefined, teamRoom || undefined);
        setPreflightUnhealthy([]);
        await load();
      } catch (e) {
        if (startTimer.current) {
          window.clearTimeout(startTimer.current);
          startTimer.current = null;
        }
        setRunIntent("none");
        setStartTimedOut(false);
        if (e instanceof api.RunPreflightBlocked) {
          setPreflightUnhealthy(e.unhealthy);
          setError(null);
          return;
        }
        if (e instanceof api.RunSetupRequired) {
          void openRunSetup();
          return;
        }
        if (e instanceof api.RunWorkspaceIntegrityError) {
          // The interrupted worktree no longer matches its fingerprint, so resume
          // refuses. A fresh start is the correct recovery: it reuses the existing
          // repo (your merged work on master is intact) and re-queues unfinished
          // tasks. Fall back to it transparently instead of dead-ending on the
          // only "Resume" affordance.
          setError(
            "Couldn't resume the previous workspace — starting a fresh run that builds on your saved work and re-queues unfinished tasks.",
          );
          beginStartRun({ skipSetupGate: true });
          return;
        }
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return true;
  };

  // F121 Part A: optimistic Stop. Flip to "Stopping…" within one frame; the
  // backend cancel_requested flag keeps it sticky across reloads/polls.
  const beginCancelRun = (): boolean => {
    setRunIntent("stopping");
    void wrap(() => api.cancelRun(projectId))();
    return true;
  };

  const openProviderSettings = () => {
    window.dispatchEvent(
      new CustomEvent(APP_NAVIGATION_EVENT, { detail: { view: "settings" } }),
    );
  };

  const openRoomSettings = () => {
    window.dispatchEvent(
      new CustomEvent(APP_NAVIGATION_EVENT, { detail: { view: "rooms" } }),
    );
  };

  const openLocalPath = async (path: string) => {
    if (!path.trim()) return;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("open_path", { path });
      setError(null);
    } catch {
      // Browser-dev fallback: surface the path instead of pretending it opened.
      setError(`Open this path: ${path}`);
    }
  };

  const closeNorthStar = () => {
    setNorthStarOpen(false);
    window.setTimeout(() => northStarButtonRef.current?.focus(), 0);
  };

  const saveNorthStar = async (nextNorthStar: string) => {
    await api.putNorthStar(projectId, nextNorthStar);
    setProject((current) =>
      current ? { ...current, northStar: nextNorthStar } : current,
    );
    await load();
  };

  const location = projectLocation(project, delivered);

  return (
    <div className="coding-shell">
      <AutoOpenRunSetup
        enabled={openSetupOnLoad}
        confirmed={project.runSetupConfirmed ?? false}
        onOpen={openRunSetup}
        onConsumed={onInitialSetupOpened}
      />
      <CodingProjectHeader
        project={project}
        northStarButtonRef={northStarButtonRef}
        onBack={onBack}
        onOpenNorthStar={() => setNorthStarOpen(true)}
      />
      {error ? <p className="coding-error" role="alert">{error}</p> : null}
      <PmControlPanel projectId={projectId} />
      <details className="coding-panel coding-team-pick" aria-label="Team">
        <summary>
          <span>Team</span>
          <span className="coding-count">{teamMembers.length}</span>
        </summary>
        <section aria-label="Team members and room">
          <div className="coding-team-select-row">
            <label htmlFor="coding-team-room">Team:</label>
            <select
              id="coding-team-room"
              value={teamRoom}
              onChange={(e) => setTeamRoom(e.target.value)}
              disabled={running}
            >
              {rooms.length === 0 ? <option value="">No rooms — create one in Council</option> : null}
              {rooms.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name || r.id}
                </option>
              ))}
            </select>
          </div>
          {teamMembers.length > 0 ? (
            <ul className="coding-team-members" aria-label="Team members">
              {teamMembers.map((member, idx) => (
                <li key={`${member.role}-${member.name}-${idx}`}>
                  <span className="coding-team-role">{member.role}</span>
                  <span>{member.name}</span>
                  <span className="coding-team-model" title={member.modelTitle}>
                    {member.model}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      </details>
      <GovernanceStatusPanel
        status={govStatus}
        onOpenStage={(stage) => {
          setBrainstormStage(stage);
          setBrainstormCommenting(false);
          setBrainstormOpen(true);
        }}
        onOpenBrainstorm={() => {
          setBrainstormStage(govStatus?.stage ?? "brainstorm");
          setBrainstormCommenting(false);
          setBrainstormOpen(true);
        }}
        onCommentBrainstorm={() => {
          setBrainstormStage(govStatus?.stage ?? "brainstorm");
          setBrainstormCommenting(true);
          setBrainstormOpen(true);
        }}
      />
      <PreflightBlockedBanner
        unhealthy={preflightUnhealthy}
        onOpenProviderSettings={openProviderSettings}
        onDismiss={() => setPreflightUnhealthy([])}
      />
      <AttentionFeed
        projectId={projectId}
        onChange={() => void load()}
        onOpenProviderSettings={() => openProviderSettings()}
        onOpenRoomSettings={() => openRoomSettings()}
      />
      {brainstormOpen ? (
        <BrainstormViewer
          projectId={projectId}
          summary={governance}
          // Open either the stage the user clicked in Building Progress, or the
          // live stuck stage used by the existing stuck actions.
          stage={brainstormStage}
          running={running}
          startCommenting={brainstormCommenting}
          onClose={() => setBrainstormOpen(false)}
          onChanged={() => void load()}
        />
      ) : null}
      <CodingProjectView
        project={project}
        tasks={tasks}
        decisions={decisions}
        artifacts={artifacts}
        toolEvents={toolEvents}
        turns={turns}
        prs={prs}
        governance={governance}
        memberNameById={memberNameById}
        governanceSlot={
          <div ref={govPanelRef} tabIndex={-1} className="coding-gov-focus-target">
            <GovernancePanel
              projectId={projectId}
              governance={governance}
              running={running}
              guardrailEnabled={guardrail}
              autonomy={autonomy}
              onToggleGuardrail={(en) => void wrap(() => api.putGuardrail(projectId, en))()}
              onChangeCadence={(c) =>
                void wrap(() => api.putAutonomy(projectId, { checkpointCadence: c }))()
              }
              onChanged={() => void load()}
              onError={setError}
            />
          </div>
        }
        runtimeSlot={
          <RunPreviewPanel
            projectId={projectId}
            testRuns={testRuns}
            onAskFixRuntime={async ({ profile, session }) => {
              const profileId = profile?.profileId ?? "default";
              // S5: the backend composes a context-rich repair task (profile
              // commands + session outcome + redacted log tail).
              await api.requestRuntimeRepair(
                projectId,
                profileId,
                session?.sessionId ?? null,
              );
              await load();
            }}
          />
        }
        groundingSlot={
          <GroundingPanel
            projectId={projectId}
            repoPath={project.repoPath}
            running={running}
            onChanged={() => void load()}
          />
        }
        publishSlot={<PublishPanel projectId={projectId} delivered={Boolean(project.delivered)} />}
        teamLogSlot={<TeamLog projectId={projectId} />}
        tokenUsageSlot={<TokenUsagePanel projectId={projectId} />}
        onboardingSlot={
          <OnboardingPanel
            project={project}
            running={running}
            onChanged={() => void load()}
            onError={setError}
          />
        }
        onDownloadRunLog={() =>
          void wrap(async () => {
            const text = await api.fetchRunLog(projectId);
            const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
            const a = document.createElement("a");
            a.href = url;
            a.download = `coding-run-${projectId}.txt`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
          })()
        }
        testCommands={testCommands}
        testRuns={testRuns}
        requireSandbox={requireSandbox}
        onSaveTestCommands={(commands) =>
          void wrap(async () => {
            await api.putTestCommands(projectId, commands);
            await load();
          })()
        }
        onToggleRequireSandbox={(value) =>
          void wrap(async () => {
            await api.putTestSettings(projectId, value);
            await load();
          })()
        }
        running={running}
        runStatus={runStatus}
        runPhase={deriveRunPhase({ intent: runIntent, running, runStatus, startTimedOut, startBaseline })}
        workingHeadline={govStatus?.headline || ""}
        onFileSaved={() => void load()}
        onAddTask={(title, role) => void wrap(() => api.addTask(projectId, title, role))()}
        onInterject={async (msg) => {
          try {
            const interjection = await api.interject(projectId, msg);
            await load();
            return interjection;
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
            throw e;
          }
        }}
        onStartRun={beginStartRun}
        onResumeRun={beginResumeRun}
        onCancelRun={beginCancelRun}
        delivery={delivered}
        onOpenProjectPath={(path) => void openLocalPath(path)}
        onOpenRunTarget={(path) => void openLocalPath(path)}
        onReviewMergeBack={() =>
          void (async () => {
            mergeOpenerRef.current = document.activeElement as HTMLElement | null;
            try {
              setMergePreview(await api.getWorktreePreview(projectId));
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          })()
        }
      />
      <section className="coding-project-settings-footer" aria-label="Project settings">
        <div>
          <h3>Project settings</h3>
          <p>Change run setup when the team, governance, cadence, or limits need to shift.</p>
          {location ? (
            <p className="coding-project-settings-location">
              <span>{location.label}:</span> <code>{location.value}</code>
            </p>
          ) : null}
        </div>
        <button type="button" className="coding-btn" onClick={() => void openRunSetup()}>
          Run setup
        </button>
      </section>
      {northStarOpen ? (
        <NorthStarDialog
          projectTitle={projectDisplayName(project)}
          northStar={project.northStar}
          onClose={closeNorthStar}
          onSave={saveNorthStar}
        />
      ) : null}
      {gateOpen && gateSeed ? (
        <RunSetupGate
          projectId={projectId}
          rooms={rooms}
          teamRoomId={teamRoom}
          onTeamRoomChange={setTeamRoom}
          initialConfig={gateSeed}
          groundingBound={Boolean(project.grounding && project.grounding.mode !== "none")}
          onClose={() => setGateOpen(false)}
          onConfirmed={() => {
            setGateOpen(false);
            void load();
          }}
        />
      ) : null}
      {delivered ? (
        <section className="coding-delivered" role="status" aria-label="Delivered">
          <h3>✓ Delivered — your project is ready to use</h3>
          <p className="coding-delivered-path">
            <code>{delivered.deliveredTo}</code>
          </p>
          {delivered.runHint ? (
            <p className="coding-delivered-run">
              Run it: <code>{delivered.runHint}</code>
            </p>
          ) : null}
          <div className="coding-delivered-actions">
            <button
              type="button"
              className="coding-btn coding-btn-accept"
              onClick={() => void openLocalPath(delivered.deliveredTo)}
            >
              Open folder
            </button>
            <button
              type="button"
              className="coding-btn"
              onClick={() => void navigator.clipboard?.writeText(delivered.deliveredTo)}
            >
              Copy path
            </button>
            <button type="button" className="coding-btn" onClick={() => setDelivered(null)}>
              Dismiss
            </button>
          </div>
        </section>
      ) : null}
      {mergePreview !== null ? (
        <div
          className="coding-merge-modal"
          role="dialog"
          aria-modal="true"
          aria-label="Review diff"
          onKeyDown={onMergeKeyDown}
        >
          <div className="coding-merge-panel" ref={mergePanelRef} tabIndex={-1}>
            <h3>Review the diff before accepting</h3>
            {project.target === "existing" ? (
              <p className="coding-error">
                ⚠ Accepting writes this diff into your real repo
                {project.repoPath ? ` at ${project.repoPath}` : ""}.
              </p>
            ) : null}

            {mergePreview.gate.blockers.length > 0 ? (
              <div className="coding-gate-blockers" role="alert">
                {mergePreview.gate.blockers.some((b) =>
                  HEAD_BINDING_BLOCKERS.has(b.code),
                ) ? (
                  <p className="coding-gate-explainer">
                    These changes were reviewed and merged individually during the
                    run. This combined, delivered diff hasn&apos;t been re-reviewed
                    as a whole yet — review it here and accept, or override.
                  </p>
                ) : null}
                <strong>Not ready to merge:</strong>
                <ul>
                  {mergePreview.gate.blockers.map((b) => (
                    <li key={b.code}>
                      <code>{b.code}</code> — {b.detail}
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="coding-gate-ok">All checks passed — ready to merge.</p>
            )}

            <ul className="coding-filediffs" aria-label="Changed files">
              {mergePreview.fileDiffs.length === 0 ? (
                <li className="coding-empty">No file changes.</li>
              ) : (
                mergePreview.fileDiffs.map((f) => (
                  <li key={f.path} className={`coding-filediff coding-change-${f.changeType}`}>
                    <span className="coding-change-type">{f.changeType}</span>
                    <code>{f.path}</code>
                    <span className="coding-diff-stat coding-diff-add">+{f.addedLines}</span>
                    <span className="coding-diff-stat coding-diff-del">-{f.removedLines}</span>
                  </li>
                ))
              )}
            </ul>
            <details className="coding-rawdiff">
              <summary>Raw diff</summary>
              <pre className="coding-diff">{mergePreview.diff || "(no changes)"}</pre>
            </details>

            <div className="coding-merge-actions">
              {mergePreview.gate.allowed ? (
                <button
                  type="button"
                  className="coding-btn coding-btn-accept"
                  onClick={() =>
                    void (async () => {
                      try {
                        const d = await api.acceptWorktree(projectId);
                        setMergePreview(null);
                        setDelivered(d);
                        await load();
                      } catch (e) {
                        setError(e instanceof Error ? e.message : String(e));
                      }
                    })()
                  }
                >
                  Accept &amp; deliver
                </button>
              ) : (
                <button
                  type="button"
                  className="coding-btn coding-btn-override"
                  onClick={() =>
                    void (async () => {
                      if (
                        !window.confirm(
                          "Override the merge gate and write incomplete/unreviewed/untested work to the repo?",
                        )
                      )
                        return;
                      try {
                        const d = await api.acceptWorktree(projectId, { override: true });
                        setMergePreview(null);
                        setDelivered(d);
                        await load();
                      } catch (e) {
                        setError(e instanceof Error ? e.message : String(e));
                      }
                    })()
                  }
                >
                  Override &amp; merge anyway
                </button>
              )}
              <button type="button" className="coding-btn" onClick={() => setMergePreview(null)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
