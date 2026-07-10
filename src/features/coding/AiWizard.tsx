// F145 — the AI Wizard: a conversation that produces a fully runnable project.
import { useEffect, useRef, useState } from "react";

import * as api from "../../lib/api/coding";

type Phase = "pick" | "chat" | "review";
type ChatMsg = { role: "user" | "pm"; text: string };

// providers that bill per token / are generally strongest — nudge toward these.
const STRONGER = new Set(["anthropic", "openai", "google"]);

export default function AiWizard({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (projectId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase>("pick");
  const [models, setModels] = useState<api.WizardModel[]>([]);
  const [model, setModel] = useState<string>("");
  const [sessionId, setSessionId] = useState<string>("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [ready, setReady] = useState(false);
  const [charter, setCharter] = useState<Record<string, unknown>>({});
  const [projectId, setProjectId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .getWizardModels()
      .then((m) => {
        setModels(m);
        if (m.length && !model) setModel(m[0].routeId);
      })
      .catch(() => setError("Couldn't load available models."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.wizardStart(model);
      setSessionId(r.sessionId);
      setMessages([{ role: "pm", text: r.reply }]);
      setPhase("chat");
    } catch {
      setError("Couldn't start the wizard with that model.");
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text }]);
    setBusy(true);
    setError(null);
    try {
      const r = await api.wizardMessage(sessionId, text);
      setMessages((m) => [...m, { role: "pm", text: r.reply }]);
      setReady(r.ready);
      setCharter(r.charter);
      if (r.ready && !projectId) {
        setProjectId(String((r.charter.north_star as string) || "").toLowerCase()
          .replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "").slice(0, 40) || "my-project");
      }
    } catch {
      setError("The model couldn't be reached — try again.");
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.wizardCreate(sessionId, projectId.trim());
      if (r.warnings.length) {
        // still created — surface the warning but proceed
        setError(r.warnings.join(" "));
      }
      onCreated(r.projectId);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't create the project.");
    } finally {
      setBusy(false);
    }
  }

  const chosen = models.find((m) => m.routeId === model);
  const strongerAvailable = models.some((m) => STRONGER.has(m.providerClass));

  return (
    <div className="coding-modal-backdrop" role="dialog" aria-label="AI Wizard" aria-modal="true">
      <div className="coding-modal coding-wizard">
        <header className="coding-wizard-head">
          <h2>AI Wizard — let's talk about the project</h2>
          <button type="button" className="coding-btn coding-btn-ghost" onClick={onClose}>
            Close
          </button>
        </header>

        {error ? <p className="coding-field-error" role="alert">{error}</p> : null}

        {phase === "pick" && (
          <div className="coding-wizard-pick">
            <label className="coding-field-label" htmlFor="wiz-model">
              Model for the wizard
              <span className="coding-field-hint">
                Pick a stronger model — this conversation sets up your whole project.
              </span>
            </label>
            <select
              id="wiz-model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              aria-label="Wizard model"
            >
              {models.map((m) => (
                <option key={m.routeId} value={m.routeId}>
                  {m.routeId} {STRONGER.has(m.providerClass) ? "★ recommended" : ""}
                </option>
              ))}
            </select>
            {models.length === 0 ? (
              <p className="coding-field-hint">No models are connected yet — connect a provider first.</p>
            ) : null}
            {chosen && !STRONGER.has(chosen.providerClass) && strongerAvailable ? (
              <p className="coding-wizard-nudge" role="note">
                Tip: a stronger model (★) will set your project up more reliably.
              </p>
            ) : null}
            <div className="coding-create-footer">
              <button type="button" className="coding-btn" onClick={start} disabled={busy || !model}>
                Start
              </button>
            </div>
          </div>
        )}

        {phase === "chat" && (
          <div className="coding-wizard-chat">
            <div className="coding-wizard-thread">
              {messages.map((m, i) => (
                <div key={i} className={`coding-wizard-turn coding-wizard-${m.role}`}>
                  <strong>{m.role === "user" ? "You" : "PM"}</strong>
                  <p>{m.text}</p>
                </div>
              ))}
              <div ref={endRef} />
            </div>
            <div className="coding-wizard-compose">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
                }}
                placeholder="Describe your project… (⌘/Ctrl+Enter to send)"
                aria-label="Message to the PM"
                rows={2}
              />
              <button type="button" className="coding-btn" onClick={send} disabled={busy || !input.trim()}>
                Send
              </button>
            </div>
            {ready ? (
              <div className="coding-create-footer">
                <button type="button" className="coding-btn" onClick={() => setPhase("review")}>
                  Review
                </button>
              </div>
            ) : (
              <p className="coding-field-hint">
                I'll enable "Review" once I have a runnable plan.
              </p>
            )}
          </div>
        )}

        {phase === "review" && (
          <div className="coding-wizard-review">
            <h3>PM Changes — create this project</h3>
            <dl className="coding-wizard-charter">
              <dt>North Star</dt><dd>{String(charter.north_star ?? "")}</dd>
              <dt>Definition of Done</dt><dd>{String(charter.definition_of_done ?? "")}</dd>
              <dt>Modality</dt><dd>{String(charter.modality ?? "")}</dd>
              <dt>Entrypoint</dt><dd>{String(charter.entrypoint ?? "")}</dd>
              <dt>Team</dt><dd>{String(charter.team_recipe ?? "balanced")}</dd>
              <dt>Autonomous</dt><dd>{charter.autonomous ? "yes — runs without asking" : "no"}</dd>
            </dl>
            {charter.autonomous ? (
              <p className="coding-wizard-nudge" role="note">
                This will run <strong>autonomously</strong> — it won't pause to ask you. You own the outcome.
              </p>
            ) : null}
            <label className="coding-field-label" htmlFor="wiz-pid">Project ID</label>
            <input
              id="wiz-pid"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              aria-label="Project id"
            />
            <div className="coding-create-footer">
              <button type="button" className="coding-btn coding-btn-ghost" onClick={() => setPhase("chat")}>
                Back
              </button>
              <button type="button" className="coding-btn" onClick={create} disabled={busy || !projectId.trim()}>
                Create
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
