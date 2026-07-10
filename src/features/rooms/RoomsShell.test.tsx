// IA refactor 2026-06-22 — RoomsShell tests.
//
// The Rooms tab owns room MANAGEMENT (relocated from the Council shell):
//   - lists shared rooms,
//   - creates a draft room and opens it in the editor,
//   - deletes a room (via the list),
//   - seeds a demo room in the empty state,
//   - imports a profile.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RoomsShell from "./index";

vi.mock("../../lib/api/council", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api/council")>(
    "../../lib/api/council",
  );
  return { ...actual, listRooms: vi.fn() };
});

vi.mock("../../lib/api/councilRoom", () => ({
  createRoom: vi.fn(),
  buildBlankRoom: vi.fn((name: string) => ({ id: "draft-room", name, members: [] })),
  deleteRoom: vi.fn(),
}));

// Stub the heavy editor + profile-import modals.
vi.mock("./CouncilRoomEditor", () => ({
  default: ({ roomId }: { roomId: string }) => (
    <div data-testid="room-editor">Editing {roomId}</div>
  ),
}));
vi.mock("./CouncilProfileImport", () => ({
  default: ({ onClose }: { onClose: () => void }) => (
    <div data-testid="profile-import">
      <button type="button" onClick={onClose}>
        close-import
      </button>
    </div>
  ),
}));

// The seed flow lives in CouncilDemoRoomSeed; spy on it.
vi.mock("./CouncilDemoRoomSeed", async () => {
  const actual = await vi.importActual<typeof import("./CouncilDemoRoomSeed")>(
    "./CouncilDemoRoomSeed",
  );
  return { ...actual, seedDemoRoom: vi.fn() };
});

import { listRooms } from "../../lib/api/council";
import { createRoom, buildBlankRoom, deleteRoom } from "../../lib/api/councilRoom";
import { seedDemoRoom } from "./CouncilDemoRoomSeed";

const listRoomsSpy = listRooms as unknown as ReturnType<typeof vi.fn>;
const createRoomSpy = createRoom as unknown as ReturnType<typeof vi.fn>;
const buildBlankRoomSpy = buildBlankRoom as unknown as ReturnType<typeof vi.fn>;
const deleteRoomSpy = deleteRoom as unknown as ReturnType<typeof vi.fn>;
const seedSpy = seedDemoRoom as unknown as ReturnType<typeof vi.fn>;

const room = {
  id: "r-1",
  name: "Room 1",
  updatedAt: "2026-06-11T00:00:00Z",
  revision: 1,
  statusHint: "ready",
};

beforeEach(() => {
  listRoomsSpy.mockReset();
  createRoomSpy.mockReset();
  buildBlankRoomSpy.mockClear();
  deleteRoomSpy.mockReset();
  seedSpy.mockReset();
});

afterEach(() => cleanup());

describe("RoomsShell", () => {
  it("lists the shared rooms", async () => {
    listRoomsSpy.mockResolvedValue([room]);
    render(<RoomsShell />);
    await waitFor(() => expect(screen.getByText("Room 1")).toBeTruthy());
  });

  it("creates a draft room and opens the editor on it", async () => {
    listRoomsSpy.mockResolvedValue([room]);
    createRoomSpy.mockResolvedValue({
      room: { id: "new-room-id", name: "New room" },
      validation: { status: "draft", errors: [] },
    });

    render(<RoomsShell />);
    await waitFor(() => screen.getByTestId("new-room-btn"));

    fireEvent.click(screen.getByTestId("new-room-btn"));

    await waitFor(() => expect(createRoomSpy).toHaveBeenCalledTimes(1));
    expect(buildBlankRoomSpy).toHaveBeenCalledWith("New room");
    await waitFor(() =>
      expect(screen.getByTestId("room-editor")).toHaveTextContent("new-room-id"),
    );
  });

  it("opens the editor for the selected room via Edit room", async () => {
    listRoomsSpy.mockResolvedValue([room]);
    render(<RoomsShell />);
    await waitFor(() => screen.getByTestId("edit-room-btn"));

    fireEvent.click(screen.getByTestId("edit-room-btn"));
    expect(screen.getByTestId("room-editor")).toHaveTextContent("r-1");
  });

  it("deletes a room from the list", async () => {
    listRoomsSpy.mockResolvedValue([room]);
    deleteRoomSpy.mockResolvedValue(undefined);
    window.confirm = vi.fn(() => true);

    render(<RoomsShell />);
    await waitFor(() => screen.getByTestId("delete-room-r-1"));

    fireEvent.click(screen.getByTestId("delete-room-r-1"));
    await waitFor(() => expect(deleteRoomSpy).toHaveBeenCalledWith("r-1"));
  });

  it("seeds a demo room from the empty state", async () => {
    listRoomsSpy.mockResolvedValue([]);
    seedSpy.mockResolvedValue(undefined);

    render(<RoomsShell />);
    await waitFor(() =>
      screen.getByRole("button", { name: /seed a demo room/i }),
    );

    fireEvent.click(screen.getByRole("button", { name: /seed a demo room/i }));
    await waitFor(() => expect(seedSpy).toHaveBeenCalled());
  });

  it("opens the profile import modal", async () => {
    listRoomsSpy.mockResolvedValue([room]);
    render(<RoomsShell />);
    await waitFor(() => screen.getByTestId("import-profile-btn"));

    fireEvent.click(screen.getByTestId("import-profile-btn"));
    expect(screen.getByTestId("profile-import")).toBeTruthy();
  });

  it("renders Import profile and Edit room as matching room actions", async () => {
    listRoomsSpy.mockResolvedValue([room]);
    render(<RoomsShell />);
    const importBtn = await screen.findByTestId("import-profile-btn");
    const editBtn = await screen.findByTestId("edit-room-btn");

    expect(importBtn).toHaveClass("council-room-action-btn");
    expect(editBtn).toHaveClass("council-room-action-btn");
  });
});
