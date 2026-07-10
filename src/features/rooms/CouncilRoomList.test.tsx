// F031-DEMO-CORPUS Task 3 — CouncilRoomList tests.
//
// Empty-state seed affordance + structured-error banner +
// Retry button + "Advanced: skip corpus seed" disclosure.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CouncilRoomList from "./CouncilRoomList";
import type { CouncilRoomSummary } from "../council/types";

// Spy on the seedDemoRoom export. We can't easily mock the whole module
// from inside vitest config, so we use vi.mock at the top level.
vi.mock("./CouncilDemoRoomSeed", async () => {
  const actual = await vi.importActual<
    typeof import("./CouncilDemoRoomSeed")
  >("./CouncilDemoRoomSeed");
  return {
    ...actual,
    seedDemoRoom: vi.fn(),
  };
});

vi.mock("../../lib/api/councilRoom", () => ({
  deleteRoom: vi.fn(),
}));

import { DemoSeedError, seedDemoRoom } from "./CouncilDemoRoomSeed";
import { deleteRoom } from "../../lib/api/councilRoom";

const seedSpy = seedDemoRoom as unknown as ReturnType<typeof vi.fn>;
const deleteSpy = deleteRoom as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  seedSpy.mockReset();
});

afterEach(() => cleanup());

function renderEmpty() {
  const onSelect = vi.fn();
  const onRoomsChanged = vi.fn();
  render(
    <CouncilRoomList
      rooms={[]}
      selectedId={null}
      onSelect={onSelect}
      onRoomsChanged={onRoomsChanged}
    />,
  );
  return { onSelect, onRoomsChanged };
}

describe("CouncilRoomList empty state", () => {
  it("renders the Seed demo room button", () => {
    seedSpy.mockResolvedValueOnce(undefined);
    renderEmpty();
    expect(
      screen.getByRole("button", { name: /seed a demo room/i }),
    ).toBeTruthy();
  });

  it("renders rooms list when rooms are present (no seed UI)", () => {
    const rooms: CouncilRoomSummary[] = [
      {
        id: "r-1",
        name: "Room 1",
        updatedAt: "2026-06-11T00:00:00Z",
        revision: 1,
        statusHint: "ready",
      },
    ];
    render(
      <CouncilRoomList
        rooms={rooms}
        selectedId="r-1"
        onSelect={() => {}}
        onRoomsChanged={() => {}}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /seed a demo room/i }),
    ).toBeNull();
  });

  it("shows a New room button in the empty state and invokes onNewRoom", () => {
    const onNewRoom = vi.fn();
    render(
      <CouncilRoomList
        rooms={[]}
        selectedId={null}
        onSelect={() => {}}
        onRoomsChanged={() => {}}
        onNewRoom={onNewRoom}
      />,
    );
    fireEvent.click(screen.getByTestId("new-room-btn"));
    expect(onNewRoom).toHaveBeenCalled();
  });

  it("shows a New room button alongside an existing rooms list", () => {
    const onNewRoom = vi.fn();
    const rooms: CouncilRoomSummary[] = [
      {
        id: "r-1",
        name: "Room 1",
        updatedAt: "2026-06-11T00:00:00Z",
        revision: 1,
        statusHint: "ready",
      },
    ];
    render(
      <CouncilRoomList
        rooms={rooms}
        selectedId="r-1"
        onSelect={() => {}}
        onRoomsChanged={() => {}}
        onNewRoom={onNewRoom}
      />,
    );
    fireEvent.click(screen.getByTestId("new-room-btn"));
    expect(onNewRoom).toHaveBeenCalled();
    // The existing room is still shown.
    expect(screen.getByText("Room 1")).toBeTruthy();
  });

  it("deletes a room after confirm and refreshes the list", async () => {
    deleteSpy.mockReset();
    deleteSpy.mockResolvedValueOnce(undefined);
    window.confirm = vi.fn(() => true);
    const onRoomsChanged = vi.fn();
    const rooms: CouncilRoomSummary[] = [
      { id: "r-1", name: "Room 1", updatedAt: "2026-06-11T00:00:00Z",
        revision: 1, statusHint: "ready" },
    ];
    render(
      <CouncilRoomList rooms={rooms} selectedId="r-1" onSelect={() => {}}
        onRoomsChanged={onRoomsChanged} />,
    );
    fireEvent.click(screen.getByTestId("delete-room-r-1"));
    await waitFor(() => expect(deleteSpy).toHaveBeenCalledWith("r-1"));
    expect(onRoomsChanged).toHaveBeenCalled();
  });

  it("does not delete when the confirm is dismissed", () => {
    deleteSpy.mockReset();
    window.confirm = vi.fn(() => false);
    const rooms: CouncilRoomSummary[] = [
      { id: "r-1", name: "Room 1", updatedAt: "2026-06-11T00:00:00Z",
        revision: 1, statusHint: "ready" },
    ];
    render(
      <CouncilRoomList rooms={rooms} selectedId="r-1" onSelect={() => {}}
        onRoomsChanged={() => {}} />,
    );
    fireEvent.click(screen.getByTestId("delete-room-r-1"));
    expect(deleteSpy).not.toHaveBeenCalled();
  });
});

describe("CouncilRoomList — seed error UI (invariant 4)", () => {
  it("renders the error banner with Retry + Advanced disclosure on DemoSeedError", async () => {
    seedSpy.mockRejectedValueOnce(new DemoSeedError("sha256 mismatch"));
    renderEmpty();
    fireEvent.click(screen.getByRole("button", { name: /seed a demo room/i }));

    await waitFor(() => {
      expect(screen.getByTestId("seed-error-banner")).toBeTruthy();
    });
    expect(screen.getByText(/sha256 mismatch/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /^retry$/i })).toBeTruthy();
    // Advanced disclosure is collapsed by default but the summary is rendered.
    expect(screen.getByText(/advanced: skip corpus seed/i)).toBeTruthy();
  });

  it("Retry button re-invokes seedDemoRoom without skipCorpus", async () => {
    seedSpy
      .mockRejectedValueOnce(new DemoSeedError("first failure"))
      .mockResolvedValueOnce(undefined);
    const { onRoomsChanged } = renderEmpty();

    fireEvent.click(screen.getByRole("button", { name: /seed a demo room/i }));
    await waitFor(() => screen.getByTestId("seed-error-banner"));

    fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    await waitFor(() => expect(seedSpy).toHaveBeenCalledTimes(2));

    // Both calls must be without skipCorpus.
    expect(seedSpy.mock.calls[0][0]).toBeUndefined();
    expect(seedSpy.mock.calls[1][0]).toBeUndefined();
    await waitFor(() => expect(onRoomsChanged).toHaveBeenCalled());
  });

  it("Advanced disclosure invokes seedDemoRoom with skipCorpus=true", async () => {
    seedSpy
      .mockRejectedValueOnce(new DemoSeedError("first failure"))
      .mockResolvedValueOnce(undefined);
    renderEmpty();

    fireEvent.click(screen.getByRole("button", { name: /seed a demo room/i }));
    await waitFor(() => screen.getByTestId("seed-error-banner"));

    fireEvent.click(
      screen.getByRole("button", { name: /seed without corpus/i }),
    );
    await waitFor(() => expect(seedSpy).toHaveBeenCalledTimes(2));

    expect(seedSpy.mock.calls[1][0]).toEqual({ skipCorpus: true });
  });
});
