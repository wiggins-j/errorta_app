import { useCallback, useEffect, useMemo, useState } from "react";

import {
  listCorpora,
  type CorpusCapabilities,
  type CorpusSummary,
} from "../../lib/api/corpus";
import {
  getStoredKnowledgeCorpus,
  KNOWLEDGE_CORPUS_EVENT,
  KNOWLEDGE_CORPUS_STORAGE_KEY,
  setKnowledgeCorpus,
} from "../../lib/featureNavigation";

export interface KnowledgeCorpusSelection {
  corpora: CorpusSummary[];
  loading: boolean;
  error: string | null;
  selectedName: string;
  selected: CorpusSummary | null;
  setSelectedName(next: string): void;
  reload(): Promise<CorpusSummary[]>;
}

const DISABLED_CAPABILITIES: CorpusCapabilities = {
  list_files: false,
  upload_files: false,
  folder_watch: false,
  refresh_preview: false,
  remote_ingest: false,
};

const LOCAL_CAPABILITIES: CorpusCapabilities = {
  list_files: true,
  upload_files: true,
  folder_watch: true,
  refresh_preview: true,
  remote_ingest: false,
};

export function missingCorpus(name: string): CorpusSummary {
  return {
    name,
    fileCount: 0,
    readyCount: 0,
    status: "missing",
    source: "unknown",
    unit: "files",
    capabilities: { ...DISABLED_CAPABILITIES },
  };
}

export function draftLocalCorpus(name: string): CorpusSummary {
  return {
    name,
    fileCount: 0,
    readyCount: 0,
    status: "empty",
    source: "local",
    unit: "files",
    capabilities: { ...LOCAL_CAPABILITIES },
  };
}

export function useKnowledgeCorpusSelection(): KnowledgeCorpusSelection {
  const [corpora, setCorpora] = useState<CorpusSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedName, setSelectedNameState] = useState(getStoredKnowledgeCorpus);

  const setSelectedName = useCallback((next: string) => {
    setSelectedNameState(next);
    setKnowledgeCorpus(next);
  }, []);

  const reload = useCallback(async (): Promise<CorpusSummary[]> => {
    setLoading(true);
    try {
      const items = await listCorpora();
      setCorpora(items);
      setError(null);
      // `picked` is set only when we auto-select a default (no current/stored
      // value). Persisting must happen OUTSIDE the updater — the updater has to
      // be pure (StrictMode invokes it twice), and setKnowledgeCorpus()
      // dispatches a DOM event + writes localStorage.
      let picked: string | null = null;
      setSelectedNameState((current) => {
        if (current) return current;
        const stored = getStoredKnowledgeCorpus();
        if (stored) return stored;
        picked = items[0]?.name ?? "";
        return picked;
      });
      if (picked) setKnowledgeCorpus(picked);
      return items;
    } catch (err) {
      setCorpora([]);
      setError(err instanceof Error ? err.message : String(err));
      return [];
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    const onKnowledgeCorpus = (event: Event) => {
      const detail = (event as CustomEvent<{ corpus?: string }>).detail;
      setSelectedNameState(detail?.corpus ?? "");
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === KNOWLEDGE_CORPUS_STORAGE_KEY) {
        setSelectedNameState(event.newValue ?? "");
      }
    };
    window.addEventListener(KNOWLEDGE_CORPUS_EVENT, onKnowledgeCorpus);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(KNOWLEDGE_CORPUS_EVENT, onKnowledgeCorpus);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const selected = useMemo(() => {
    if (!selectedName) return null;
    return corpora.find((c) => c.name === selectedName) ?? missingCorpus(selectedName);
  }, [corpora, selectedName]);

  return {
    corpora,
    loading,
    error,
    selectedName,
    selected,
    setSelectedName,
    reload,
  };
}
