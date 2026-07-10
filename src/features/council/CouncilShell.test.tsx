// F031-DEMO-CORPUS Task 5 — CouncilShell demo-prompt button tests.
//
// Asserts:
//   - "Try the demo prompt" button visible when active room metadata
//     carries `demo_marker === DEMO_ROOM_MARKER`.
//   - Hidden when active room is non-demo.
//   - Clicking fills the composer textarea with `DEMO_PROMPT`.
//   - `DEMO_PROMPT` is non-empty and not a generic greeting.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CouncilShell from "./CouncilShell";
import { DEMO_PROMPT, DEMO_ROOM_MARKER } from "../rooms/CouncilDemoRoomSeed";

// Mock the council API so the shell can mount without a real sidecar.
vi.mock("../../lib/api/council", async () => {
  const actual = await vi.importActual<
    typeof import("../../lib/api/council")
  >("../../lib/api/council");
  return {
    ...actual,
    listRooms: vi.fn(),
    listRuns: vi.fn(),
    getRoomMetadata: vi.fn(),
    getRun: vi.fn(),
    getRunAuditSummary: vi.fn(),
    getMobileActivity: vi.fn().mockResolvedValue({ seq: 0, runId: null }),
    injectMessage: vi.fn(),
    createRun: vi.fn(),
    cancelRun: vi.fn(),
    pauseRun: vi.fn(),
    resumeRun: vi.fn(),
  };
});

// Council now only READS the shared room API to select a room to run.
// Room management (create/edit/delete/seed/import) moved to the Rooms tab.
vi.mock("../../lib/api/councilRoom", () => ({
  getRoomFull: vi.fn().mockResolvedValue({ room: {}, validation: { status: "draft", errors: [] } }),
}));

// Prevent AgentContextInspector from firing real network calls in jsdom.
vi.mock("../../lib/api/agentContext", () => ({
  listAgentContextCapsules: vi.fn().mockResolvedValue([]),
  getAgentContextCapsule: vi.fn().mockResolvedValue(null),
  packAgentContextCapsule: vi.fn().mockResolvedValue(""),
}));

// Avoid a real `/healthz` probe; AiarReadinessBanner self-resolves to false on error.
vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>(
    "../../lib/api",
  );
  return {
    ...actual,
    sidecarHealth: vi
      .fn()
      .mockResolvedValue({ aiar_pin: { available: true, version: null, source: "absent" } }),
  };
});

import {
  listRooms,
  listRuns,
  getRoomMetadata,
  getRun,
  getRunAuditSummary,
} from "../../lib/api/council";

const listRoomsSpy = listRooms as unknown as ReturnType<typeof vi.fn>;
const listRunsSpy = listRuns as unknown as ReturnType<typeof vi.fn>;
const getRoomMetadataSpy = getRoomMetadata as unknown as ReturnType<typeof vi.fn>;
const getRunSpy = getRun as unknown as ReturnType<typeof vi.fn>;
const getRunAuditSummarySpy = getRunAuditSummary as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  listRoomsSpy.mockReset();
  listRunsSpy.mockReset();
  listRunsSpy.mockResolvedValue([]);
  getRoomMetadataSpy.mockReset();
  getRunAuditSummarySpy.mockReset();
  getRunAuditSummarySpy.mockResolvedValue(null);
  // Some legacy tests still write browser storage; keep test isolation strict
  // even though CouncilShell now restores visible runs through listRuns(room).
  try {
    sessionStorage.clear();
  } catch {
    /* no-op */
  }
});

afterEach(() => cleanup());

const demoRoom = {
  id: "demo-1",
  name: "Demo Room",
  updatedAt: "2026-06-11T00:00:00Z",
  revision: 1,
  statusHint: "draft",
};
const otherRoom = {
  id: "r-2",
  name: "Other Room",
  updatedAt: "2026-06-11T00:00:00Z",
  revision: 1,
  statusHint: "ready",
};
const thirdRoom = {
  id: "r-3",
  name: "Third Room",
  updatedAt: "2026-06-11T00:00:00Z",
  revision: 1,
  statusHint: "ready",
};

describe("CouncilShell — demo prompt button", () => {
  it("renders 'Try the demo prompt' when the active room is the demo room", async () => {
    listRoomsSpy.mockResolvedValue([demoRoom]);
    getRoomMetadataSpy.mockResolvedValue({ demo_marker: DEMO_ROOM_MARKER });

    render(<CouncilShell />);

    await waitFor(() => {
      expect(screen.getByTestId("try-demo-prompt-btn")).toBeTruthy();
    });
  });

  it("does NOT render the demo button when the active room is not the demo room", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom]);
    getRoomMetadataSpy.mockResolvedValue(null);

    render(<CouncilShell />);

    // Wait for the rooms list call to settle and metadata fetch to complete.
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalled());
    // Demo button must be absent.
    expect(screen.queryByTestId("try-demo-prompt-btn")).toBeNull();
  });

  it("clicking 'Try the demo prompt' fills the composer textarea with DEMO_PROMPT", async () => {
    listRoomsSpy.mockResolvedValue([demoRoom]);
    getRoomMetadataSpy.mockResolvedValue({ demo_marker: DEMO_ROOM_MARKER });

    render(<CouncilShell />);

    await waitFor(() => screen.getByTestId("try-demo-prompt-btn"));

    fireEvent.click(screen.getByTestId("try-demo-prompt-btn"));

    const textarea = screen.getByTestId(
      "council-prompt-textarea",
    ) as HTMLTextAreaElement;
    await waitFor(() => {
      expect(textarea.value).toBe(DEMO_PROMPT);
    });
  });
});

describe("CouncilShell — room management moved to Rooms tab", () => {
  it("does NOT render room create/edit/delete/seed management affordances", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom]);
    getRoomMetadataSpy.mockResolvedValue(null);

    render(<CouncilShell />);
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalled());

    // Selection-only: none of the management controls live in Council now.
    expect(screen.queryByTestId("new-room-btn")).toBeNull();
    expect(screen.queryByTestId("edit-room-btn")).toBeNull();
    expect(screen.queryByTestId("import-profile-btn")).toBeNull();
    expect(screen.queryByTestId(`delete-room-${otherRoom.id}`)).toBeNull();
    expect(screen.queryByRole("button", { name: /seed a demo room/i })).toBeNull();
  });

  it("still lets the user SELECT an existing room", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom, thirdRoom]);
    getRoomMetadataSpy.mockResolvedValue(null);

    render(<CouncilShell />);
    await waitFor(() =>
      expect(getRoomMetadataSpy).toHaveBeenCalledWith(otherRoom.id),
    );

    fireEvent.click(screen.getByRole("option", { name: /Third Room/i }));
    await waitFor(() =>
      expect(getRoomMetadataSpy).toHaveBeenCalledWith(thirdRoom.id),
    );
  });

  it("'Manage rooms →' (empty state) dispatches a navigate-to-rooms event", async () => {
    listRoomsSpy.mockResolvedValue([]);
    getRoomMetadataSpy.mockResolvedValue(null);
    const navSpy = vi.fn();
    window.addEventListener("errorta:navigate", navSpy);
    try {
      render(<CouncilShell />);
      await waitFor(() => screen.getByTestId("manage-rooms-btn"));

      fireEvent.click(screen.getByTestId("manage-rooms-btn"));

      expect(navSpy).toHaveBeenCalled();
      const evt = navSpy.mock.calls[0][0] as CustomEvent<{ view?: string }>;
      expect(evt.detail?.view).toBe("rooms");
    } finally {
      window.removeEventListener("errorta:navigate", navSpy);
    }
  });
});

describe("DEMO_PROMPT sanity (Task 5 contract)", () => {
  it("is not empty and not a generic greeting", () => {
    expect(DEMO_PROMPT.length).toBeGreaterThan(0);
    expect(DEMO_PROMPT).not.toMatch(/^hello/i);
    expect(DEMO_PROMPT).toMatch(/[.?!]/); // one full sentence
  });
});

// QA P1 #2 — Forward the room's stored corpus_ids to createRun.
import {
  createRun,
} from "../../lib/api/council";

const createRunSpy = createRun as unknown as ReturnType<typeof vi.fn>;

describe("CouncilShell — per-room prompt drafts", () => {
  beforeEach(() => {
    createRunSpy.mockReset();
  });

  it("keeps unsent prompt drafts scoped to the selected room", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom, thirdRoom]);
    getRoomMetadataSpy.mockResolvedValue(null);

    render(<CouncilShell />);
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalledWith(otherRoom.id));

    const textarea = screen.getByTestId(
      "council-prompt-textarea",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Prompt for room two" } });

    fireEvent.click(screen.getByRole("option", { name: /Third Room/i }));
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalledWith(thirdRoom.id));
    expect(textarea.value).toBe("");

    fireEvent.change(textarea, { target: { value: "Prompt for room three" } });
    fireEvent.click(screen.getByRole("option", { name: /Other Room/i }));
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalledWith(otherRoom.id));
    expect(textarea.value).toBe("Prompt for room two");

    fireEvent.click(screen.getByRole("option", { name: /Third Room/i }));
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalledWith(thirdRoom.id));
    expect(textarea.value).toBe("Prompt for room three");
  });

  it("clears only the draft for the room that successfully starts a run", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom, thirdRoom]);
    getRoomMetadataSpy.mockResolvedValue(null);
    createRunSpy.mockResolvedValue({
      run: { runId: "r-y", state: "running", backendStatus: "running" },
      events: [],
    });

    render(<CouncilShell />);
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalledWith(otherRoom.id));

    const textarea = screen.getByTestId(
      "council-prompt-textarea",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Run this room" } });
    fireEvent.click(screen.getByRole("option", { name: /Third Room/i }));
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalledWith(thirdRoom.id));
    fireEvent.change(textarea, { target: { value: "Keep this draft" } });
    fireEvent.click(screen.getByRole("option", { name: /Other Room/i }));
    await waitFor(() => expect(textarea.value).toBe("Run this room"));

    fireEvent.click(screen.getByRole("button", { name: /^run/i }));
    await waitFor(() => expect(createRunSpy).toHaveBeenCalled());
    expect(textarea.value).toBe("");

    fireEvent.click(screen.getByRole("option", { name: /Third Room/i }));
    await waitFor(() => expect(textarea.value).toBe("Keep this draft"));
  });
});

describe("CouncilShell — forwards room corpus_ids to createRun (QA P1 #2)", () => {
  beforeEach(() => {
    createRunSpy.mockReset();
  });
  it("passes the active room's corpus_ids when the user clicks Run", async () => {
    listRoomsSpy.mockResolvedValue([demoRoom]);
    // The /healthz + metadata bag both come back through getRoomMetadata.
    // Carry both the demo_marker (so the prompt button renders) AND
    // corpus_ids (so the shell forwards them).
    getRoomMetadataSpy.mockResolvedValue({
      demo_marker: DEMO_ROOM_MARKER,
      corpus_ids: ["welcome"],
    });
    createRunSpy.mockResolvedValue({
      run: {
        runId: "r-x",
        state: "running",
        backendStatus: "running",
      },
      events: [],
    });

    render(<CouncilShell />);

    // Wait for the demo room to be detected (proves getRoomMetadata resolved).
    await waitFor(() => screen.getByTestId("try-demo-prompt-btn"));

    // Click Try the demo prompt to fill the composer, then click Run.
    fireEvent.click(screen.getByTestId("try-demo-prompt-btn"));
    // The composer needs SOMETHING in it before Run becomes a meaningful click.
    const textarea = screen.getByTestId(
      "council-prompt-textarea",
    ) as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).toBe(DEMO_PROMPT));

    fireEvent.click(screen.getByRole("button", { name: /^run/i }));

    await waitFor(() => expect(createRunSpy).toHaveBeenCalled());
    const callArgs = createRunSpy.mock.calls[0];
    // signature: createRun(roomId, prompt, options?)
    expect(callArgs[0]).toBe(demoRoom.id);
    expect(callArgs[1]).toBe(DEMO_PROMPT);
    expect(callArgs[2]).toMatchObject({ corpusIds: ["welcome"] });
  });

  it("passes empty corpus_ids when the active room has none", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom]);
    getRoomMetadataSpy.mockResolvedValue(null);
    createRunSpy.mockResolvedValue({
      run: { runId: "r-y", state: "running", backendStatus: "running" },
      events: [],
    });

    render(<CouncilShell />);
    await waitFor(() => expect(getRoomMetadataSpy).toHaveBeenCalled());

    const textarea = screen.getByTestId(
      "council-prompt-textarea",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: /^run/i }));

    await waitFor(() => expect(createRunSpy).toHaveBeenCalled());
    expect(createRunSpy.mock.calls[0][2]).toMatchObject({ corpusIds: [] });
  });
});

describe("CouncilShell — room-scoped visible run", () => {
  beforeEach(() => {
    getRunSpy.mockReset();
  });

  it("loads the selected room's latest run and shows its transcript", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom]);
    listRunsSpy.mockResolvedValue([
      {
        runId: "r-live",
        roomId: otherRoom.id,
        state: "running",
        backendStatus: "running",
        updatedAt: "2026-06-12T00:00:00Z",
        eventCount: 1,
        lastSequence: 1,
      },
    ]);
    getRoomMetadataSpy.mockResolvedValue(null);
    getRunSpy.mockResolvedValue({
      run: {
        runId: "r-live",
        roomId: otherRoom.id,
        state: "running",
        backendStatus: "running",
        prompt: "Room-specific prompt",
      },
      events: [
        {
          id: "ev-1",
          type: "member_message",
          sequence: 1,
          status: "completed",
          createdAt: "2026-06-12T00:00:00Z",
          memberId: "m-1",
          round: 1,
          payload: { content: "Reconnected answer.", model: "gemma3" },
          raw: undefined,
        },
      ],
    });

    render(<CouncilShell />);

    await waitFor(() =>
      expect(listRunsSpy).toHaveBeenCalledWith({ roomId: otherRoom.id, limit: 1 }),
    );
    await waitFor(() =>
      expect(getRunSpy).toHaveBeenCalledWith("r-live"),
    );
    await waitFor(() =>
      expect(screen.getByText(/Reconnected answer\./)).toBeTruthy(),
    );
    expect(screen.getByText(/Room-specific prompt/)).toBeTruthy();
  });

  it("clears the previous room's prompt and transcript when the next room has no runs", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom, thirdRoom]);
    getRoomMetadataSpy.mockResolvedValue(null);
    listRunsSpy.mockImplementation(({ roomId }: { roomId?: string }) => {
      if (roomId === otherRoom.id) {
        return Promise.resolve([
          {
            runId: "r-old",
            roomId: otherRoom.id,
            state: "done",
            backendStatus: "done",
            updatedAt: "2026-06-12T00:00:00Z",
            eventCount: 1,
            lastSequence: 1,
          },
        ]);
      }
      return Promise.resolve([]);
    });
    getRunSpy.mockResolvedValue({
      run: {
        runId: "r-old",
        roomId: otherRoom.id,
        state: "done",
        backendStatus: "done",
        prompt: "Old room prompt",
      },
      events: [
        {
          id: "ev-old",
          type: "member_message",
          sequence: 1,
          status: "completed",
          createdAt: "2026-06-12T00:00:00Z",
          memberId: "m-1",
          round: 1,
          payload: { content: "Old room answer.", model: "gemma3" },
          raw: undefined,
        },
      ],
    });

    render(<CouncilShell />);

    await waitFor(() => expect(screen.getByText(/Old room answer\./)).toBeTruthy());
    fireEvent.click(screen.getByRole("option", { name: /Third Room/i }));

    await waitFor(() =>
      expect(listRunsSpy).toHaveBeenCalledWith({ roomId: thirdRoom.id, limit: 1 }),
    );
    await waitFor(() => expect(screen.queryByText(/Old room answer\./)).toBeNull());
    expect(screen.queryByText(/Old room prompt/)).toBeNull();
  });
});

describe("CouncilShell — collapsible Audit pane", () => {
  it("hides the Audit pane and widens Transcript when toggled", async () => {
    listRoomsSpy.mockResolvedValue([otherRoom]);
    getRoomMetadataSpy.mockResolvedValue(null);

    const { container } = render(<CouncilShell />);
    // Audit pane present by default.
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "Audit" })).toBeTruthy(),
    );
    const shell = container.querySelector(".council-shell")!;
    expect(shell.classList.contains("council-shell--audit-collapsed")).toBe(false);

    fireEvent.click(screen.getByTestId("audit-toggle"));

    // Audit pane gone; shell switched to the collapsed (2-column) layout.
    expect(screen.queryByRole("region", { name: "Audit" })).toBeNull();
    expect(shell.classList.contains("council-shell--audit-collapsed")).toBe(true);
  });
});
