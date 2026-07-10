// F031 Phase 2 — Prompt composer + Run/Stop buttons.
//
// F031-DEMO-CORPUS Task 5: composer can now run controlled when the
// shell needs to push a pre-baked prompt into the textarea (e.g. the
// "Try the demo prompt" button). The default uncontrolled behavior is
// preserved when `value` / `onChange` are not supplied.
import { useState } from "react";
import type { CouncilUiRunState } from "./types";

interface Props {
  disabled: boolean;
  onRun: (prompt: string, options?: { dryFakeMembers?: boolean }) => void;
  onCancel: () => void;
  runState?: CouncilUiRunState;
  /** Optional controlled value. When set, the parent manages the prompt text. */
  value?: string;
  /** Required when `value` is supplied. */
  onChange?: (next: string) => void;
  /**
   * F049: send a live message into the running run. When provided, the
   * composer stays usable WHILE a run is live and a "Send" button submits the
   * text as a user interjection that the next member picks up.
   */
  onInterject?: (text: string) => void;
}

const RUNNING_STATES: ReadonlySet<CouncilUiRunState> = new Set([
  "running",
  "paused",
  "finalizing",
  "cancelling",
  "submitting",
]);

export default function CouncilPromptComposer({
  disabled,
  onRun,
  onCancel,
  runState,
  value,
  onChange,
  onInterject,
}: Props) {
  const [localPrompt, setLocalPrompt] = useState("");
  const isControlled = value !== undefined;
  const prompt = isControlled ? value! : localPrompt;
  const setPrompt = (next: string) => {
    if (isControlled) {
      onChange?.(next);
    } else {
      setLocalPrompt(next);
    }
  };
  const [dryFake, setDryFake] = useState(false);
  const isRunning = runState !== undefined && RUNNING_STATES.has(runState);
  // Fake-run is a dev/test affordance. Hide it in production builds so the
  // checkbox can never silently route a real run to deterministic stubs
  // (invariant 10: fake members are first-class but never default in prod).
  const isProd = (import.meta.env as { PROD?: boolean }).PROD === true;
  const showFakeToggle = !isProd;

  const handleSubmit = () => {
    const trimmed = prompt.trim();
    if (!trimmed) return;
    onRun(trimmed, { dryFakeMembers: showFakeToggle && dryFake });
  };

  const handleInterject = () => {
    const trimmed = prompt.trim();
    if (!trimmed || !onInterject) return;
    onInterject(trimmed);
    setPrompt("");
  };

  // F049: while a run is live AND the parent wired an interjection handler, the
  // composer becomes a live chat box — the user can message the council and the
  // next member picks it up.
  const canInterject = isRunning && onInterject !== undefined;
  const textareaDisabled = disabled || (isRunning && !canInterject);

  return (
    <div className="council-composer">
      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder={canInterject ? "Message the council…" : "Prompt for the Council…"}
        aria-label={canInterject ? "Message the council" : "Council prompt"}
        disabled={textareaDisabled}
        rows={2}
        data-testid="council-prompt-textarea"
      />
      <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
        {showFakeToggle && !isRunning && (
          <label style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
            <input
              type="checkbox"
              checked={dryFake}
              onChange={(e) => setDryFake(e.target.checked)}
            />{" "}
            Fake-run
          </label>
        )}
        {isRunning ? (
          <>
            {canInterject && (
              <button
                type="button"
                disabled={prompt.trim() === ""}
                onClick={handleInterject}
                data-testid="council-interject-send"
              >
                Send
              </button>
            )}
            <button type="button" onClick={onCancel}>
              Stop
            </button>
          </>
        ) : (
          <button
            type="button"
            disabled={disabled || prompt.trim() === ""}
            onClick={handleSubmit}
          >
            Run
          </button>
        )}
      </div>
    </div>
  );
}
