// First-run onboarding — a single "Connect your AI" screen.
//
// Providers API keys + subscription CLIs + local Ollama. That's the only thing
// that gates "I can ask Errorta something," so it's the whole first-run flow.
// Everything else — AIAR retrieval, data residency, the sample corpus, hardware
// tuning — lives in Settings and is discovered in-app. See docs/AIAR_SETUP.md.
//
// Both "Continue" and every "Skip" affordance in the step call onComplete;
// App.tsx owns the `errorta.onboarding.complete` marker.
import StepConnectAI from "./StepConnectAI";
import "./onboarding.css";

interface Props {
  onComplete: () => void;
}

export default function OnboardingFlow({ onComplete }: Props) {
  return (
    <section className="onboarding-flow">
      <div className="onboarding-topbar">
        <header className="onboarding-header">
          <h1>Welcome to Errorta</h1>
          <p className="onboarding-subtitle">
            Connect the AI you want to use. It&rsquo;s all optional, and you can
            change everything later in <strong>Settings</strong>.
          </p>
        </header>
      </div>

      <div className="onboarding-body">
        <StepConnectAI onAdvance={onComplete} onSkip={onComplete} />
      </div>
    </section>
  );
}
