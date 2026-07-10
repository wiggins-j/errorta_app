// F121 Part B — the pre-first-run readiness gate ("Run setup"). One view, no
// hidden Advanced drawer: a preset selector that PRE-FILLS every covered setting
// but leaves them all visible + editable; editing any field flips the active
// label to "Custom" (values kept). The Team section runs the F120 provider-auth
// preflight; "Ready to run" is disabled while a required (PM/drafting) member is
// unauthenticated. On confirm the resolved config is applied via the existing
// setters (server-side) and remembered as the user's next-project default.
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  confirmRunSetup,
  runSetupPreflight,
  type PreflightUnhealthyEntry,
  type RunSetupConfig,
} from "../../lib/api/coding";
import { getCliLoginCommand } from "../../lib/api/providerKeys";
import type { CouncilRoomSummary } from "../council/types";
import {
  matchPreset,
  PRESETS,
  RUN_SETUP_COVERED_FIELDS,
  withRunSetupDefaults,
  type PresetName,
} from "./runSetupPresets";

export interface RunSetupGateProps {
  projectId: string;
  rooms: CouncilRoomSummary[];
  /** The room currently selected as the team (drives the preflight). */
  teamRoomId: string;
  onTeamRoomChange: (roomId: string) => void;
  /** The pre-fill seed: the project's live config OR the sticky defaults. */
  initialConfig: RunSetupConfig;
  /** Whether grounding is bound (shown read-only; bound elsewhere). */
  groundingBound?: boolean;
  onClose: () => void;
  /** Called after a successful confirm — the container then starts the run. */
  onConfirmed: () => void;
}

const GOVERNANCE_MODES = ["off", "light", "strict"];
const GOVERNANCE_MODE_LABELS: Record<string, string> = {
  off: "Off",
  light: "Light",
  strict: "Strict",
};
// Plain-language explanation of each "Human in the loop" level, shown behind the ⓘ.
const GOVERNANCE_MODE_HELP: Array<{ mode: string; text: string }> = [
  {
    mode: "Off",
    text: "No reviewer and no approvals. The PM plans and the team builds right away. Fully autonomous — you're only involved if you stop it.",
  },
  {
    mode: "Light",
    text: "A reviewer checks the spec and plan and the PM revises, but the PM has the final say: if they can't agree, the PM decides and the run keeps going. You're only pulled in for the finished result.",
  },
  {
    mode: "Strict",
    text: "The reviewer and PM must both agree on each artifact; if they deadlock, the run pauses and asks you to break the tie.",
  },
];
const APPROVAL_MODES = ["none", "per_slice", "per_milestone", "final_only"];
const CADENCES = ["off", "every_n_tasks", "per_milestone", "on_merge_ready"];

export default function RunSetupGate({
  projectId,
  rooms,
  teamRoomId,
  onTeamRoomChange,
  initialConfig,
  groundingBound = false,
  onClose,
  onConfirmed,
}: RunSetupGateProps) {
  const [config, setConfig] = useState<RunSetupConfig>(() =>
    withRunSetupDefaults(initialConfig),
  );
  const [unhealthy, setUnhealthy] = useState<PreflightUnhealthyEntry[]>([]);
  const [checking, setChecking] = useState(false);
  const [checkedOnce, setCheckedOnce] = useState(false);
  const [showHitlInfo, setShowHitlInfo] = useState(false);
  const [checkedTeamRoomId, setCheckedTeamRoomId] = useState<string | null>(null);
  const [loginCmds, setLoginCmds] = useState<Record<string, string>>({});
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activePreset: PresetName = useMemo(() => matchPreset(config), [config]);

  const applyPreset = (name: Exclude<PresetName, "custom">) => {
    // Pre-fill the covered fields from the preset; keep team + grounding choices.
    setConfig((prev) => ({ ...prev, ...PRESETS[name] }));
  };

  const setField = <K extends keyof RunSetupConfig>(key: K, value: RunSetupConfig[K]) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  const recheck = useCallback(async () => {
    if (!teamRoomId) {
      setUnhealthy([]);
      setCheckedOnce(false);
      setCheckedTeamRoomId(null);
      return;
    }
    setChecking(true);
    setError(null);
    try {
      const list = await runSetupPreflight(projectId, teamRoomId);
      setUnhealthy(list);
      // Fetch the corrected login command for each logged-out provider.
      const cmds: Record<string, string> = {};
      await Promise.all(
        list.map(async (u) => {
          try {
            const lc = await getCliLoginCommand(u.provider);
            if (lc.loginArgv.length) cmds[u.provider] = lc.loginArgv.join(" ");
          } catch {
            /* metadata fetch best-effort — the remediation text still shows */
          }
        }),
      );
      setLoginCmds(cmds);
      setCheckedOnce(true);
      setCheckedTeamRoomId(teamRoomId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setUnhealthy([]);
      setCheckedOnce(false);
      setCheckedTeamRoomId(null);
    } finally {
      setChecking(false);
    }
  }, [projectId, teamRoomId]);

  // Re-run the auth preflight whenever the team changes.
  useEffect(() => {
    void recheck();
  }, [recheck]);

  // D4: block "Ready to run" while any required member is unauthenticated. v1
  // blocks on any unhealthy provider (the preflight only flags auth/availability
  // failures that would stall a run). A redundant reviewer warning could relax
  // this later; for now any unhealthy provider blocks (locked by a test).
  const readyDisabled =
    confirming ||
    checking ||
    !checkedOnce ||
    checkedTeamRoomId !== teamRoomId ||
    unhealthy.length > 0 ||
    !teamRoomId;

  const onReady = async () => {
    setConfirming(true);
    setError(null);
    try {
      await confirmRunSetup(projectId, { ...config, teamRoomId });
      onConfirmed();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setConfirming(false);
    }
  };

  return (
    <div className="coding-run-setup" role="dialog" aria-modal="true" aria-label="Run setup">
      <div className="coding-run-setup-panel">
        <header className="coding-run-setup-head">
          <h3>Run setup</h3>
          <p className="coding-run-setup-sub">
            Choose a preset and review every setting before the team runs. Nothing
            is hidden — the active preset is{" "}
            <strong data-testid="active-preset">{activePreset}</strong>.
          </p>
        </header>

        <section className="coding-run-setup-presets" aria-label="Preset">
          {(["careful", "autonomous"] as const).map((name) => (
            <button
              key={name}
              type="button"
              className={
                activePreset === name
                  ? "coding-btn coding-preset-active"
                  : "coding-btn"
              }
              aria-pressed={activePreset === name}
              onClick={() => applyPreset(name)}
            >
              {name === "careful" ? "Careful" : "Autonomous"}
            </button>
          ))}
          <span className="coding-preset-label">
            {activePreset === "custom" ? "Custom (edited)" : ""}
          </span>
        </section>

        {error ? (
          <p className="coding-error" role="alert">
            {error}
          </p>
        ) : null}

        {/* Human in the loop (governance) */}
        <fieldset className="coding-run-setup-group">
          <legend className="coding-hitl-legend">
            <span>Human in the loop</span>
            <button
              type="button"
              className="coding-info-toggle"
              aria-expanded={showHitlInfo}
              aria-label="What do Off, Light, and Strict mean?"
              title="What do Off, Light, and Strict mean?"
              onClick={() => setShowHitlInfo((v) => !v)}
            >
              ⓘ
            </button>
          </legend>
          {showHitlInfo ? (
            <dl className="coding-hitl-info" role="note">
              {GOVERNANCE_MODE_HELP.map((h) => (
                <div key={h.mode}>
                  <dt>{h.mode}</dt>
                  <dd>{h.text}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          <label>
            <span>Human in the loop</span>
            <select
              aria-label="Human in the loop"
              value={config.governanceMode ?? "light"}
              onChange={(e) =>
                setField("governanceMode", e.target.value as RunSetupConfig["governanceMode"])
              }
            >
              {GOVERNANCE_MODES.map((m) => (
                <option key={m} value={m}>
                  {GOVERNANCE_MODE_LABELS[m] ?? m}
                </option>
              ))}
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={Boolean(config.blockOnProblems)}
              onChange={(e) => setField("blockOnProblems", e.target.checked)}
            />
            Block on showstoppers
          </label>
          <label>
            Human code approval
            <select
              aria-label="Human code approval"
              aria-describedby="coding-code-approval-status"
              value={config.humanCodeApproval ?? "final_only"}
              onChange={(e) =>
                setField(
                  "humanCodeApproval",
                  e.target.value as RunSetupConfig["humanCodeApproval"],
                )
              }
            >
              {APPROVAL_MODES.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <small id="coding-code-approval-status">
              Saved preference; code-approval pauses are not enforced yet.
            </small>
          </label>
          <label>
            Max review rounds
            <input
              type="number"
              min={0}
              value={config.maxReviewRounds ?? 2}
              onChange={(e) => setField("maxReviewRounds", Number(e.target.value))}
            />
          </label>
        </fieldset>

        {/* Cadence */}
        <fieldset className="coding-run-setup-group">
          <legend>Checkpoint cadence</legend>
          <label>
            Cadence
            <select
              value={config.checkpointCadence ?? "per_milestone"}
              onChange={(e) =>
                setField(
                  "checkpointCadence",
                  e.target.value as RunSetupConfig["checkpointCadence"],
                )
              }
            >
              {CADENCES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label>
            Every N tasks
            <input
              type="number"
              min={1}
              value={config.checkpointN ?? 5}
              disabled={config.checkpointCadence !== "every_n_tasks"}
              onChange={(e) => setField("checkpointN", Number(e.target.value))}
            />
          </label>
        </fieldset>

        {/* Guardrail */}
        <fieldset className="coding-run-setup-group">
          <legend>Superpowers guardrail</legend>
          <label>
            <input
              type="checkbox"
              checked={Boolean(config.guardrailEnabled)}
              onChange={(e) => setField("guardrailEnabled", e.target.checked)}
            />
            On
          </label>
        </fieldset>

        {/* Spend caps */}
        <fieldset className="coding-run-setup-group">
          <legend>Spend caps</legend>
          <label>
            Max iterations
            <input
              type="number"
              min={1}
              value={config.maxIterations ?? 50}
              onChange={(e) => setField("maxIterations", Number(e.target.value))}
            />
          </label>
          <label>
            Max model calls
            <input
              type="number"
              min={0}
              placeholder="unlimited"
              value={config.maxModelCalls ?? ""}
              onChange={(e) =>
                setField(
                  "maxModelCalls",
                  e.target.value === "" ? null : Number(e.target.value),
                )
              }
            />
          </label>
          <label>
            Max parallel workers
            <input
              type="number"
              min={1}
              placeholder="auto"
              value={config.maxParallelWorkers ?? ""}
              onChange={(e) =>
                setField(
                  "maxParallelWorkers",
                  e.target.value === "" ? null : Number(e.target.value),
                )
              }
            />
          </label>
          <label>
            Member failure limit
            <input
              type="number"
              min={1}
              value={config.memberFailureLimit ?? 3}
              onChange={(e) => setField("memberFailureLimit", Number(e.target.value))}
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={Boolean(config.preflightEnabled)}
              onChange={(e) => setField("preflightEnabled", e.target.checked)}
            />
            Provider auth preflight
          </label>
        </fieldset>

        {/* Grounding (read-only marker — bound in the Knowledge panel) */}
        <fieldset className="coding-run-setup-group">
          <legend>Grounding</legend>
          <p className="coding-run-setup-note">
            {groundingBound
              ? "Bound to a corpus — answers will use grounding."
              : "Greenfield — no corpus bound. Bind one in Knowledge if needed."}
          </p>
        </fieldset>

        {/* Team + auth preflight */}
        <fieldset className="coding-run-setup-group" aria-label="Team">
          <legend>Team</legend>
          <label>
            Room
            <select
              value={teamRoomId}
              onChange={(e) => onTeamRoomChange(e.target.value)}
            >
              {rooms.length === 0 ? (
                <option value="">No rooms — create one in Council</option>
              ) : null}
              {rooms.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name || r.id}
                </option>
              ))}
            </select>
          </label>
          <div className="coding-run-setup-auth" aria-label="Provider auth">
            {checking ? (
              <span className="coding-run-setup-note">Checking provider sign-in…</span>
            ) : unhealthy.length === 0 ? (
              <span className="coding-auth-badge coding-auth-ok" role="status">
                {checkedOnce && teamRoomId
                  ? "✓ All required providers are signed in"
                  : "Select a team to check provider sign-in"}
              </span>
            ) : (
              <ul className="coding-auth-list">
                {unhealthy.map((u) => (
                  <li key={u.provider} className="coding-auth-badge coding-auth-bad">
                    <strong>{u.provider}</strong> — not logged in. {u.remediation}
                    {loginCmds[u.provider] ? (
                      <code className="coding-auth-cmd">{loginCmds[u.provider]}</code>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
            <button
              type="button"
              className="coding-btn"
              onClick={() => void recheck()}
              disabled={checking || !teamRoomId}
            >
              Re-check
            </button>
          </div>
        </fieldset>

        <footer className="coding-run-setup-foot">
          <button type="button" className="coding-btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="coding-btn coding-btn-accept"
            onClick={() => void onReady()}
            disabled={readyDisabled}
          >
            {confirming ? "Saving…" : "Ready to run"}
          </button>
        </footer>
      </div>
    </div>
  );
}

// Re-exported so the coverage test (criterion 9) imports the single source.
export { RUN_SETUP_COVERED_FIELDS };
