// INTEGRATION — onboarding state aggregation client.
import { getJSON } from "../api";

export type OnboardingStep =
  | "residency"
  | "hardware"
  | "ollama"
  | "welcome"
  | "judge"
  | "done";

export interface OnboardingState {
  residency_ready: boolean;
  residency_mode: "local" | "ssh-remote" | "cloud";
  hardware_ready: boolean;
  ollama_ready: boolean;
  corpora_present: boolean;
  judge_ready: boolean;
  recommended_next_step: OnboardingStep;
  corpora: string[];
  ollama_error: string | null;
}

export interface CorpusListItem {
  name: string;
  file_count: number;
  ready_count: number;
}

export interface CorpusList {
  corpora: CorpusListItem[];
}

export function getState(): Promise<OnboardingState> {
  return getJSON<OnboardingState>("/onboarding/state");
}

export function listCorpora(): Promise<CorpusList> {
  return getJSON<CorpusList>("/onboarding/corpora");
}
