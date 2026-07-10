// INTEGRATION — first-run onboarding wizard.
// Residency → Hardware → Connect AI → Sample corpus. The user can skip at any
// step; sidebar navigation is never blocked (App.tsx controls that gate).
// Residency is first because hardware scan and model setup depend on whether
// Errorta is targeting this Mac, a server, or a hosted sidecar. The Judge and
// Briefs steps were removed (F132) — those features are discovered in-app.
import { useEffect, useState } from "react";
import StepHardware from "./StepHardware";
import StepResidency from "./StepResidency";
import StepConnectAI from "./StepConnectAI";
import StepWelcome from "./StepWelcome";
import { useOnboardingState } from "./useOnboardingState";
import "./onboarding.css";

// F040-02: the standalone "ollama" step was folded into "connect-ai" (the
// "Connect your AI" step now owns Ollama detect/install alongside provider keys
// + subscription CLIs). connect-ai sits after hardware so the hardware scan has
// already run.
type StepKey = "hardware" | "residency" | "connect-ai" | "welcome";

// NOTE: this list is duplicated below in `advance()` as `order`. Both copies
// must stay in sync. If you edit the step order, edit BOTH places.
const STEPS: { key: StepKey; label: string }[] = [
  { key: "residency", label: "Data residency" },
  { key: "hardware", label: "Hardware" },
  { key: "connect-ai", label: "Connect AI" },
  { key: "welcome", label: "Sample corpus" },
];

interface Props {
  onComplete: () => void;
}

export default function OnboardingFlow({ onComplete }: Props) {
  const { state, loaded, refresh } = useOnboardingState();
  const [step, setStep] = useState<StepKey>("residency");
  const [touched, setTouched] = useState(false);

  // On first load, jump to the server-recommended next step (so a returning
  // user with hardware already scanned lands on Ollama, not Hardware).
  //
  // Server state owns the residency-first recommendation. The localStorage
  // sentinel remains as a fast UI marker immediately after Apply.
  useEffect(() => {
    if (!loaded || touched) return;
    const next = state.recommended_next_step;
    const residencySeen =
      state.residency_ready ||
      typeof window !== "undefined" &&
      window.localStorage?.getItem("errorta.onboarding.residency.seen") === "1";
    if (!residencySeen) {
      setStep("residency");
    } else if (next === "hardware") {
      setStep("hardware");
    } else if (next === "ollama") {
      // F040-02: the backend still recommends "ollama"; map it to the folded-in
      // "connect-ai" step.
      setStep("connect-ai");
    } else if (next === "welcome") {
      setStep("welcome");
    } else {
      // F132: "judge"/"briefs"/"done" are removed/terminal — land on the last
      // step (Sample corpus) so the user sees the handoff rather than the wizard
      // auto-dismissing.
      setStep("welcome");
    }
  }, [loaded, state.recommended_next_step, state.residency_ready, touched]);

  const advance = async (from: StepKey) => {
    setTouched(true);
    await refresh();
    // Duplicate of `STEPS` keys above — see the note on STEPS.
    const order: StepKey[] = ["residency", "hardware", "connect-ai", "welcome"];
    const i = order.indexOf(from);
    setStep(order[Math.min(i + 1, order.length - 1)]);
  };

  const currentIndex = STEPS.findIndex((s) => s.key === step);
  const progress = ((currentIndex + 1) / STEPS.length) * 100;

  return (
    <section className="onboarding-flow">
      <div className="onboarding-topbar">
        <header className="onboarding-header">
          <h1>Welcome to Errorta</h1>
          <p className="onboarding-subtitle">
            A quick setup for data residency, model fit, and your first corpus.
            You can skip any step.
          </p>
        </header>

        <div
          className="onboarding-progress"
          aria-label={`Step ${currentIndex + 1} of ${STEPS.length}`}
        >
          <span
            className="onboarding-progress-bar"
            style={{ width: `${progress}%` }}
          />
        </div>

        <ol className="onboarding-steps">
          {STEPS.map((s, i) => {
            const isCurrent = s.key === step;
            const isDone =
              (s.key === "hardware" && state.hardware_ready) ||
              (s.key === "residency" &&
                (state.residency_ready ||
                  (typeof window !== "undefined" &&
                    window.localStorage?.getItem(
                      "errorta.onboarding.residency.seen",
                    ) === "1"))) ||
              (s.key === "connect-ai" &&
                (state.ollama_ready ||
                  (typeof window !== "undefined" &&
                    window.localStorage?.getItem(
                      "errorta.onboarding.connect-ai.seen",
                    ) === "1"))) ||
              (s.key === "welcome" && state.corpora_present);
            return (
              <li
                key={s.key}
                className={[
                  "onboarding-step-pill",
                  isCurrent ? "is-current" : "",
                  isDone ? "is-done" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <button
                  type="button"
                  className="onboarding-step-pill-btn"
                  aria-current={isCurrent ? "step" : undefined}
                  onClick={() => {
                    setTouched(true);
                    setStep(s.key);
                  }}
                >
                  <span className="onboarding-step-num">{i + 1}</span>
                  <span className="onboarding-step-label">{s.label}</span>
                </button>
              </li>
            );
          })}
        </ol>
      </div>

      <div className="onboarding-body" data-step={step} data-index={currentIndex}>
        {step === "hardware" ? (
          <StepHardware
            done={state.hardware_ready}
            onAdvance={() => advance("hardware")}
            onSkip={onComplete}
          />
        ) : null}
        {step === "residency" ? (
          <StepResidency
            onAdvance={() => advance("residency")}
            onSkip={onComplete}
          />
        ) : null}
        {step === "connect-ai" ? (
          <StepConnectAI
            done={state.ollama_ready}
            onAdvance={() => advance("connect-ai")}
            onSkip={onComplete}
          />
        ) : null}
        {step === "welcome" ? (
          <StepWelcome
            done={state.corpora_present}
            onAdvance={onComplete}
            onSkip={onComplete}
          />
        ) : null}
      </div>
    </section>
  );
}
