// INTEGRATION — onboarding gate entry point. App.tsx renders this when the
// onboarding flag is not yet set in localStorage. The real wizard lives in
// OnboardingFlow; this is a thin shim so App.tsx's lazy import stays stable.
import OnboardingFlow from "./OnboardingFlow";

export default function Onboarding({ onComplete }: { onComplete: () => void }) {
  return <OnboardingFlow onComplete={onComplete} />;
}
