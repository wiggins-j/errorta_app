// F103 — full-window cold-launch splash. Shown by App.tsx until the local
// sidecar has returned /healthz once (or the user picks limited mode after a
// failure). Deliberately uses NO circular spinner and NO busy cursor — a
// horizontal progress rail (CSS-animated, disabled under reduced motion) plus
// changing phase copy carries the "alive but waiting" signal.
import type { StartupActions, StartupState } from "../lib/useStartupGate";

interface Props {
  /** Whether we are still loading or have entered the failure state. */
  failed: boolean;
  state: StartupState;
  actions: StartupActions;
}

const PHASE_STEPS = [
  { key: "shell", label: "Opening desktop shell" },
  { key: "sidecar", label: "Starting local sidecar" },
  { key: "ai", label: "Loading local AI services" },
  { key: "ready", label: "Ready" },
] as const;

// Map the gate's phase to the index of the currently-active step.
function activeStepIndex(phase: StartupState["phase"]): number {
  switch (phase) {
    case "opening_shell":
      return 0;
    case "waiting_for_port":
      return 1;
    case "waiting_for_healthz":
      return 2;
    case "ready":
      return 3;
    default:
      return 2;
  }
}

const LONG_BOOT_MS = 12_000;

export function StartupSplash({ failed, state, actions }: Props) {
  const stepIndex = activeStepIndex(state.phase);
  const longBoot = !failed && state.elapsedMs >= LONG_BOOT_MS;

  const detail = failed
    ? state.lastError
      ? `The local backend reported: ${state.lastError}`
      : "The local backend didn't become ready in time."
    : state.developerMode
      ? "Developer mode — connecting to the manually-run sidecar on the dev port."
      : longBoot
        ? "Still starting. This is normal after an update or cold restart."
        : "Preparing the local backend. First launch can take up to two minutes.";

  return (
    <div className="errorta-startup" data-failed={failed ? "true" : "false"}>
      <div className="errorta-startup-inner">
        <div className="errorta-startup-brand">Errorta</div>

        <div className="errorta-startup-status" role="status" aria-live="polite">
          {failed && (
            <p className="errorta-startup-message">
              Errorta couldn't start the local backend
            </p>
          )}
          <p className="errorta-startup-detail">{detail}</p>
        </div>

        {!failed && (
          <>
            <div
              className="errorta-startup-rail"
              role="presentation"
              aria-hidden="true"
            >
              <span className="errorta-startup-rail-fill" />
            </div>
            <ol className="errorta-startup-steps">
              {PHASE_STEPS.map((step, i) => {
                const status =
                  i < stepIndex ? "done" : i === stepIndex ? "active" : "pending";
                return (
                  <li
                    key={step.key}
                    className="errorta-startup-step"
                    data-status={status}
                  >
                    {step.label}
                  </li>
                );
              })}
            </ol>
          </>
        )}

        {failed && (
          <div className="errorta-startup-actions">
            <button
              type="button"
              className="errorta-startup-btn errorta-startup-btn-primary"
              onClick={actions.retry}
            >
              Retry startup
            </button>
            <button
              type="button"
              className="errorta-startup-btn"
              onClick={() => {
                void actions.openLogs();
              }}
            >
              Open logs
            </button>
            <button
              type="button"
              className="errorta-startup-btn"
              onClick={actions.openLimited}
            >
              Open in limited mode
            </button>
            <button
              type="button"
              className="errorta-startup-btn"
              onClick={() => {
                void actions.quit();
              }}
            >
              Quit Errorta
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default StartupSplash;
