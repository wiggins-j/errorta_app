import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import * as corpusApi from "../../lib/api/corpus";
import CorpusStatsFooter from "./CorpusStatsFooter";
import DuplicateFilePrompt from "./DuplicateFilePrompt";
import FileListTable from "./FileListTable";
import FileSizeConfirmDialog from "./FileSizeConfirmDialog";
import FileUploadArea from "./FileUploadArea";
import type {
  CorpusFile,
  CorpusStats,
  IngestionStatus,
  UploadResponse,
} from "./types";

const LARGE_BYTES_DEFAULT = 100 * 1024 * 1024;
const TOO_MANY_FILES = 100;

export default function CorpusDropZone({ corpus }: { corpus: string }) {
  const [files, setFiles] = useState<CorpusFile[]>([]);
  const [stats, setStats] = useState<CorpusStats>({
    file_count: 0,
    chunk_count: 0,
    token_count: 0,
    disk_bytes: 0,
  });
  const [extensions, setExtensions] = useState<string[]>([]);
  const [largeBytes, setLargeBytes] = useState(LARGE_BYTES_DEFAULT);
  const [pendingLarge, setPendingLarge] = useState<File[] | null>(null);
  const [duplicates, setDuplicates] = useState<string[] | null>(null);
  // Files that came back marked as duplicates — stashed so the "Re-ingest"
  // button on the duplicate prompt can actually re-upload them with
  // overwrite_duplicates=true. Without this the user's choice is a no-op.
  const [duplicateFiles, setDuplicateFiles] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const filesRef = useRef(files);
  filesRef.current = files;

  const refresh = useCallback(async () => {
    try {
      const r = await corpusApi.listFiles(corpus);
      setFiles(r.files);
      setStats(r.stats);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [corpus]);

  // Initial load + formats.
  useEffect(() => {
    corpusApi
      .listFormats()
      .then((r) => {
        setExtensions(r.extensions);
        setLargeBytes(r.large_file_bytes);
      })
      .catch(() => {
        /* non-fatal */
      });
    void refresh();
  }, [refresh]);

  // SSE subscription for per-file status updates.
  useEffect(() => {
    const close = corpusApi.subscribeEvents((ev) => {
      if (ev.type !== "file.status" || ev.corpus !== corpus) return;
      setFiles((prev) => {
        const idx = prev.findIndex((f) => f.file_id === ev.file_id);
        if (idx === -1) {
          // New file — bounce refresh.
          void refresh();
          return prev;
        }
        const next = prev.slice();
        next[idx] = {
          ...next[idx],
          status: ev.status as IngestionStatus,
          error: ev.error ?? null,
          chunk_count: ev.chunk_count,
          token_count: ev.token_count,
          progress: ev.progress,
        };
        return next;
      });
      // When something hits a terminal state, refresh stats from server.
      if (ev.status === "ready" || ev.status === "failed") {
        void refresh();
      }
    });
    return close;
  }, [corpus, refresh]);

  const uploadOnce = useCallback(
    async (
      toSend: File[],
      opts: { confirmLarge?: boolean; overwriteDuplicates?: boolean },
    ): Promise<UploadResponse> => {
      setBusy(true);
      setError(null);
      try {
        const resp = await corpusApi.uploadFiles(corpus, toSend, opts);
        return resp;
      } finally {
        setBusy(false);
      }
    },
    [corpus],
  );

  const handleFiles = useCallback(
    async (selected: File[]) => {
      if (selected.length === 0) return;
      if (selected.length > TOO_MANY_FILES) {
        const ok = window.confirm(
          `You're about to ingest ${selected.length} files. Continue?`,
        );
        if (!ok) return;
      }
      const large = selected.filter((f) => f.size > largeBytes);
      if (large.length > 0) {
        setPendingLarge(selected);
        return;
      }
      try {
        const r = await uploadOnce(selected, {});
        const dups = r.results.filter((it) => it.status === "duplicate");
        if (dups.length > 0) {
          const dupNames = new Set(dups.map((d) => d.filename));
          setDuplicates(dups.map((d) => d.filename));
          setDuplicateFiles(selected.filter((f) => dupNames.has(f.name)));
        }
        await refresh();
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [largeBytes, refresh, uploadOnce],
  );

  const confirmLarge = useCallback(async () => {
    if (!pendingLarge) return;
    const toSend = pendingLarge;
    setPendingLarge(null);
    try {
      const r = await uploadOnce(toSend, { confirmLarge: true });
      const dups = r.results.filter((it) => it.status === "duplicate");
      if (dups.length > 0) {
        const dupNames = new Set(dups.map((d) => d.filename));
        setDuplicates(dups.map((d) => d.filename));
        setDuplicateFiles(toSend.filter((f) => dupNames.has(f.name)));
      }
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }, [pendingLarge, refresh, uploadOnce]);

  const handleDelete = useCallback(
    async (fileId: string) => {
      try {
        await corpusApi.deleteFile(corpus, fileId);
        await refresh();
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [corpus, refresh],
  );

  const handleReingest = useCallback(
    async (fileId: string) => {
      try {
        await corpusApi.reingestFile(corpus, fileId);
        await refresh();
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [corpus, refresh],
  );

  const handleReingestAll = useCallback(async () => {
    try {
      await corpusApi.reingestAll(corpus);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }, [corpus, refresh]);

  const largeFileDescriptors = useMemo(
    () =>
      pendingLarge
        ? pendingLarge
            .filter((f) => f.size > largeBytes)
            .map((f) => ({ name: f.name, size: f.size }))
        : [],
    [pendingLarge, largeBytes],
  );

  return (
    <div className="corpus-zone">
      <FileUploadArea
        onFiles={handleFiles}
        supportedExtensions={extensions}
        disabled={busy}
      />
      {error ? <p className="error-note">{error}</p> : null}
      <div className="corpus-toolbar">
        <button type="button" onClick={() => void refresh()} disabled={busy}>
          Refresh
        </button>
        <button type="button" onClick={() => void handleReingestAll()} disabled={busy}>
          Re-ingest all
        </button>
      </div>
      <FileListTable
        files={files}
        onDelete={handleDelete}
        onReingest={handleReingest}
      />
      <CorpusStatsFooter stats={stats} />
      {pendingLarge ? (
        <FileSizeConfirmDialog
          files={largeFileDescriptors}
          onConfirm={() => void confirmLarge()}
          onCancel={() => setPendingLarge(null)}
        />
      ) : null}
      {duplicates ? (
        <DuplicateFilePrompt
          filenames={duplicates}
          onSkip={() => {
            setDuplicates(null);
            setDuplicateFiles([]);
          }}
          onReingest={async () => {
            const toResend = duplicateFiles;
            setDuplicates(null);
            setDuplicateFiles([]);
            if (toResend.length === 0) {
              await refresh();
              return;
            }
            try {
              await uploadOnce(toResend, { overwriteDuplicates: true });
            } catch (e) {
              setError((e as Error).message);
            }
            await refresh();
          }}
        />
      ) : null}
    </div>
  );
}
