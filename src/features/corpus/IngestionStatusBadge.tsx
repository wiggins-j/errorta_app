import type { IngestionStatus } from "./types";
import ProgressSpinner from "./ProgressSpinner";

const LABELS: Record<IngestionStatus, string> = {
  queued: "Queued",
  extracting: "Extracting text",
  chunking: "Chunking",
  embedding: "Embedding",
  ready: "Ready",
  failed: "Failed",
};

export default function IngestionStatusBadge({
  status,
  progress,
  error,
}: {
  status: IngestionStatus;
  progress?: number;
  error?: string | null;
}) {
  if (status === "ready") return <span className="badge badge-ready">Ready</span>;
  if (status === "failed")
    return (
      <span className="badge badge-failed" title={error ?? undefined}>
        Failed: {error ?? "unknown"}
      </span>
    );
  return (
    <span className="badge badge-progress">
      {LABELS[status]} <ProgressSpinner progress={progress} />
    </span>
  );
}
