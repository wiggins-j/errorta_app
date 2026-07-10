// F001-polish — empty state stub used by metrics + future surfaces.

interface Props {
  title?: string;
  message: string;
}

export default function EmptyState({ title, message }: Props) {
  return (
    <div className="empty-state" role="status">
      {title && <div className="empty-state-title">{title}</div>}
      <div className="empty-state-message">{message}</div>
    </div>
  );
}
