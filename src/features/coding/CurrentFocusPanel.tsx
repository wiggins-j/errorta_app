// F137 — Current Focus goals panel. Replaces the F135 single "Current focus"
// textarea with a first-class, multi-item, ordered, lifecycle-managed list: the
// operative scope the PM plans against, with the North Star as a reference. Add,
// reorder (up/down), edit, drop (archive), and human-accept a PM-completed focus;
// archived focuses show as read-only history.
import { useCallback, useEffect, useRef, useState } from "react";

import * as api from "../../lib/api/coding";
import type { Focus } from "../../lib/api/coding";

export default function CurrentFocusPanel({
  projectId,
  running,
  onError,
  onChanged,
  phase,
  northStar,
}: {
  projectId: string;
  running?: boolean;
  onError: (msg: string) => void;
  onChanged?: () => void;
  // F141 WS-I: project phase. When "north_star" and there are no focuses yet,
  // the panel is hidden and the North Star shows as the active objective. When
  // omitted (undefined) the panel always shows (back-compat / non-gated hosts).
  phase?: string;
  northStar?: string;
}) {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const [current, setCurrent] = useState<Focus[]>([]);
  const [archived, setArchived] = useState<Focus[]>([]);
  const [title, setTitle] = useState("");
  const [adding, setAdding] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");

  const fail = useCallback(
    (err: unknown) => onError(err instanceof Error ? err.message : String(err)),
    [onError],
  );

  const reload = useCallback(async () => {
    try {
      const [a, completed, ar] = await Promise.all([
        api.listFocuses(projectId, "active"),
        api.listFocuses(projectId, "completed"),
        api.listFocuses(projectId, "archived"),
      ]);
      if (!mounted.current) return;
      setCurrent([...a, ...completed]);
      setArchived(ar);
    } catch (err) {
      fail(err);
    }
  }, [projectId, fail]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const add = async () => {
    const t = title.trim();
    if (!t) return;
    setAdding(true);
    try {
      await api.addFocus(projectId, t);
      if (!mounted.current) return;
      setTitle("");
      await reload();
      onChanged?.();
    } catch (err) {
      fail(err);
    } finally {
      if (mounted.current) setAdding(false);
    }
  };

  const move = async (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= active.length) return;
    const ids = active.map((f) => f.id);
    [ids[index], ids[target]] = [ids[target], ids[index]];
    try {
      const next = await api.reorderFocuses(projectId, ids);
      if (!mounted.current) return;
      setCurrent([...next, ...current.filter((focus) => focus.status === "completed")]);
      onChanged?.();
    } catch (err) {
      fail(err);
    }
  };

  const archive = async (id: string) => {
    try {
      await api.updateFocus(projectId, id, { status: "archived" });
      await reload();
      onChanged?.();
    } catch (err) {
      fail(err);
    }
  };

  const complete = async (id: string) => {
    try {
      await api.updateFocus(projectId, id, { status: "completed" });
      await reload();
      onChanged?.();
    } catch (err) {
      fail(err);
    }
  };

  const accept = async (id: string) => {
    try {
      await api.acceptFocus(projectId, id);
      await reload();
      onChanged?.();
    } catch (err) {
      fail(err);
    }
  };

  const saveEdit = async (id: string) => {
    const t = editTitle.trim();
    if (!t) return;
    try {
      await api.updateFocus(projectId, id, { title: t });
      if (!mounted.current) return;
      setEditingId(null);
      await reload();
      onChanged?.();
    } catch (err) {
      fail(err);
    }
  };

  const active = current.filter((focus) => focus.status === "active");

  // F141 WS-I: gate to the steering phase. A brand-new project (phase
  // "north_star", no focuses) hasn't reached its North Star yet, so it works
  // off the North Star and the Current Focus panel is hidden. Never hide a panel
  // that already holds focuses.
  const hasAnyFocus = current.length > 0 || archived.length > 0;
  const inNorthStarPhase = phase === "north_star";
  const showFocus = !inNorthStarPhase || hasAnyFocus;
  const [panelOpen, setPanelOpen] = useState(!inNorthStarPhase);
  // The pre-steering "Building toward" objective is a collapsible panel like the
  // rest of the sidebar; open by default so the North Star stays visible.
  const [objectiveOpen, setObjectiveOpen] = useState(true);

  // The view isn't remounted when a run flips phase north_star -> steering, so
  // the one-shot initializer above can leave the panel collapsed right when it
  // becomes the primary steering control. Auto-open it once on that transition
  // (a ref so a later manual collapse isn't undone; re-armed if it goes back).
  const autoOpenedRef = useRef(false);
  useEffect(() => {
    if (!inNorthStarPhase && !autoOpenedRef.current) {
      autoOpenedRef.current = true;
      setPanelOpen(true);
    } else if (inNorthStarPhase) {
      autoOpenedRef.current = false;
    }
  }, [inNorthStarPhase]);

  if (!showFocus) {
    return (
      <details
        className="coding-panel coding-objective"
        aria-label="Active objective"
        open={objectiveOpen}
        onToggle={(e) => setObjectiveOpen((e.target as HTMLDetailsElement).open)}
      >
        <summary>
          <span>Building toward</span>
        </summary>
        <section>
          <p className="coding-objective-ns">{northStar || "Your North Star"}</p>
        </section>
      </details>
    );
  }

  const topFocus = active[0];

  return (
    <details
      className="coding-panel coding-current-focus"
      aria-label="Current Focus"
      open={panelOpen}
      onToggle={(e) => setPanelOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary>
        <span>Current Focus</span>
        {active.length > 0 ? (
          <span className="coding-count" aria-label={`${active.length} active`}>
            {active.length}
          </span>
        ) : null}
        {topFocus ? (
          <span className="coding-focus-now">Now: {topFocus.title}</span>
        ) : null}
      </summary>
      <section className="coding-focus">
      <div className="coding-focus-head">
        <span className="coding-field-hint">
          What the team should build now. The North Star is a reference guardrail; these
          are the scope.
        </span>
      </div>

      {current.length === 0 ? (
        <p className="coding-focus-empty" role="status">
          No Current Focus yet. Add what you want the team to work on now.
        </p>
      ) : (
        <ul className="coding-focus-list">
          {current.map((f, i) => {
            const activeIndex = active.findIndex((focus) => focus.id === f.id);
            return <li key={f.id} className="coding-focus-item">
              <span className="coding-focus-order" aria-hidden="true">
                {i + 1}
              </span>
              {editingId === f.id ? (
                <input
                  className="coding-focus-edit"
                  value={editTitle}
                  aria-label={`Edit focus ${i + 1}`}
                  autoFocus
                  onChange={(e) => setEditTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void saveEdit(f.id);
                    if (e.key === "Escape") setEditingId(null);
                  }}
                />
              ) : (
                <span className="coding-focus-title">
                  {f.title}
                  {f.status === "completed" ? (
                    <span className="coding-focus-badge" role="status">
                      Ready for review
                    </span>
                  ) : null}
                </span>
              )}
              <span className="coding-focus-actions">
                {editingId === f.id ? (
                  <button type="button" className="coding-btn" onClick={() => void saveEdit(f.id)}>
                    Save
                  </button>
                ) : (
                  <>
                    {f.status === "active" ? (
                      <>
                        <button
                          type="button"
                          className="coding-btn coding-btn-icon"
                          aria-label={`Move focus ${i + 1} up`}
                          disabled={activeIndex === 0}
                          onClick={() => void move(activeIndex, -1)}
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          className="coding-btn coding-btn-icon"
                          aria-label={`Move focus ${i + 1} down`}
                          disabled={activeIndex === active.length - 1}
                          onClick={() => void move(activeIndex, 1)}
                        >
                          ↓
                        </button>
                      </>
                    ) : null}
                    <button
                      type="button"
                      className="coding-btn coding-btn-ghost"
                      onClick={() => {
                        setEditingId(f.id);
                        setEditTitle(f.title);
                      }}
                    >
                      Edit
                    </button>
                    {f.status === "completed" ? (
                      <button
                        type="button"
                        className="coding-btn coding-btn-primary"
                        disabled={running}
                        title={running ? "Stop the run to accept" : undefined}
                        onClick={() => void accept(f.id)}
                      >
                        Accept
                      </button>
                    ) : (
                      <>
                        <button
                          type="button"
                          className="coding-btn coding-btn-ghost"
                          onClick={() => void complete(f.id)}
                        >
                          Mark complete
                        </button>
                        <button
                          type="button"
                          className="coding-btn coding-btn-ghost"
                          onClick={() => void archive(f.id)}
                        >
                          Archive
                        </button>
                      </>
                    )}
                  </>
                )}
              </span>
            </li>;
          })}
        </ul>
      )}

      <div className="coding-focus-add">
        <input
          className="coding-focus-input"
          value={title}
          placeholder="Add a focus — e.g. Make the Council rooms panel collapsible"
          aria-label="New focus"
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void add();
          }}
        />
        <button
          type="button"
          className="coding-btn coding-btn-primary"
          onClick={() => void add()}
          disabled={adding || !title.trim()}
        >
          {adding ? "Adding…" : "Add focus"}
        </button>
      </div>

      {archived.length > 0 ? (
        <details
          className="coding-focus-archived"
          open={showArchived}
          onToggle={(e) => setShowArchived((e.target as HTMLDetailsElement).open)}
        >
          <summary>Archived focuses ({archived.length})</summary>
          <ul className="coding-focus-archived-list">
            {archived.map((f) => (
              <li key={f.id}>
                <span className="coding-focus-archived-title">{f.title}</span>
                {f.acceptedAt || f.archivedAt ? (
                  <span className="coding-field-hint">
                    {(f.acceptedAt || f.archivedAt).slice(0, 10)}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      </section>
    </details>
  );
}
