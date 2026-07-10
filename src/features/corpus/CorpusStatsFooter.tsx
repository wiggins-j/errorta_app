import type { CorpusStats } from "./types";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export default function CorpusStatsFooter({ stats }: { stats: CorpusStats }) {
  return (
    <footer className="corpus-stats">
      <span>{stats.file_count} files</span>
      <span>{stats.chunk_count.toLocaleString()} chunks</span>
      <span>~{stats.token_count.toLocaleString()} tokens</span>
      <span>{formatBytes(stats.disk_bytes)} on disk</span>
    </footer>
  );
}
