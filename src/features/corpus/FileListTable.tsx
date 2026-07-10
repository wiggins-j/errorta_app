import { memo } from "react";
import FileRow from "./FileRow";
import type { CorpusFile } from "./types";

function FileListTableImpl({
  files,
  onDelete,
  onReingest,
}: {
  files: CorpusFile[];
  onDelete: (fileId: string) => Promise<void> | void;
  onReingest: (fileId: string) => Promise<void> | void;
}) {
  if (files.length === 0) {
    return <p className="empty-note">No files yet — drop some above to get started.</p>;
  }
  return (
    <table className="file-list">
      <thead>
        <tr>
          <th>File</th>
          <th>Size</th>
          <th>Status</th>
          <th>Chunks</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {files.map((f) => (
          <FileRow
            key={f.file_id}
            file={f}
            onDelete={onDelete}
            onReingest={onReingest}
          />
        ))}
      </tbody>
    </table>
  );
}

// Memoize so the SSE event firehose doesn't re-render every row on every tick.
export default memo(FileListTableImpl);
