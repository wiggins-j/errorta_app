// INTEGRATION — onboarding state polling hook.
import { useEffect, useState } from "react";
import * as onboardingApi from "../../lib/api/onboarding";
import type { OnboardingState } from "../../lib/api/onboarding";

const DEFAULT_STATE: OnboardingState = {
  residency_ready: false,
  residency_mode: "local",
  hardware_ready: false,
  ollama_ready: false,
  corpora_present: false,
  judge_ready: false,
  recommended_next_step: "residency",
  corpora: [],
  ollama_error: null,
};

export function useOnboardingState(pollMs = 3000) {
  const [state, setState] = useState<OnboardingState>(DEFAULT_STATE);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const s = await onboardingApi.getState();
        if (!cancelled) {
          setState(s);
          setError(null);
          setLoaded(true);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          setLoaded(true);
        }
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(tick, pollMs);
        }
      }
    };

    tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [pollMs]);

  const refresh = async () => {
    try {
      const s = await onboardingApi.getState();
      setState(s);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return { state, error, loaded, refresh };
}
