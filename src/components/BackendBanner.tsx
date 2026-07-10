// F069 — a slim, non-blocking banner shown when the backend is not ready inside
// an already-open shell (post-ready backend loss, or F103 limited mode). The
// cold-launch wait is now handled by the full-window StartupSplash; this banner
// only covers the in-shell degraded state. F103 — uses a static status dot, not
// a circular spinner.
export function BackendBanner({ ready }: { ready: boolean }) {
  if (ready) return null;
  return (
    <div className="backend-banner" role="status" aria-live="polite">
      <span className="backend-banner-dot" aria-hidden="true" />
      <span className="backend-banner-text">
        Local backend unavailable — backend-dependent features stay disabled
        until it reconnects.
      </span>
    </div>
  );
}
