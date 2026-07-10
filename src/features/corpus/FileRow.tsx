import DeleteFileButton from "./DeleteFileButton";
import IngestionStatusBadge from "./IngestionStatusBadge";
import ReIngestFileButton from "./ReIngestFileButton";
import type { CorpusFile } from "./types";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export default function FileRow({
  file,
  onDelete,
  onReingest,
}: {
  file: CorpusFile;
  onDelete: (fileId: string) => Promise<void> | void;
  onReingest: (fileId: string) => Promise<void> | void;
}) {
  return (
    <tr>
      <td>{file.original_path}</td>
      <td>{formatBytes(file.size_bytes)}</td>
      <td>
        <IngestionStatusBadge
          status={file.status}
          progress={file.progress}
          error={file.error}
        />
      </td>
      <td>{file.chunk_count}</td>
      <td>
        <ReIngestFileButton onReingest={() => onReingest(file.file_id)} />{" "}
        <DeleteFileButton onDelete={() => onDelete(file.file_id)} />
      </td>
    </tr>
  );
}
