import { useState } from "react";
import * as api from "../../lib/api/coding";
import type {
  AutonomyPolicy,
  GovernanceMode,
  GovernanceSummary,
} from "../../lib/api/coding";
import PmLearningSheet from "./PmLearningSheet";

const MODES: GovernanceMode[] = ["off", "light", "strict"];
const CADENCES = ["off", "every_n_tasks", "per_milestone", "on_merge_ready"];

function artifactLabel(kind: string): string {
  return kind.replace(/_/g, " ");
}

export interface GovernancePanelProps {
  projectId: string;
  governance: GovernanceSummary | null;
  running?: boolean;
  guardrailEnabled?: boolean;
  autonomy?: AutonomyPolicy | null;
  onToggleGuardrail?: (enabled: boolean) => void;
  onChangeCadence?: (cadence: string) => void;
  onChanged?: () => void;
  onError?: (message: string) => void;
}

export default function GovernancePanel({
  projectId,
  governance,
  running = false,
  guardrailEnabled = true,
  autonomy = null,
  onToggleGuardrail,
  onChangeCadence,
  onChanged,
  onError,
}: GovernancePanelProps) {
  const [learningOpen, setLearningOpen] = useState(false);
  const state = governance?.state;
  const pending = governance?.approvals.filter((a) => a.state === "pending") ?? [];
  const artifacts = governance?.artifacts ?? [];
  const slices = governance?.planSlices ?? [];

  async function run(action: () => Promise<unknown>) {
    try {
      await action();
      onChanged?.();
    } catch (err) {
      onError?.(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <details className="coding-panel coding-governance">
      <summary>
        <span>PM Governance</span>
        <span className="coding-count">{state?.mode ?? "off"}</span>
      </summary>
      <section aria-label="PM Governance">
        <div className="coding-governance-head">
          <label>
            <span>Human in the loop</span>
            <select
              value={state?.mode ?? "off"}
              disabled={running}
              aria-label="Human in the loop"
              onChange={(e) =>
                void run(() =>
                  api.putGovernanceSettings(projectId, {
                    mode: e.target.value as GovernanceMode,
                  }),
                )
              }
            >
              {MODES.map((mode) => (
                <option key={mode} value={mode}>
                  {mode}
                </option>
              ))}
            </select>
          </label>
          <span className="coding-governance-phase">
            Phase: <code>{state?.phase ?? "idle"}</code>
          </span>
        </div>
        <p className="coding-field-hint">
          How much <strong>you</strong> gate the PM. <code>off</code> = it plans,
          builds, and ships without your approval; <code>light</code> and{" "}
          <code>strict</code> add spec &amp; review gates you must accept.
        </p>

        <div className="coding-governance-learning">
          <button
            type="button"
            className="coding-btn coding-btn-small"
            onClick={() => setLearningOpen(true)}
          >
            What the PM has learned →
          </button>
          <span className="coding-field-hint">
            Model performance the PM has accumulated across all your projects.
          </span>
        </div>
        <PmLearningSheet
          isOpen={learningOpen}
          onClose={() => setLearningOpen(false)}
        />

        <label className="coding-toggle">
          <input
            type="checkbox"
            checked={state?.blockOnProblems !== false}
            disabled={running}
            aria-label="Block on Showstoppers"
            onChange={(e) =>
              void run(() =>
                api.putGovernanceSettings(projectId, {
                  blockOnProblems: e.target.checked,
                }),
              )
            }
          />
          <span>Block on Showstoppers</span>
        </label>

        <div className="coding-governance-autonomy" role="group" aria-label="Run guardrails">
          <label className="coding-toggle">
            <input
              type="checkbox"
              checked={guardrailEnabled}
              onChange={(e) => onToggleGuardrail?.(e.target.checked)}
            />
            <span>Superpowers Guardrail</span>
          </label>
          <label>
            <span>Checkpoint cadence</span>
            <select
              value={autonomy?.checkpointCadence ?? "per_milestone"}
              onChange={(e) => onChangeCadence?.(e.target.value)}
              aria-label="Checkpoint cadence"
            >
              {CADENCES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          {autonomy ? (
            <span className="coding-cap">Budget: {autonomy.maxIterations} iterations</span>
          ) : null}
        </div>

        {pending.length ? (
          <div className="coding-governance-approvals" role="group" aria-label="Pending approvals">
            <h4>Pending approvals</h4>
            <ul>
              {pending.slice().reverse().map((approval) => (
                <li key={approval.approvalId}>
                  <span className="coding-task-role">{approval.kind}</span>
                  <code>{approval.artifactId}</code>
                  <button
                    type="button"
                    className="coding-btn coding-btn-small"
                    onClick={() =>
                      void run(() =>
                        api.approveGovernanceApproval(projectId, approval.approvalId),
                      )
                    }
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="coding-btn coding-btn-small"
                    onClick={() => {
                      const feedback = window.prompt("Feedback for the PM") ?? "";
                      void run(() =>
                        api.rejectGovernanceApproval(
                          projectId,
                          approval.approvalId,
                          feedback,
                        ),
                      );
                    }}
                  >
                    Reject
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="coding-empty">No pending governance approvals.</p>
        )}

        {artifacts.length ? (
          <ul className="coding-governance-artifacts" aria-label="Governance artifacts">
            {artifacts.slice().reverse().map((artifact) => (
              <li key={artifact.artifactId}>
                <span className="coding-task-role">{artifactLabel(artifact.artifactKind)}</span>
                <strong>{artifact.title}</strong>
                <span className={`coding-art-status coding-gov-${artifact.state}`}>
                  {artifact.state}
                </span>
                <code>{artifact.artifactId}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p className="coding-empty">No governance artifacts yet.</p>
        )}

        {slices.length ? (
          <div className="coding-governance-slices" aria-label="Approved plan slices">
            <h4>Approved plan slices</h4>
            <ol>
              {slices.map((slice) => (
                <li key={slice.sliceId}>
                  <code>{slice.sliceId}</code>
                  <span>{slice.title}</span>
                </li>
              ))}
            </ol>
          </div>
        ) : null}
      </section>
    </details>
  );
}
