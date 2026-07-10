// F-DIST-01 slice 7 — "Send feedback" with a mandatory show-before-send review.
// The tester writes a message, we build the F-INFRA-06 *redacted* bundle, show
// them EXACTLY what's in it (files + what was scrubbed), and only send on an
// explicit confirm. Reachable when locked/unactivated (feedback needs no license).

import { useState } from "react";
import {
  previewFeedback,
  submitFeedback,
  type FeedbackKind,
  type FeedbackPreview,
} from "../../lib/api/alpha";
import "./alphaFeedback.css";

type Step = "compose" | "review" | "sent";

export interface SendFeedbackProps {
  /** Optional heading override (e.g. on the lock screen). */
  title?: string;
}

export default function SendFeedback({ title = "Send feedback" }: SendFeedbackProps) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<Step>("compose");
  const [kind, setKind] = useState<FeedbackKind>("bug");
  const [message, setMessage] = useState("");
  const [preview, setPreview] = useState<FeedbackPreview | null>(null);
  const [ticketId, setTicketId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setStep("compose");
    setMessage("");
    setKind("bug");
    setPreview(null);
    setTicketId(null);
    setError(null);
    setBusy(false);
  }

  async function onPrepare() {
    setBusy(true);
    setError(null);
    try {
      setPreview(await previewFeedback(kind, message.trim()));
      setStep("review");
    } catch {
      setError("Couldn't prepare the report. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function onSend() {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      setTicketId(await submitFeedback(preview.preparedId));
      setStep("sent");
    } catch {
      setError("Couldn't send the report. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        className="alpha-fb-open"
        onClick={() => {
          reset();
          setOpen(true);
        }}
      >
        {title}
      </button>
    );
  }

  return (
    <section className="alpha-fb" aria-labelledby="alpha-fb-title">
      <h3 id="alpha-fb-title">{title}</h3>

      {step === "compose" && (
        <div className="alpha-fb-body">
          <label className="alpha-fb-label" htmlFor="alpha-fb-kind">
            Type
          </label>
          <select
            id="alpha-fb-kind"
            className="alpha-fb-select"
            value={kind}
            onChange={(e) => setKind(e.target.value as FeedbackKind)}
          >
            <option value="bug">Bug</option>
            <option value="suggestion">Suggestion</option>
            <option value="crash">Crash</option>
          </select>

          <label className="alpha-fb-label" htmlFor="alpha-fb-msg">
            What happened?
          </label>
          <textarea
            id="alpha-fb-msg"
            className="alpha-fb-textarea"
            rows={4}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Describe what you were doing…"
          />
          <p className="alpha-fb-note">
            We'll attach a diagnostic bundle — with your file names, corpus contents, prompts, and
            keys removed — and show it to you before anything is sent.
          </p>
        </div>
      )}

      {step === "review" && preview && (
        <div className="alpha-fb-body">
          <p className="alpha-fb-review-lead">This is exactly what will be sent:</p>
          <dl className="alpha-fb-review">
            <dt>Your message</dt>
            <dd>{preview.message || <em>(none)</em>}</dd>
            <dt>Attached bundle</dt>
            <dd>
              {preview.bundle.files.length} file{preview.bundle.files.length === 1 ? "" : "s"}
              {preview.bundle.sha256 ? ` · sha256 ${preview.bundle.sha256.slice(0, 12)}…` : ""}
            </dd>
            {preview.bundle.files.length > 0 && (
              <>
                <dt>Files</dt>
                <dd>
                  <ul className="alpha-fb-files">
                    {preview.bundle.files.map((f) => (
                      <li key={f}>
                        <code>{f}</code>
                      </li>
                    ))}
                  </ul>
                </dd>
              </>
            )}
            {Object.keys(preview.bundle.redaction).length > 0 && (
              <>
                <dt>Scrubbed before sending</dt>
                <dd>
                  {Object.entries(preview.bundle.redaction)
                    .filter(([, n]) => n > 0)
                    .map(([k, n]) => `${k}: ${n}`)
                    .join(" · ") || "nothing sensitive found"}
                </dd>
              </>
            )}
          </dl>
        </div>
      )}

      {step === "sent" && (
        <div className="alpha-fb-body">
          <p className="alpha-fb-thanks">Thanks — your report is in.</p>
          {ticketId && (
            <p className="alpha-fb-ticket">
              Reference: <code>{ticketId}</code>
            </p>
          )}
        </div>
      )}

      {error && (
        <p className="alpha-fb-error" role="alert">
          {error}
        </p>
      )}

      <div className="alpha-fb-actions">
        {step === "compose" && (
          <button
            type="button"
            className="alpha-fb-btn primary"
            disabled={busy || !message.trim()}
            onClick={onPrepare}
          >
            {busy ? "Preparing…" : "Review before sending"}
          </button>
        )}
        {step === "review" && (
          <>
            <button
              type="button"
              className="alpha-fb-btn primary"
              disabled={busy}
              onClick={onSend}
            >
              {busy ? "Sending…" : "Send it"}
            </button>
            <button
              type="button"
              className="alpha-fb-btn"
              disabled={busy}
              onClick={() => setStep("compose")}
            >
              Back
            </button>
          </>
        )}
        <button type="button" className="alpha-fb-btn" onClick={() => setOpen(false)}>
          {step === "sent" ? "Done" : "Cancel"}
        </button>
      </div>
    </section>
  );
}
