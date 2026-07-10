// F031 Phase 2/5 — three-region Council shell.
// Left: room list (with seed-demo affordance when empty).
// Center: transcript + composer + run controls + status banner.
// Right: audit summary.
// Phase 5: clicking a context_built event opens the ContextInspectionDrawer.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelRun,
  createRun,
  getMobileActivity,
  getRoomMetadata,
  getRun,
  getRunAuditSummary,
  injectMessage,
  listRooms,
  listRuns,
  pauseRun,
  resumeRun,
} from "../../lib/api/council";
import { getRoomFull } from "../../lib/api/councilRoom";
import CouncilWorkRail from "./work/CouncilWorkRail";
import { roomUsesWorkRail } from "./work/roomUsesWorkRail";
import type {
  CouncilRoomSummary,
  CouncilRunAuditSummary,
  CouncilRunStatus,
  CouncilTranscriptEvent,
} from "./types";
import CouncilRoomList from "../rooms/CouncilRoomList";
import CouncilTranscript from "./CouncilTranscript";
import CouncilCalloutPanel from "./CouncilCalloutPanel";
import CouncilPromptComposer from "./CouncilPromptComposer";
import CouncilRunStatusBanner from "./CouncilRunStatusBanner";
import CouncilRunControls from "./CouncilRunControls";
import ContextAuditSummary from "./ContextAuditSummary";
import ContextInspectionDrawer from "./ContextInspectionDrawer";
import AgentContextInspector from "./AgentContextInspector";
import AiarReadinessBanner from "./AiarReadinessBanner";
import ApplyWorkspacePanel from "./ApplyWorkspacePanel";
import { DEMO_ROOM_MARKER, DEMO_PROMPT } from "../rooms/CouncilDemoRoomSeed";

const AUDIT_COLLAPSED_KEY = "errorta.council.auditCollapsed";

interface InspectionTarget {
  runId: string;
  // QA P1 #1: Inspect opens the drawer at the ROUND level so the
  // compare strip is reachable (per-turn always returns one manifest).
  round: number;
  memberId?: string;
  // Retained for the drawer's subtitle and accessibility labels.
  turnId: string;
}

export default function CouncilShell() {
  const [rooms, setRooms] = useState<CouncilRoomSummary[]>([]);
  const [selectedRoomId, setSelectedRoomId] = useState<string | null>(null);
  const [run, setRun] = useState<CouncilRunStatus | null>(null);
  const [activePrompt, setActivePrompt] = useState<string>("");
  const [events, setEvents] = useState<CouncilTranscriptEvent[]>([]);
  const [audit, setAudit] = useState<CouncilRunAuditSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inspection, setInspection] = useState<InspectionTarget | null>(null);
  const [workRailRoom, setWorkRailRoom] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [isDemoRoom, setIsDemoRoom] = useState(false);
  const [composerDraftsByRoom, setComposerDraftsByRoom] = useState<
    Record<string, string>
  >({});
  // Collapse the right-hand Audit pane so the Transcript spans the full width.
  const [auditCollapsed, setAuditCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(AUDIT_COLLAPSED_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggleAudit = useCallback(() => {
    setAuditCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(AUDIT_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // ignore storage failures (private mode / test env)
      }
      return next;
    });
  }, []);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const composerInput = selectedRoomId
    ? composerDraftsByRoom[selectedRoomId] ?? ""
    : "";
  const setSelectedRoomDraft = useCallback((next: string) => {
    if (!selectedRoomId) return;
    setComposerDraftsByRoom((prev) => ({ ...prev, [selectedRoomId]: next }));
  }, [selectedRoomId]);

  const refreshRooms = useCallback(async () => {
    try {
      const r = await listRooms();
      setRooms(r);
      if (r.length > 0 && selectedRoomId === null) {
        setSelectedRoomId(r[0].id);
      }
    } catch (err) {
      setError(
        `council_api_unreachable: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }, [selectedRoomId]);

  // Room MANAGEMENT (create/edit/delete/seed/import) now lives in the Rooms
  // tab. Council only selects an existing room to run. The list's
  // "Manage rooms →" affordance deep-links there via the shared navigate event
  // (App.tsx listens for { view: "rooms" }).
  const goToRooms = useCallback(() => {
    window.dispatchEvent(
      new CustomEvent("errorta:navigate", { detail: { view: "rooms" } }),
    );
  }, []);

  // Rooms are now created/edited/deleted in the Rooms tab. Re-pull the shared
  // room list when this view regains focus so changes made there show up here
  // without a full remount.
  useEffect(() => {
    const onFocus = () => {
      void refreshRooms();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refreshRooms]);

  // Initial rooms load.
  useEffect(() => {
    let cancelled = false;
    listRooms()
      .then((r) => {
        if (!cancelled) {
          setRooms(r);
          if (r.length > 0 && selectedRoomId === null) {
            setSelectedRoomId(r[0].id);
          }
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(`council_api_unreachable: ${err?.message ?? err}`);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load the latest run for the selected room. This keeps the visible prompt,
  // transcript, status, audit, and run controls scoped to the room instead of
  // leaking the last active run into newly created or newly selected rooms.
  useEffect(() => {
    let cancelled = false;
    if (!selectedRoomId) {
      setRun(null);
      setActivePrompt("");
      setEvents([]);
      setAudit(null);
      return;
    }
    setRun(null);
    setActivePrompt("");
    setEvents([]);
    setAudit(null);
    listRuns({ roomId: selectedRoomId, limit: 1 })
      .then(async (summaries) => {
        if (cancelled || summaries.length === 0) return;
        const fresh = await getRun(summaries[0].runId);
        if (cancelled) return;
        if (fresh.run.roomId && fresh.run.roomId !== selectedRoomId) return;
        setRun(fresh.run);
        setActivePrompt(fresh.run.prompt ?? "");
        setEvents(fresh.events);
        getRunAuditSummary(fresh.run.runId)
          .then((a) => {
            if (!cancelled) setAudit(a);
          })
          .catch(() => undefined);
      })
      .catch(() => {
        if (!cancelled) {
          setRun(null);
          setActivePrompt("");
          setEvents([]);
          setAudit(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRoomId]);

  // Track the active room's stored corpus_ids so we forward them to
  // createRun. QA P1 #2 lock: without this, the seeded demo room's
  // corpus_ids silently drop on every run.
  const [activeRoomCorpusIds, setActiveRoomCorpusIds] = useState<string[]>([]);

  // Fetch room metadata when active room changes. Detect the demo room
  // and capture the room-level corpus_ids.
  useEffect(() => {
    let cancelled = false;
    if (!selectedRoomId) {
      setIsDemoRoom(false);
      setActiveRoomCorpusIds([]);
      return;
    }
    getRoomMetadata(selectedRoomId)
      .then((m) => {
        if (cancelled) return;
        setIsDemoRoom(m?.demo_marker === DEMO_ROOM_MARKER);
        // `corpus_ids` lives on the room body's `_extras` passthrough,
        // not on `metadata`. Pull from both for forward-compat.
        const fromMeta = (m as Record<string, unknown> | null)?.corpus_ids;
        if (Array.isArray(fromMeta)) {
          setActiveRoomCorpusIds(
            fromMeta.filter((v): v is string => typeof v === "string"),
          );
        } else {
          setActiveRoomCorpusIds([]);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setIsDemoRoom(false);
          setActiveRoomCorpusIds([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRoomId]);

  // F046 — the work rail only appears for tool/policy-gated rooms. Load the
  // full room to decide; a plain Council room shows no rail.
  useEffect(() => {
    let cancelled = false;
    if (!selectedRoomId) {
      setWorkRailRoom(null);
      return;
    }
    getRoomFull(selectedRoomId)
      .then((resp) => {
        if (!cancelled) setWorkRailRoom(resp.room);
      })
      .catch(() => {
        if (!cancelled) setWorkRailRoom(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRoomId]);

  // F084: member ids configured as designated steelman advocates, so the
  // transcript can badge their turns "Steelman · unverified".
  const steelmanMemberIds = useMemo(() => {
    const members = Array.isArray(workRailRoom?.members) ? workRailRoom!.members : [];
    return (members as Array<Record<string, unknown>>)
      .filter((m) => Boolean((m?.metadata as Record<string, unknown> | undefined)?.steelman))
      .map((m) => String(m.id ?? ""))
      .filter(Boolean);
  }, [workRailRoom]);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  // Poll events + audit while a run is non-terminal.
  useEffect(() => {
    if (!run) return;
    if (["done", "failed", "cancelled"].includes(run.state)) {
      stopPolling();
      return;
    }
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const fresh = await getRun(run.runId);
        setRun(fresh.run);
        setEvents(fresh.events);
        const a = await getRunAuditSummary(run.runId).catch(() => null);
        if (a) setAudit(a);
      } catch (err) {
        setError(`event_stream_disconnected: ${String(err)}`);
      }
    }, 500);
    return stopPolling;
  }, [run, stopPolling]);

  // F074 — auto-surface a run the phone just touched (started or messaged), so
  // it "pops up" on the desktop without hunting for it. Polls a monotonic seq;
  // seeds a baseline on first read so only NEW phone activity opens a run.
  const surfacedSeqRef = useRef<number>(-1);
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const act = await getMobileActivity();
        if (cancelled) return;
        if (surfacedSeqRef.current < 0) {
          surfacedSeqRef.current = act.seq; // baseline — don't surface old activity
          return;
        }
        if (act.seq <= surfacedSeqRef.current) return;
        surfacedSeqRef.current = act.seq;
        if (act.runId && act.runId !== run?.runId) {
          const fresh = await getRun(act.runId);
          if (cancelled) return;
          if (fresh.run.roomId) setSelectedRoomId(fresh.run.roomId);
          setRun(fresh.run);
          setActivePrompt(fresh.run.prompt ?? "");
          setEvents(fresh.events);
        }
      } catch {
        /* connector off / transient — ignore */
      }
    };
    void tick();
    const id = setInterval(tick, 2000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [run?.runId]);

  const onRun = useCallback(
    async (prompt: string, options?: { dryFakeMembers?: boolean }) => {
      const roomId = selectedRoomId;
      if (!roomId) return;
      setBusy(true);
      setError(null);
      setAudit(null);
      try {
        // QA P1 #2: forward the room's stored corpus_ids explicitly so the
        // run actually retrieves against them. Backend has a fallback too,
        // but passing here keeps the contract crisp: the UI knows which
        // corpora the active room runs against.
        const { run: created, events: initialEvents } = await createRun(
          roomId,
          prompt,
          {
            ...(options ?? {}),
            corpusIds: activeRoomCorpusIds,
          },
        );
        setRun(created);
        setActivePrompt(prompt);
        setEvents(initialEvents);
        setComposerDraftsByRoom((prev) => ({ ...prev, [roomId]: "" }));
        getRunAuditSummary(created.runId)
          .then((a) => setAudit(a))
          .catch(() => undefined);
      } catch (err) {
        setError(`run_submission_failed: ${String(err)}`);
      } finally {
        setBusy(false);
      }
    },
    [selectedRoomId, activeRoomCorpusIds],
  );

  const onCancel = useCallback(async () => {
    if (!run) return;
    try {
      const next = await cancelRun(run.runId);
      setRun(next);
    } catch (err) {
      setError(`cancel_failed: ${String(err)}`);
    }
  }, [run]);

  const onPause = useCallback(async () => {
    if (!run) return;
    try {
      const next = await pauseRun(run.runId);
      setRun(next);
    } catch (err) {
      setError(`pause_failed: ${String(err)}`);
    }
  }, [run]);

  const onResume = useCallback(async () => {
    if (!run) return;
    try {
      const next = await resumeRun(run.runId);
      setRun(next);
    } catch (err) {
      setError(`resume_failed: ${String(err)}`);
    }
  }, [run]);

  // F049: send a live message into the running run. The next member picks it up
  // as authoritative direction; the polling loop surfaces it in the transcript.
  const onInterject = useCallback(async (text: string) => {
    if (!run) return;
    try {
      await injectMessage(run.runId, text);
    } catch (err) {
      setError(`interjection_failed: ${String(err)}`);
    }
  }, [run]);

  const onInspect = useCallback(
    (args: { round: number; memberId: string | undefined; turnId: string }) => {
      if (!run) return;
      setInspection({
        runId: run.runId,
        round: args.round,
        memberId: args.memberId,
        turnId: args.turnId,
      });
    },
    [run],
  );

  return (
    // QA P2 #7 (2026-06-12): no `role="main"` here — App.tsx wraps each
    // feature in a real `<main>` element, and a nested second main is
    // a landmark-uniqueness violation. No `aria-label` either — axe
    // flags `aria-prohibited-attr` because a roleless div is a generic
    // element where aria-label is not permitted. The three inner
    // <section> landmarks each carry their own labels (Rooms /
    // Transcript / Audit), which is what assistive tech navigates by.
    <div
      className={
        "council-shell" + (auditCollapsed ? " council-shell--audit-collapsed" : "")
      }
    >
      <section className="council-pane" aria-label="Rooms">
        <h2>Rooms</h2>
        {/* Selection-only: Council picks an existing shared room to run.
            Create/edit/delete/seed/import live in the Rooms tab. */}
        <CouncilRoomList
          rooms={rooms}
          selectedId={selectedRoomId}
          onSelect={setSelectedRoomId}
          manage={false}
          onManageRooms={goToRooms}
        />
      </section>

      <section className="council-pane" aria-label="Transcript">
        <div className="council-pane-head">
          <h2>Transcript</h2>
          <button
            type="button"
            className="council-audit-toggle"
            onClick={toggleAudit}
            aria-pressed={auditCollapsed}
            title={auditCollapsed ? "Show the Audit pane" : "Hide the Audit pane"}
            data-testid="audit-toggle"
          >
            {auditCollapsed ? "Show audit ◂" : "Hide audit ▸"}
          </button>
        </div>
        {error && (
          <div className="council-status-banner error" role="alert">
            {error}
          </div>
        )}
        {run && <CouncilRunStatusBanner status={run} />}
        <CouncilRunControls
          status={run}
          onPause={onPause}
          onResume={onResume}
          onCancel={onCancel}
        />
        <CouncilTranscript
          events={events}
          onInspect={onInspect}
          userPrompt={activePrompt || run?.prompt || undefined}
          steelmanMemberIds={steelmanMemberIds}
        />
        {roomUsesWorkRail(workRailRoom) && (
          <CouncilWorkRail runId={run?.runId ?? null} events={events} />
        )}
        {run && (
          <CouncilCalloutPanel
            runId={run.runId}
            roomId={selectedRoomId}
            live={!["done", "failed", "cancelled"].includes(run.state)}
          />
        )}
        {run && ["done", "failed", "cancelled"].includes(run.state) && (
          // Renders nothing unless the run produced an auto-apply patch; keyed
          // on runId so it re-fetches the proposed patch per terminal run.
          <ApplyWorkspacePanel key={run.runId} runId={run.runId} />
        )}
        {isDemoRoom && <AiarReadinessBanner />}
        <CouncilPromptComposer
          disabled={busy || !selectedRoomId}
          onRun={onRun}
          onCancel={onCancel}
          onInterject={onInterject}
          runState={run?.state}
          value={composerInput}
          onChange={setSelectedRoomDraft}
        />
        {isDemoRoom && (
          <div className="council-demo-prompt-actions">
            <button
              type="button"
              className="council-demo-prompt-btn"
              onClick={() => setSelectedRoomDraft(DEMO_PROMPT)}
              data-testid="try-demo-prompt-btn"
            >
              Try the demo prompt
            </button>
          </div>
        )}
      </section>

      {!auditCollapsed && (
        <section className="council-pane" aria-label="Audit">
          <h2>Audit</h2>
          <ContextAuditSummary summary={audit} />
          <AgentContextInspector />
        </section>
      )}

      {inspection && (
        <ContextInspectionDrawer
          runId={inspection.runId}
          round={inspection.round}
          turnId={inspection.turnId}
          memberId={inspection.memberId}
          onClose={() => setInspection(null)}
        />
      )}

    </div>
  );
}
