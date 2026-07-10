import { useEffect, useMemo, useRef, useState } from "react";
import QRCode from "qrcode";
import {
  getPairingStatus,
  startPairing,
  type PairingStart,
  type PairingStatus,
} from "../../lib/api/mobileConnector";

interface Props {
  open: boolean;
  onClose: () => void;
  onPaired: () => void;
}

function secondsRemaining(expiresAt: string): number {
  const expires = Date.parse(expiresAt);
  if (!Number.isFinite(expires)) return 0;
  return Math.max(0, Math.ceil((expires - Date.now()) / 1000));
}

function stepLabel(status: PairingStatus | null): string {
  if (!status) return "Waiting for phone";
  if (status.state === "awaiting_device") return "Waiting for phone";
  if (status.state === "awaiting_approval") return "Enter PIN on phone";
  if (status.state === "approved" || status.state === "consumed") return "Paired";
  if (status.state === "denied") return "Too many attempts";
  if (status.state === "expired") return "Code expired";
  return status.state;
}

export function PairPhoneModal({ open, onClose, onPaired }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [pairing, setPairing] = useState<PairingStart | null>(null);
  const [status, setStatus] = useState<PairingStatus | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const qrText = useMemo(() => {
    if (!pairing) return "";
    return JSON.stringify(pairing.pairingPayload);
  }, [pairing]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setPairing(null);
    setStatus(null);
    setError(null);
    startPairing()
      .then((next) => {
        if (cancelled) return;
        setPairing(next);
        setRemaining(secondsRemaining(next.expiresAt));
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [open, refreshKey]);

  useEffect(() => {
    if (!open || !pairing || !canvasRef.current || !qrText) return;
    QRCode.toCanvas(canvasRef.current, qrText, {
      errorCorrectionLevel: "H",
      margin: 1,
      width: 240,
    }).catch((err) => setError(String(err)));
  }, [open, pairing, qrText]);

  useEffect(() => {
    if (!open || !pairing) return;
    let cancelled = false;
    const activePairing = pairing;
    async function poll() {
      try {
        const next = await getPairingStatus(activePairing.sessionId);
        if (cancelled) return;
        setStatus(next);
        if (next.state === "consumed") {
          onPaired();
          onClose();
        }
      } catch (err) {
        if (!cancelled) setError(String(err));
      }
    }
    poll();
    const id = setInterval(poll, 1500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [open, pairing, onClose, onPaired]);

  useEffect(() => {
    if (!open || !pairing) return;
    setRemaining(secondsRemaining(pairing.expiresAt));
    const id = setInterval(() => {
      setRemaining(secondsRemaining(pairing.expiresAt));
    }, 1000);
    return () => clearInterval(id);
  }, [open, pairing]);

  if (!open) return null;

  const terminal =
    status?.state === "expired" ||
    status?.state === "denied" ||
    remaining <= 0;

  return (
    <div className="mobile-pairing-backdrop" role="presentation">
      <div
        className="mobile-pairing-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mobile-pairing-title"
      >
        <div className="mobile-pairing-header">
          <div>
            <h3 id="mobile-pairing-title">Pair iPhone</h3>
            <p className="shell-muted">
              Scan with Errorta, then enter the PIN on your phone.
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close pairing">
            Close
          </button>
        </div>

        {error ? <p className="shell-error">{error}</p> : null}

        {pairing ? (
          <div className="mobile-pairing-content">
            <div className="mobile-qr-frame">
              <canvas ref={canvasRef} aria-label="Mobile pairing QR code" />
            </div>
            <div className="mobile-pairing-details">
              <span className="shell-muted">PIN</span>
              <strong className="mobile-pairing-pin">{pairing.pin ?? "Manual approval"}</strong>
              <span className="shell-muted">
                Expires in {remaining}s
              </span>
              <ol className="mobile-pairing-steps" aria-label="Pairing progress">
                <li data-active={status == null || status.state === "awaiting_device"}>
                  Waiting for phone
                </li>
                <li data-active={status?.state === "awaiting_approval"}>
                  Enter PIN on phone
                </li>
                <li data-active={status?.state === "approved" || status?.state === "consumed"}>
                  Paired
                </li>
              </ol>
              {status?.deviceDraft ? (
                <p className="shell-muted">
                  Connected: {status.deviceDraft.display_name}
                </p>
              ) : null}
              <p className={terminal ? "shell-error" : "shell-muted"}>
                {stepLabel(status)}
              </p>
              {terminal ? (
                <button type="button" onClick={() => setRefreshKey((v) => v + 1)}>
                  New code
                </button>
              ) : null}
            </div>
          </div>
        ) : (
          <p className="shell-muted">Preparing pairing code...</p>
        )}
      </div>
    </div>
  );
}

export default PairPhoneModal;
