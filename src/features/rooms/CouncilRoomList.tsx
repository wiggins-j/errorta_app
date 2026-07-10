// F031 Phase 2/5 + F031-DEMO-CORPUS — shared room list.
//
// Two modes (IA refactor 2026-06-22):
//   - `manage` (default, used by the Rooms tab): full management — New room,
//     per-room Delete, and the empty-state demo seed (with structured-error
//     banner + Retry + "Advanced: skip corpus seed" disclosure).
//   - selection-only (`manage={false}`, used by Council to pick a room to run):
//     row selection only. No create/delete/seed; the empty state nudges the
//     user to the Rooms tab via the optional `onManageRooms` link.
import { useState } from "react";
import type { CouncilRoomSummary } from "../council/types";
import { DemoSeedError, seedDemoRoom } from "./CouncilDemoRoomSeed";
import { deleteRoom } from "../../lib/api/councilRoom";
import AiarReadinessBanner from "../council/AiarReadinessBanner";

interface Props {
  rooms: CouncilRoomSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** Enable room MANAGEMENT (create/delete/seed). Defaults to true (Rooms tab). */
  manage?: boolean;
  onRoomsChanged?: () => void;
  onNewRoom?: () => void;
  creatingRoom?: boolean;
  /** Selection-only empty/footer affordance — navigate to the Rooms tab. */
  onManageRooms?: () => void;
}

export default function CouncilRoomList({
  rooms,
  selectedId,
  onSelect,
  manage = true,
  onRoomsChanged,
  onNewRoom,
  creatingRoom,
  onManageRooms,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [seedError, setSeedError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const onDeleteRoom = async (room: CouncilRoomSummary) => {
    if (deletingId) return;
    if (!window.confirm(`Delete room "${room.name}"? This cannot be undone.`)) {
      return;
    }
    setDeletingId(room.id);
    try {
      await deleteRoom(room.id);
      onRoomsChanged?.();
    } catch (err) {
      setSeedError(
        `delete_failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setDeletingId(null);
    }
  };

  const runSeed = async (skipCorpus = false) => {
    setBusy(true);
    setSeedError(null);
    try {
      await seedDemoRoom(skipCorpus ? { skipCorpus: true } : undefined);
      onRoomsChanged?.();
    } catch (err) {
      if (err instanceof DemoSeedError) {
        setSeedError(err.structuredReason);
      } else {
        setSeedError(
          `seed_failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    } finally {
      setBusy(false);
    }
  };

  if (rooms.length === 0) {
    // Selection-only empty state (Council): no seed/create here — that lives
    // in the Rooms tab. Just nudge the user there.
    if (!manage) {
      return (
        <div className="council-room-list">
          <p className="council-empty">
            No rooms yet. Create one in the Rooms tab, then come back to run it
            here.
          </p>
          {onManageRooms && (
            <div className="council-room-list-actions">
              <button
                type="button"
                className="council-manage-rooms-btn"
                onClick={onManageRooms}
                data-testid="manage-rooms-btn"
              >
                Manage rooms →
              </button>
            </div>
          )}
        </div>
      );
    }
    return (
      <div className="council-room-list">
        <AiarReadinessBanner />
        <p className="council-empty">
          No rooms yet. The demo seed ensures the F007 welcome corpus is on disk
          and creates a known-good 2-fake-member room.
        </p>
        <div className="council-room-list-actions">
          {onNewRoom && (
            <button
              type="button"
              className="council-new-room-btn"
              onClick={onNewRoom}
              disabled={busy || creatingRoom}
              data-testid="new-room-btn"
            >
              {creatingRoom ? "Creating…" : "+ New room"}
            </button>
          )}
          <button
            type="button"
            className="seed-demo-btn"
            onClick={() => runSeed(false)}
            disabled={busy}
            aria-label="Seed a demo room"
          >
            {busy ? "Seeding…" : "Seed demo room"}
          </button>
        </div>
        {seedError && (
          <div
            className="council-status-banner error"
            role="alert"
            data-testid="seed-error-banner"
          >
            <p>
              Could not seed the demo: <code>{seedError}</code>
            </p>
            <button
              type="button"
              className="seed-retry-btn"
              onClick={() => runSeed(false)}
              disabled={busy}
            >
              Retry
            </button>
            <details className="seed-advanced">
              <summary>Advanced: skip corpus seed</summary>
              <p>
                Skips the welcome-corpus install. Inspection will show zero
                retrieved sources, but the demo room will still run.
              </p>
              <button
                type="button"
                className="seed-skip-corpus-btn"
                onClick={() => runSeed(true)}
                disabled={busy}
              >
                Seed without corpus
              </button>
            </details>
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="council-room-list">
      <div className="council-room-list-header">
        <span className="council-room-list-title">Rooms</span>
        {manage && onNewRoom && (
          <button
            type="button"
            className="council-new-room-btn"
            onClick={onNewRoom}
            disabled={creatingRoom}
            data-testid="new-room-btn"
          >
            {creatingRoom ? "Creating…" : "+ New room"}
          </button>
        )}
        {!manage && onManageRooms && (
          <button
            type="button"
            className="council-manage-rooms-btn"
            onClick={onManageRooms}
            data-testid="manage-rooms-btn"
          >
            Manage rooms →
          </button>
        )}
      </div>
      <ul
        className="council-room-list-ul"
        role={manage ? "list" : "listbox"}
        aria-label="Council rooms"
      >
        {rooms.map((room) => (
          <li
            key={room.id}
            role={manage ? "listitem" : "presentation"}
            className="council-room-li"
          >
            <button
              type="button"
              className="council-room-row"
              role={manage ? undefined : "option"}
              aria-selected={manage ? undefined : room.id === selectedId}
              aria-current={room.id === selectedId ? "true" : undefined}
              onClick={() => onSelect(room.id)}
            >
              <span className="room-name">{room.name}</span>
              <span className="room-meta">
                rev {room.revision} · {room.statusHint} · {room.updatedAt}
              </span>
            </button>
            {manage && (
              <button
                type="button"
                className="council-room-delete"
                aria-label={`Delete room ${room.name}`}
                title="Delete room"
                disabled={deletingId === room.id}
                onClick={() => onDeleteRoom(room)}
                data-testid={`delete-room-${room.id}`}
              >
                {deletingId === room.id ? "…" : "✕"}
              </button>
            )}
          </li>
        ))}
      </ul>
      {seedError && (
        <div className="council-status-banner error" role="alert">
          <code>{seedError}</code>
        </div>
      )}
    </div>
  );
}
