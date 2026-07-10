// F005 — small badge marking a file as watched-source vs. directly uploaded.
import type { FileSource } from "./types";

interface Props {
  source: FileSource;
}

export function FileSourceBadge({ source }: Props) {
  const watched = source === "watched";
  return (
    <span
      title={watched ? "Auto-ingested from watched folder" : "Uploaded directly"}
      style={{
        display: "inline-block",
        padding: "1px 6px",
        fontSize: 11,
        borderRadius: 4,
        border: "1px solid",
        borderColor: watched ? "var(--accent)" : "var(--border-strong)",
        color: watched ? "var(--text)" : "var(--text-muted)",
        background: watched ? "var(--accent-soft)" : "transparent",
      }}
    >
      {watched ? "watched" : "uploaded"}
    </span>
  );
}
