// Rooms feature (IA refactor 2026-06-22).
//
// Room MANAGEMENT moved out of the Council shell into its own tab: list,
// create, edit (CouncilRoomEditor), delete, demo-seed, and profile import.
// Rooms are SHARED — the same backend rooms power Council, Coding Team, and
// this tab; only the management UI lives here. Council and Coding select from
// these rooms to run.
import { useCallback, useEffect, useState } from "react";
import { listRooms } from "../../lib/api/council";
import { createRoom, buildBlankRoom } from "../../lib/api/councilRoom";
import type { CouncilRoomSummary } from "../council/types";
import CouncilRoomList from "./CouncilRoomList";
import CouncilRoomEditor from "./CouncilRoomEditor";
import CouncilProfileImport from "./CouncilProfileImport";
import "../council/council.css";

export default function RoomsShell() {
  const [rooms, setRooms] = useState<CouncilRoomSummary[]>([]);
  const [selectedRoomId, setSelectedRoomId] = useState<string | null>(null);
  const [editingRoomId, setEditingRoomId] = useState<string | null>(null);
  const [importingProfile, setImportingProfile] = useState(false);
  const [creatingRoom, setCreatingRoom] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshRooms = useCallback(async () => {
    try {
      const r = await listRooms();
      setRooms(r);
      setSelectedRoomId((prev) =>
        prev && r.some((room) => room.id === prev)
          ? prev
          : r.length > 0
            ? r[0].id
            : null,
      );
    } catch (err) {
      setError(
        `council_api_unreachable: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    listRooms()
      .then((r) => {
        if (cancelled) return;
        setRooms(r);
        if (r.length > 0) setSelectedRoomId((prev) => prev ?? r[0].id);
      })
      .catch((err) => {
        if (!cancelled) setError(`council_api_unreachable: ${err?.message ?? err}`);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Create a blank room, then open the editor on it so the user can configure
  // models/settings before it's used by Council or Coding.
  const onNewRoom = useCallback(async () => {
    setCreatingRoom(true);
    setError(null);
    try {
      const resp = await createRoom(buildBlankRoom("New room"));
      const newId = String((resp.room as { id?: unknown }).id ?? "");
      await refreshRooms();
      if (newId) {
        setSelectedRoomId(newId);
        setEditingRoomId(newId);
      }
    } catch (err) {
      setError(
        `create_room_failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setCreatingRoom(false);
    }
  }, [refreshRooms]);

  return (
    <div className="rooms-shell">
      <section className="council-pane" aria-label="Rooms">
        <h2>Rooms</h2>
        <p className="rooms-intro">
          Rooms are shared across Council and Coding Team. Create and configure
          them here, then select one to run from those tabs.
        </p>
        {error && (
          <div className="council-status-banner error" role="alert">
            {error}
          </div>
        )}
        <CouncilRoomList
          rooms={rooms}
          selectedId={selectedRoomId}
          onSelect={setSelectedRoomId}
          manage
          onRoomsChanged={refreshRooms}
          onNewRoom={onNewRoom}
          creatingRoom={creatingRoom}
        />
        <div className="rooms-actions">
          <button
            type="button"
            className="council-room-action-btn council-import-profile-btn"
            onClick={() => setImportingProfile(true)}
            data-testid="import-profile-btn"
          >
            Import profile
          </button>
          {selectedRoomId && (
            <button
              type="button"
              className="council-room-action-btn council-edit-room-btn"
              onClick={() => setEditingRoomId(selectedRoomId)}
              data-testid="edit-room-btn"
            >
              Edit room
            </button>
          )}
        </div>
      </section>

      {editingRoomId && (
        <div className="council-modal-overlay" role="presentation">
          <CouncilRoomEditor
            roomId={editingRoomId}
            onClose={() => setEditingRoomId(null)}
            onSaved={() => {
              refreshRooms();
            }}
          />
        </div>
      )}

      {importingProfile && (
        <div className="council-modal-overlay" role="presentation">
          <CouncilProfileImport
            onClose={() => setImportingProfile(false)}
            onCreated={(roomId) => {
              setImportingProfile(false);
              refreshRooms();
              if (roomId) {
                setSelectedRoomId(roomId);
                setEditingRoomId(roomId);
              }
            }}
          />
        </div>
      )}
    </div>
  );
}
