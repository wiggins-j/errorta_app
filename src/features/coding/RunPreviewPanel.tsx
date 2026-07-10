// F101 S2 — Run & Preview panel.
//
// Frontend-only control surface for the frozen runtime route contract. The
// backend owns sandboxing/processes/log redaction; this panel stays honest
// about what it has actually observed from profiles, sessions, and health.
import { useCallback, useEffect, useMemo, useState } from "react";

import * as api from "../../lib/api/coding";
import type {
  RuntimeHealthStatus,
  RuntimeProfile,
  RuntimeSession,
  RuntimeTestResult,
  TestRun,
} from "../../lib/api/coding";

const ACTIVE_STATES = new Set(["starting", "running", "healthy", "unhealthy"]);
const POLL_MS = 2000;

type PanelState =
  | "loading"
  | "unavailable"
  | "no_profile"
  | "setup_required"
  | "stopped"
  | "starting"
  | "running"
  | "healthy"
  | "unhealthy"
  | "crashed"
  | "stopped_error";

export interface RuntimeFixContext {
  profile: RuntimeProfile | null;
  session: RuntimeSession | null;
  logs: string[];
}

export interface RunPreviewPanelProps {
  projectId: string;
  testRuns?: TestRun[];
  onAskFixRuntime?: (context: RuntimeFixContext) => void | Promise<void>;
}

function quoteArg(arg: string): string {
  return /^[A-Za-z0-9_./:@%+=,-]+$/.test(arg) ? arg : JSON.stringify(arg);
}

function formatArgv(argv: string[]): string {
  return argv.length ? argv.map(quoteArg).join(" ") : "(no command)";
}

// F101-02: a CLI profile is one explicitly typed `cli`, or any managed_local
// profile that has no openable demo URL (a process that just runs and exits).
// The backend re-validates; this only chooses the panel flavor.
function isCliProfile(profile: RuntimeProfile): boolean {
  if (profile.kind === "cli") return true;
  return profile.runtimeMode === "managed_local" && !profile.demo?.url;
}

// F101-02: a display-only argv splitter for the effective-command preview. The
// backend parses extra-args authoritatively (shlex, no shell); this is just to
// show the user roughly what will run. It honors quotes/whitespace but never
// interprets shell metacharacters.
function previewSplit(text: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    out.push(m[1] ?? m[2] ?? m[3] ?? "");
  }
  return out;
}

function setupInstallsDependencies(profile: RuntimeProfile): boolean {
  return profile.setup.some((argv) => {
    const joined = argv.join(" ").toLowerCase();
    return (
      /\bnpm\s+(install|i|ci|add)\b/.test(joined) ||
      /\byarn\s+(install|add)\b/.test(joined) ||
      /\bpnpm\s+(install|i|add)\b/.test(joined) ||
      /\bpip\s+install\b/.test(joined) ||
      /\bpoetry\s+install\b/.test(joined) ||
      /\bbundle\s+install\b/.test(joined)
    );
  });
}

function firstPort(profile: RuntimeProfile, session: RuntimeSession | null): number | null {
  if (session?.allocatedPorts.length) return session.allocatedPorts[0];
  return profile.ports.find((p) => p.preferred != null)?.preferred ?? null;
}

function demoUrl(profile: RuntimeProfile, session: RuntimeSession | null): string | null {
  const raw = profile.demo?.url;
  if (!raw) return null;
  const port = firstPort(profile, session);
  return port == null ? raw : raw.replaceAll("{port}", String(port));
}

function activeSession(session: RuntimeSession | null): boolean {
  return Boolean(session && ACTIVE_STATES.has(session.state));
}

function stateLabel(state: PanelState): string {
  switch (state) {
    case "unavailable":
      return "Runtime unavailable";
    case "no_profile":
      return "No runnable demo detected";
    case "setup_required":
      return "Setup required";
    case "stopped":
      return "Stopped";
    case "starting":
      return "Starting";
    case "running":
      return "Running";
    case "healthy":
      return "Healthy";
    case "unhealthy":
      return "Unhealthy";
    case "crashed":
      return "Crashed";
    case "stopped_error":
      return "Stopped with error";
    default:
      return "Loading runtime";
  }
}

function derivePanelState(
  loading: boolean,
  runtimeAvailable: boolean,
  profile: RuntimeProfile | null,
  session: RuntimeSession | null,
  setupDone: boolean,
): PanelState {
  if (loading && !profile) return "loading";
  if (!runtimeAvailable) return "unavailable";
  if (!profile) return "no_profile";
  if (session) {
    if (session.state === "healthy") {
      return session.healthStatus?.ok === true ? "healthy" : "running";
    }
    if (session.state === "stopped" && (session.error || session.exitCode)) {
      return "stopped_error";
    }
    if (session.state === "starting") return "starting";
    if (session.state === "running") return "running";
    if (session.state === "unhealthy") return "unhealthy";
    if (session.state === "crashed") return "crashed";
    return "stopped";
  }
  if (profile.setup.length > 0 && !setupDone) return "setup_required";
  return "stopped";
}

function latestRuntimeEvidence(testRuns: TestRun[]): TestRun | null {
  const runtime = [...testRuns].reverse().find((run) =>
    run.commandIds.some((id) =>
      /runtime|health_check|demo_smoke/.test(id.toLowerCase()),
    ),
  );
  return runtime ?? (testRuns.length ? testRuns[testRuns.length - 1] : null);
}

export async function openExternalDemo(url: string): Promise<void> {
  try {
    const shell = await import("@tauri-apps/plugin-shell");
    await shell.open(url);
    return;
  } catch {
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    if (!opened) throw new Error(`Open this URL: ${url}`);
  }
}

function isNotFoundError(err: unknown): boolean {
  return err instanceof Error && /\(404\)/.test(err.message);
}

export default function RunPreviewPanel({
  projectId,
  testRuns = [],
  onAskFixRuntime,
}: RunPreviewPanelProps) {
  const [profiles, setProfiles] = useState<RuntimeProfile[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [session, setSession] = useState<RuntimeSession | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [logsTruncated, setLogsTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [runtimeAvailable, setRuntimeAvailable] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [setupDone, setSetupDone] = useState<Record<string, boolean>>({});
  const [profileJson, setProfileJson] = useState("");
  const [profileJsonError, setProfileJsonError] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [healthResult, setHealthResult] = useState<RuntimeHealthStatus | null>(null);
  const [testResult, setTestResult] = useState<RuntimeTestResult | null>(null);
  const [cliArgs, setCliArgs] = useState("");
  const [cliArgsError, setCliArgsError] = useState<string | null>(null);
  const [runResolution, setRunResolution] = useState<api.RuntimeRunResolution | null>(null);

  const selectedProfile = useMemo(
    () => profiles.find((p) => p.profileId === selectedId) ?? profiles[0] ?? null,
    [profiles, selectedId],
  );
  const selectedProfileId = selectedProfile?.profileId ?? "";
  const currentDemoUrl = selectedProfile ? demoUrl(selectedProfile, session) : null;
  const isCli = selectedProfile ? isCliProfile(selectedProfile) : false;
  const state = derivePanelState(
    loading,
    runtimeAvailable,
    selectedProfile,
    session,
    selectedProfile ? Boolean(setupDone[selectedProfile.profileId]) : false,
  );
  const isActive = activeSession(session);
  // F144: the summary address is clickable only when the run is actually live —
  // an active session has an allocated port for the URL. Without that explicit
  // allocation, demoUrl() falls back to the profile's preferred (intended) port,
  // which must remain inert before/after a real binding exists.
  const hasAllocatedPort = Boolean(session?.allocatedPorts.length);
  const liveDemoUrl = isActive && hasAllocatedPort ? currentDemoUrl : null;
  const latestEvidence = latestRuntimeEvidence(testRuns);

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const loaded = await api.listRuntimeProfiles(projectId);
      setRuntimeAvailable(true);
      setProfiles(loaded);
      setSelectedId((current) =>
        current && loaded.some((p) => p.profileId === current)
          ? current
          : loaded[0]?.profileId ?? "",
      );
    } catch (err) {
      if (isNotFoundError(err)) {
        setRuntimeAvailable(false);
        setProfiles([]);
        setSelectedId("");
        setSession(null);
        setError(null);
        setInfo(null);
        return;
      }
      setError(err instanceof Error ? err.message : "Could not load runtime profiles.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadProfiles();
  }, [loadProfiles]);

  useEffect(() => {
    setHealthResult(null);
    setTestResult(null);
    setCliArgs("");
    setCliArgsError(null);
  }, [selectedProfileId]);

  useEffect(() => {
    if (!selectedProfile) {
      setProfileJson("");
      return;
    }
    setProfileJson(JSON.stringify(api.runtimeProfileToWire(selectedProfile), null, 2));
    setProfileJsonError(null);
  }, [selectedProfile]);

  useEffect(() => {
    if (!session || !activeSession(session)) return;
    let cancelled = false;
    const id = window.setInterval(() => {
      void api
        .getRuntimeSession(projectId, session.sessionId)
        .then((next) => {
          if (!cancelled) setSession(next);
        })
        .catch((err: unknown) => {
          if (!cancelled) setError(err instanceof Error ? err.message : "Session poll failed.");
        });
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [projectId, session]);

  useEffect(() => {
    if (!session) {
      setLogs([]);
      setLogsTruncated(false);
      return;
    }
    let cancelled = false;
    const poll = () => {
      void api
        .getRuntimeSessionLogs(projectId, session.sessionId)
        .then((next) => {
          if (cancelled) return;
          setLogs(next.lines);
          setLogsTruncated(next.truncated);
        })
        .catch(() => {
          if (!cancelled) {
            setLogs([]);
            setLogsTruncated(false);
          }
        });
    };
    poll();
    const id = window.setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [projectId, session?.sessionId]);

  async function runAction(label: string, fn: () => Promise<void>) {
    setBusy(label);
    setError(null);
    setInfo(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const detectRuntime = () =>
    runAction("detect", async () => {
      const proposed = await api.detectRuntimeProfiles(projectId);
      setRuntimeAvailable(true);
      setProfiles(proposed);
      setSelectedId(proposed[0]?.profileId ?? "");
      setSession(null);
      setLogs([]);
      setLogsTruncated(false);
      setSetupDone({});
      setHealthResult(null);
      setTestResult(null);
      if (proposed.length === 0) {
        setInfo("No runnable demo detected.");
      } else {
        setInfo("Runtime profile detected. Review the profile before first run.");
        setEditorOpen(true);
      }
    });

  const saveProfile = () =>
    runAction("save-profile", async () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(profileJson);
      } catch (err) {
        setProfileJsonError(err instanceof Error ? err.message : "Invalid JSON.");
        return;
      }
      if (!parsed || typeof parsed !== "object") {
        setProfileJsonError("Profile JSON must be an object.");
        return;
      }
      const normalized = api.runtimeProfileFrom(parsed as Record<string, unknown>);
      const saved = await api.upsertRuntimeProfile(projectId, normalized);
      setProfiles((prev) => {
        const next = prev.filter((p) => p.profileId !== saved.profileId);
        return [...next, saved];
      });
      setSelectedId(saved.profileId);
      setEditorOpen(false);
      setProfileJsonError(null);
      setHealthResult(null);
      setTestResult(null);
      setInfo("Runtime profile saved.");
    });

  const runSetup = () => {
    if (!selectedProfile) return;
    const installing = setupInstallsDependencies(selectedProfile);
    if (installing) {
      const ok = window.confirm(
        `Run setup for ${selectedProfile.profileId}?\n\n` +
          selectedProfile.setup.map(formatArgv).join("\n") +
          "\n\nSetup may install dependencies and run package hooks.",
      );
      if (!ok) return;
    }
    void runAction("setup", async () => {
      const next = await api.setupRuntimeProfile(projectId, selectedProfile.profileId);
      setSession(next);
      setSetupDone((prev) => ({ ...prev, [selectedProfile.profileId]: true }));
    });
  };

  const startRuntime = () => {
    if (!selectedProfile) return;
    void runAction("start", async () => {
      setHealthResult(null);
      setTestResult(null);
      setSession(await api.startRuntimeProfile(projectId, selectedProfile.profileId));
    });
  };

  const runCli = () => {
    if (!selectedProfile) return;
    void runAction("run-cli", async () => {
      setCliArgsError(null);
      setHealthResult(null);
      setTestResult(null);
      try {
        const next = await api.runCliTranscript(projectId, selectedProfile.profileId, {
          extraArgs: cliArgs.trim() ? cliArgs : undefined,
        });
        setSession(next);
      } catch (err) {
        if (err instanceof api.RuntimeCliArgsError) {
          setCliArgsError(err.message);
          return;
        }
        throw err;
      }
    });
  };

  const stopRuntime = () => {
    if (!selectedProfile || !session) return;
    void runAction("stop", async () => {
      await api.stopRuntimeProfile(projectId, selectedProfile.profileId);
      setSession({ ...session, state: "stopped", endedAt: new Date().toISOString() });
    });
  };

  const restartRuntime = () => {
    if (!selectedProfile) return;
    void runAction("restart", async () => {
      if (session && activeSession(session)) {
        await api.stopRuntimeProfile(projectId, selectedProfile.profileId);
      }
      setHealthResult(null);
      setTestResult(null);
      setSession(await api.startRuntimeProfile(projectId, selectedProfile.profileId));
    });
  };

  const openDemo = () => {
    if (!currentDemoUrl) return;
    void runAction("open-demo", async () => {
      await openExternalDemo(currentDemoUrl);
    });
  };

  const runHealthCheck = () => {
    if (!selectedProfile) return;
    void runAction("health", async () => {
      const next = await api.runRuntimeHealthCheck(projectId, selectedProfile.profileId);
      setHealthResult(next);
      if (session) setSession({ ...session, healthStatus: next });
    });
  };

  const runTests = () => {
    if (!selectedProfile) return;
    void runAction("test", async () => {
      setTestResult(await api.runRuntimeTest(projectId, selectedProfile.profileId, "demo_smoke"));
    });
  };

  const askFixRuntime = () => {
    void runAction("ask-fix", async () => {
      if (onAskFixRuntime) {
        await onAskFixRuntime({ profile: selectedProfile, session, logs });
        setInfo("Runtime repair task added to the Coding Team backlog.");
      } else {
        setInfo("Runtime repair handoff is not wired for this project yet.");
      }
    });
  };

  // F101-03: the universal Run front door. Preview shows the grounded plan
  // (command, modality, host, tier, provenance) as the consent step; Confirm
  // executes it and adopts the returned session into the existing lifecycle.
  const previewRun = () =>
    runAction("run-preview", async () => {
      setRunResolution(await api.resolveRuntimeRun(projectId, { confirm: false }));
    });

  const doConfirmRun = (confirmReducedIsolation: boolean) =>
    runAction("run-confirm", async () => {
      const res = await api.resolveRuntimeRun(projectId, {
        confirm: true,
        confirmReducedIsolation,
      });
      if (res.session) {
        setRunResolution(null);
        setHealthResult(null);
        setTestResult(null);
        setSession(res.session);
        await loadProfiles();
        if (res.plan) setSelectedId(res.plan.profileId);
      } else {
        // Refused (T2 consent required) or the resolution changed between
        // preview and confirm — surface the latest outcome, never silently no-op.
        setRunResolution(res);
      }
    });

  const confirmRun = () => doConfirmRun(false);
  const confirmRunReduced = () => doConfirmRun(true);

  const dismissRun = () => setRunResolution(null);

  return (
    <details className="coding-panel coding-runtime">
      <summary>
        <span>Preview</span>
        <span className={`coding-runtime-state coding-runtime-state-${state}`}>
          {stateLabel(state)}
        </span>
      </summary>
      <section aria-label="Preview">
        {error ? <p className="coding-error" role="alert">{error}</p> : null}
        {info ? <p className="coding-runtime-info" role="status">{info}</p> : null}
        {session?.sandboxBackend === "none" ? (
          <p className="coding-runtime-warning" role="alert">
            Reduced isolation: this runtime session ran without an OS sandbox.
          </p>
        ) : null}
        {selectedProfile?.sandbox === "none" && session?.sandboxBackend !== "none" ? (
          <p className="coding-runtime-warning">
            This profile requests sandbox=none. Start results will show the actual
            backend used by the runner.
          </p>
        ) : null}

        <div className="coding-runtime-top">
          {profiles.length > 1 ? (
            <label>
              <span>Profile</span>
              <select
                value={selectedProfileId}
                onChange={(e) => {
                  setSelectedId(e.target.value);
                  setSession(null);
                }}
                aria-label="Runtime profile"
              >
                {profiles.map((profile) => (
                  <option key={profile.profileId} value={profile.profileId}>
                    {profile.profileId}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <button
            type="button"
            className="coding-btn coding-btn-small"
            onClick={detectRuntime}
            disabled={busy !== null || !runtimeAvailable}
          >
            Detect runtime
          </button>
          <button
            type="button"
            className="coding-btn coding-btn-small"
            onClick={() => setEditorOpen((v) => !v)}
            disabled={!selectedProfile}
          >
            Edit profile
          </button>
        </div>

        {runtimeAvailable ? (
          <div className="coding-runtime-run" aria-label="Run">
            <button
              type="button"
              className="coding-btn coding-btn-primary"
              onClick={previewRun}
              disabled={busy !== null}
            >
              Run
            </button>
            <p className="coding-file-note">
              Run resolves how to start this project and shows the command before
              anything executes. It never runs a command whose files aren’t there.
            </p>
            {runResolution
              ? runResolution.resolved && runResolution.plan
                ? (
                    <RunPlanPreview
                      plan={runResolution.plan}
                      runnable={runResolution.runnable}
                      reason={runResolution.reason}
                      requiresReducedConsent={runResolution.requiresReducedIsolationConsent}
                      busy={busy !== null}
                      onConfirm={confirmRun}
                      onConfirmReduced={confirmRunReduced}
                      onCancel={dismissRun}
                    />
                  )
                : (
                    <RunUnresolved
                      lookedFor={runResolution.lookedFor}
                      reason={runResolution.reason}
                      onDismiss={dismissRun}
                    />
                  )
              : null}
          </div>
        ) : null}

        {!runtimeAvailable ? (
          <div className="coding-runtime-empty">
            <h4>Runtime preview unavailable</h4>
            <p>
              This sidecar build does not expose runtime preview routes yet.
            </p>
          </div>
        ) : !selectedProfile ? (
          <div className="coding-runtime-empty">
            <h4>No runnable demo detected</h4>
            <p>
              Detect a runtime profile when the project has files, or ask the
              Coding Team to add one. This is not an error.
            </p>
          </div>
        ) : (
          <>
            <RuntimeSummary
              profile={selectedProfile}
              session={session}
              state={state}
              liveDemoUrl={liveDemoUrl}
              onOpenDemo={openDemo}
              openDemoDisabled={busy !== null}
            />
            <CommandPreview profile={selectedProfile} />
            {isCli ? (
              <div className="coding-runtime-cli-args" aria-label="CLI run arguments">
                <label>
                  <span>Extra arguments</span>
                  <input
                    type="text"
                    value={cliArgs}
                    onChange={(e) => {
                      setCliArgs(e.target.value);
                      setCliArgsError(null);
                    }}
                    placeholder="--name world"
                    spellCheck={false}
                    autoComplete="off"
                  />
                </label>
                <p className="coding-file-note">
                  Extra arguments are parsed like a shell command line but run
                  without a shell.
                </p>
                <div className="coding-runtime-cli-command" aria-label="Effective command">
                  <span>Will run</span>
                  <code>
                    {formatArgv([
                      ...selectedProfile.start,
                      ...previewSplit(cliArgs.trim()),
                    ])}
                  </code>
                </div>
                {cliArgsError ? (
                  <p className="coding-file-note coding-file-error" role="alert">
                    {cliArgsError}
                  </p>
                ) : null}
              </div>
            ) : null}
            <details className="coding-runtime-advanced">
              <summary>Advanced controls</summary>
              <div className="coding-runtime-actions" aria-label="Runtime actions">
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={runSetup}
                disabled={
                  busy !== null ||
                  selectedProfile.setup.length === 0 ||
                  isActive
                }
              >
                Run setup
              </button>
              {isCli ? (
                <button
                  type="button"
                  className="coding-btn coding-btn-small"
                  onClick={runCli}
                  disabled={busy !== null || isActive}
                >
                  Run (CLI)
                </button>
              ) : null}
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={startRuntime}
                disabled={
                  busy !== null ||
                  isActive ||
                  selectedProfile.runtimeMode === "static"
                }
              >
                Start
              </button>
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={stopRuntime}
                disabled={busy !== null || !isActive}
              >
                Stop
              </button>
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={restartRuntime}
                disabled={
                  busy !== null ||
                  selectedProfile.runtimeMode === "static"
                }
              >
                Restart
              </button>
              {isCli ? null : (
                <button
                  type="button"
                  className="coding-btn coding-btn-small"
                  onClick={openDemo}
                  disabled={busy !== null || !currentDemoUrl}
                >
                  Open demo
                </button>
              )}
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={runTests}
                disabled={busy !== null}
              >
                Run tests
              </button>
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={askFixRuntime}
                disabled={busy !== null}
              >
                Ask Coding Team to fix runtime
              </button>
              </div>
            </details>
          </>
        )}

        {editorOpen && selectedProfile ? (
          <div className="coding-runtime-editor" aria-label="Runtime profile JSON">
            <label>
              <span>Profile JSON</span>
              <textarea
                value={profileJson}
                onChange={(e) => setProfileJson(e.target.value)}
                rows={12}
                spellCheck={false}
              />
            </label>
            {profileJsonError ? (
              <p className="coding-file-note coding-file-error" role="alert">
                {profileJsonError}
              </p>
            ) : null}
            <div className="coding-runtime-actions">
              <button type="button" className="coding-btn coding-btn-small" onClick={saveProfile}>
                Save profile
              </button>
              <button
                type="button"
                className="coding-btn coding-btn-small"
                onClick={() => setEditorOpen(false)}
              >
                Close
              </button>
            </div>
          </div>
        ) : null}

        <div className="coding-runtime-grid">
          {isCli ? (
            <TranscriptBlock profile={selectedProfile} session={session} logs={logs} />
          ) : (
            <HealthBlock
              profile={selectedProfile}
              session={session}
              healthResult={healthResult}
              onCheck={runHealthCheck}
              disabled={!selectedProfile || busy !== null}
            />
          )}
          <EnvBlock profile={selectedProfile} />
          <EvidenceBlock evidence={latestEvidence} result={testResult} />
        </div>

        <details className="coding-runtime-logs" open={Boolean(session)}>
          <summary>
            Logs <span className="coding-count">{logs.length}</span>
          </summary>
          {session ? (
            logs.length ? (
              <>
                {logsTruncated ? (
                  <p className="coding-file-note">Log tail is capped and redacted.</p>
                ) : null}
                <pre aria-label="Runtime logs">{logs.join("\n")}</pre>
              </>
            ) : (
              <p className="coding-empty">No log lines yet.</p>
            )
          ) : (
            <p className="coding-empty">Logs appear after setup or start creates a session.</p>
          )}
        </details>
      </section>
    </details>
  );
}

function trustTierLabel(tier: number): string {
  switch (tier) {
    case 0:
      return "T0 — sandboxed (headless)";
    case 1:
      return "T1 — sandboxed (windowed)";
    case 2:
      return "T2 — reduced isolation (consent)";
    default:
      return `tier ${tier}`;
  }
}

function RunPlanPreview({
  plan,
  runnable,
  reason,
  requiresReducedConsent,
  busy,
  onConfirm,
  onConfirmReduced,
  onCancel,
}: {
  plan: api.RuntimeLaunchPlan;
  runnable: boolean;
  reason: string | null;
  requiresReducedConsent: boolean;
  busy: boolean;
  onConfirm: () => void;
  onConfirmReduced: () => void;
  onCancel: () => void;
}) {
  // T2 (reduced isolation): no OS sandbox can host the window; running needs a
  // second explicit consent. Distinct from a genuinely unavailable run type.
  const t2 = requiresReducedConsent || reason === "reduced_isolation_consent_required";
  const trulyUnavailable = !runnable && !t2;
  return (
    <div className="coding-runtime-plan" aria-label="Launch plan">
      <h4>Launch plan</h4>
      <dl>
        <div>
          <dt>Command</dt>
          <dd>
            <code>{formatArgv(plan.start)}</code>
          </dd>
        </div>
        <div>
          <dt>Modality</dt>
          <dd>{plan.launchKind || plan.modality}</dd>
        </div>
        <div>
          <dt>Host</dt>
          <dd>{plan.host || "local"}</dd>
        </div>
        <div>
          <dt>Trust tier</dt>
          <dd>{trustTierLabel(plan.trustTier)}</dd>
        </div>
        <div>
          <dt>Grounded by</dt>
          <dd>
            {plan.groundedBy}
            {plan.verifiedPaths.length ? ` (${plan.verifiedPaths.join(", ")})` : ""}
          </dd>
        </div>
      </dl>
      {plan.warnings.length ? (
        <ul className="coding-runtime-warnings">
          {plan.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}
      {trulyUnavailable ? (
        <p className="coding-runtime-warning" role="alert">
          This run type isn’t available on this host
          {reason ? ` (${reason})` : ""}. Use the Advanced controls.
        </p>
      ) : null}
      {t2 ? (
        <p className="coding-runtime-warning" role="alert">
          No OS sandbox can host this window here, so it would run with{" "}
          <strong>reduced isolation</strong> (no sandbox). This needs your explicit
          consent.
        </p>
      ) : null}
      <div className="coding-runtime-actions">
        {t2 ? (
          <button
            type="button"
            className="coding-btn coding-btn-primary"
            onClick={onConfirmReduced}
            disabled={busy}
          >
            Run with reduced isolation
          </button>
        ) : (
          <button
            type="button"
            className="coding-btn coding-btn-primary"
            onClick={onConfirm}
            disabled={busy || !runnable}
          >
            Confirm &amp; run
          </button>
        )}
        <button type="button" className="coding-btn coding-btn-small" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function RunUnresolved({
  lookedFor,
  reason,
  onDismiss,
}: {
  lookedFor: string[];
  reason: string | null;
  onDismiss: () => void;
}) {
  return (
    <div className="coding-runtime-unresolved" aria-label="How to run unknown">
      <h4>Errorta doesn’t know how to run this yet</h4>
      <p>
        {reason === "no_worktree"
          ? "This project has no files to run yet."
          : "Nothing grounded resolved. Here’s what Run looked for:"}
      </p>
      {lookedFor.length ? (
        <ul>
          {lookedFor.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : null}
      <p className="coding-file-note">
        Add a runtime profile in Advanced controls, or ask the Coding Team to add
        a run profile.
      </p>
      <button type="button" className="coding-btn coding-btn-small" onClick={onDismiss}>
        Dismiss
      </button>
    </div>
  );
}

function RuntimeSummary({
  profile,
  session,
  state,
  liveDemoUrl,
  onOpenDemo,
  openDemoDisabled,
}: {
  profile: RuntimeProfile;
  session: RuntimeSession | null;
  state: PanelState;
  liveDemoUrl: string | null;
  onOpenDemo: () => void;
  openDemoDisabled: boolean;
}) {
  const demo = demoUrl(profile, session);
  return (
    <div className="coding-runtime-summary" aria-label="Runtime summary">
      <dl>
        <div>
          <dt>Profile</dt>
          <dd>{profile.profileId}</dd>
        </div>
        <div>
          <dt>Kind</dt>
          <dd>{profile.kind}</dd>
        </div>
        <div>
          <dt>Mode</dt>
          <dd>{profile.runtimeMode}</dd>
        </div>
        <div>
          <dt>State</dt>
          <dd>{stateLabel(state)}</dd>
        </div>
        <div>
          <dt>Sandbox</dt>
          <dd>{session?.sandboxBackend ?? profile.sandbox}</dd>
        </div>
        <div>
          <dt>Demo</dt>
          <dd>
            {liveDemoUrl ? (
              <button
                type="button"
                className="coding-link-btn coding-runtime-demo-link"
                onClick={onOpenDemo}
                disabled={openDemoDisabled}
                aria-label={`Open demo in browser: ${liveDemoUrl}`}
              >
                {liveDemoUrl}
              </button>
            ) : (
              demo ?? "none"
            )}
          </dd>
        </div>
      </dl>
      {profile.safetyWarnings.length ? (
        <ul className="coding-runtime-warnings">
          {profile.safetyWarnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function CommandPreview({ profile }: { profile: RuntimeProfile }) {
  return (
    <div className="coding-runtime-command-preview" aria-label="Runtime commands">
      <h4>Commands before first run</h4>
      {profile.setup.length ? (
        <div>
          <span>Setup</span>
          <ol>
            {profile.setup.map((argv, idx) => (
              <li key={`${idx}-${argv.join("\0")}`}>
                <code>{formatArgv(argv)}</code>
              </li>
            ))}
          </ol>
        </div>
      ) : (
        <p className="coding-empty">No setup command declared.</p>
      )}
      <div>
        <span>Start</span>
        <code>{formatArgv(profile.start)}</code>
      </div>
    </div>
  );
}

function HealthBlock({
  profile,
  session,
  healthResult,
  onCheck,
  disabled,
}: {
  profile: RuntimeProfile | null;
  session: RuntimeSession | null;
  healthResult: RuntimeHealthStatus | null;
  onCheck: () => void;
  disabled: boolean;
}) {
  const health = healthResult ?? session?.healthStatus ?? null;
  return (
    <div className="coding-runtime-block" aria-label="Health-check detail">
      <h4>Health</h4>
      {profile?.health ? (
        <dl>
          <div>
            <dt>Type</dt>
            <dd>{profile.health.type}</dd>
          </div>
          <div>
            <dt>Target</dt>
            <dd>{profile.health.url ?? "none"}</dd>
          </div>
          <div>
            <dt>Result</dt>
            <dd>
              {health ? (
                <span className={health.ok ? "coding-tc-pass" : "coding-tc-fail"}>
                  {health.ok ? "passed" : "failed"} {health.detail}
                </span>
              ) : (
                "not checked"
              )}
            </dd>
          </div>
        </dl>
      ) : (
        <p className="coding-empty">No health check declared.</p>
      )}
      <button
        type="button"
        className="coding-btn coding-btn-small"
        onClick={onCheck}
        disabled={disabled || !profile?.health}
      >
        Check health
      </button>
    </div>
  );
}

function TranscriptBlock({
  profile,
  session,
  logs,
}: {
  profile: RuntimeProfile;
  session: RuntimeSession | null;
  logs: string[];
}) {
  // Only a finished CLI transcript run produces a pass/fail verdict. While the
  // run is still active there is no exit code yet.
  const isTranscriptSession = session?.kind === "cli_transcript";
  const terminal = Boolean(
    session && !ACTIVE_STATES.has(session.state) && isTranscriptSession,
  );
  const timedOut = session?.error === "timed_out";
  const exitCode = session?.exitCode ?? null;
  const passed = session?.passed ?? (exitCode === 0 ? true : exitCode != null ? false : null);

  let badge: { className: string; text: string } | null = null;
  if (terminal) {
    if (timedOut) {
      const secs = profile.demo?.timeoutSeconds;
      badge = {
        className: "coding-tc-fail",
        text: secs
          ? `Timed out after ${secs}s (process killed)`
          : "Timed out (process killed)",
      };
    } else if (passed) {
      badge = { className: "coding-tc-pass", text: "passed · exit 0" };
    } else {
      badge = {
        className: "coding-tc-fail",
        text: exitCode == null ? "failed" : `failed · exit ${exitCode}`,
      };
    }
  }

  return (
    <div
      className="coding-runtime-block"
      aria-label={`CLI transcript for ${profile.profileId}`}
    >
      <h4>CLI transcript</h4>
      {badge ? (
        <p>
          <span className={badge.className}>{badge.text}</span>
        </p>
      ) : session && isTranscriptSession ? (
        <p className="coding-empty">Running…</p>
      ) : null}
      {session && isTranscriptSession ? (
        logs.length ? (
          <pre aria-label="CLI transcript output">{logs.join("\n")}</pre>
        ) : (
          <p className="coding-empty">No output captured yet.</p>
        )
      ) : (
        <p className="coding-empty">
          Run (CLI) to capture a transcript and exit code.
        </p>
      )}
    </div>
  );
}

function EnvBlock({ profile }: { profile: RuntimeProfile | null }) {
  return (
    <div className="coding-runtime-block" aria-label="Environment requirements">
      <h4>Environment</h4>
      {profile?.envRequired.length ? (
        <ul>
          {profile.envRequired.map((env) => (
            <li key={env}>
              <code>{env}</code>
            </li>
          ))}
        </ul>
      ) : (
        <p className="coding-empty">No environment variables declared.</p>
      )}
    </div>
  );
}

function EvidenceBlock({
  evidence,
  result,
}: {
  evidence: TestRun | null;
  result: RuntimeTestResult | null;
}) {
  return (
    <div className="coding-runtime-block" aria-label="Latest tester evidence">
      <h4>Latest tester evidence</h4>
      {result ? (
        <p className={result.passed === false ? "coding-tc-fail" : "coding-tc-pass"}>
          Runtime test {result.passed === false ? "failed" : "recorded"}:{" "}
          {result.detail || result.kind}
        </p>
      ) : evidence ? (
        <p>
          <span className={evidence.passed ? "coding-tc-pass" : "coding-tc-fail"}>
            {evidence.passed ? "passed" : "failed"}
          </span>{" "}
          {evidence.commandIds.join(", ") || "test command"} · sandbox{" "}
          {evidence.sandbox || "unknown"}
        </p>
      ) : (
        <p className="coding-empty">No runtime evidence recorded yet.</p>
      )}
    </div>
  );
}
