export type KnowledgeFeature = "briefs" | "corpus" | "watch";

export interface KnowledgeNavigationTarget {
  feature: KnowledgeFeature;
  corpus?: string;
}

export const KNOWLEDGE_CORPUS_STORAGE_KEY = "errorta.knowledge.activeCorpus";
export const KNOWLEDGE_CORPUS_EVENT = "errorta:knowledge-corpus";
export const APP_NAVIGATION_EVENT = "errorta:navigate";

export function getStoredKnowledgeCorpus(): string {
  try {
    return localStorage.getItem(KNOWLEDGE_CORPUS_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setKnowledgeCorpus(corpus: string): void {
  try {
    if (corpus) {
      localStorage.setItem(KNOWLEDGE_CORPUS_STORAGE_KEY, corpus);
    } else {
      localStorage.removeItem(KNOWLEDGE_CORPUS_STORAGE_KEY);
    }
  } catch {
    // Ignore storage failures; the custom event still updates this window.
  }
  window.dispatchEvent(
    new CustomEvent(KNOWLEDGE_CORPUS_EVENT, {
      detail: { corpus },
    }),
  );
}

export function navigateKnowledge(target: KnowledgeNavigationTarget): void {
  if (target.corpus !== undefined) {
    setKnowledgeCorpus(target.corpus);
  }
  window.dispatchEvent(
    new CustomEvent(APP_NAVIGATION_EVENT, {
      detail: { view: target.feature },
    }),
  );
}
