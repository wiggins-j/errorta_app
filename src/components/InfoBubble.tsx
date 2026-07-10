import { useId, useState } from "react";

// A small "ⓘ" affordance that reveals a plain-language explanation on hover,
// focus, or click. CSS handles hover/focus; click toggles a pinned state so
// touch and keyboard users (and anyone who wants to read without holding the
// pointer still) can keep it open. Accessible: the icon is a real button with
// aria-label, and the tip is associated via aria-describedby.
export default function InfoBubble({ label, text }: { label: string; text: string }) {
  const [pinned, setPinned] = useState(false);
  const id = useId();
  return (
    <span className="info-bubble">
      <button
        type="button"
        className={`info-bubble-icon${pinned ? " info-bubble-pinned" : ""}`}
        aria-label={`What is ${label}?`}
        aria-expanded={pinned}
        aria-describedby={id}
        onClick={() => setPinned((p) => !p)}
        onBlur={() => setPinned(false)}
      >
        i
      </button>
      <span
        id={id}
        role="tooltip"
        className={`info-bubble-tip${pinned ? " info-bubble-tip-pinned" : ""}`}
      >
        {text}
      </span>
    </span>
  );
}
