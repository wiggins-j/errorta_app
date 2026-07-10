export default function ProgressSpinner({ progress }: { progress?: number }) {
  if (typeof progress === "number" && progress > 0 && progress < 1) {
    return <span aria-label="progress">{Math.round(progress * 100)}%</span>;
  }
  return <span aria-label="loading">…</span>;
}
