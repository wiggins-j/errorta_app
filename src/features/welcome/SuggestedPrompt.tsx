// F007 — pre-filled prompt rendered after ingest.
// F109 — Run deep-links to the Judge feature with the prompt prefilled (the
// judge IS the run surface; there is no separate Simulate tab).
import { useState } from "react";

interface Props {
  prompt: string;
  corpusName: string;
}

export default function SuggestedPrompt({ prompt, corpusName }: Props) {
  const [value, setValue] = useState(prompt);

  const handleRun = () => {
    // Navigate to the Judge with this prompt prefilled. The judge consumes the
    // prompt one-shot on mount; it does NOT auto-run (no surprise model call).
    window.dispatchEvent(
      new CustomEvent("errorta:navigate", {
        detail: { view: "judge", prompt: value },
      }),
    );
  };

  return (
    <div className="welcome-suggested">
      <p className="welcome-suggested-eyebrow">
        Corpus <code>{corpusName}</code> is ready. Suggested prompt:
      </p>
      <textarea
        className="welcome-suggested-input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        rows={3}
      />
      <div className="welcome-suggested-actions">
        <button
          type="button"
          className="welcome-cta-primary"
          onClick={handleRun}
          disabled={value.trim().length === 0}
        >
          Run in Judge
        </button>
        <button
          type="button"
          className="welcome-cta-secondary"
          onClick={() => setValue("")}
        >
          Clear
        </button>
      </div>
    </div>
  );
}
