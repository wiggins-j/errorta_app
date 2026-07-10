// F002 — full-screen scanning splash shown while POST /hardware/scan is in flight.

export interface HardwareScanModalProps {
  visible: boolean;
}

export function HardwareScanModal({ visible }: HardwareScanModalProps) {
  if (!visible) return null;
  return (
    <div className="feature-pane" role="status" aria-live="polite">
      <h2>Scanning your hardware...</h2>
      <p className="feature-pane-note">
        Detecting GPU, RAM, CPU, disk, and OS. This should take just a few seconds.
      </p>
    </div>
  );
}
