// F143 (Slice D bonus) — council MEMBER_MESSAGE usage flows through adaptEvent.
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ getJSON: vi.fn(), postJSON: vi.fn() }));
import { getJSON } from "../api";
import { getRunEvents } from "./council";

const mockGetJSON = getJSON as unknown as ReturnType<typeof vi.fn>;

afterEach(() => vi.clearAllMocks());

describe("getRunEvents usage adaptation", () => {
  it("maps a MEMBER_MESSAGE usage dict to camelCase", async () => {
    mockGetJSON.mockResolvedValueOnce({
      events: [
        {
          id: "e1", run_id: "r1", sequence: 1, type: "member_message",
          status: "ok", created_at: "t", member_id: "m-1", round: 1,
          usage: { input_tokens: 200, output_tokens: 80, cache_read_input_tokens: 5 },
        },
        {
          id: "e2", run_id: "r1", sequence: 2, type: "context_built",
          status: "ok", created_at: "t", // no usage
        },
      ],
      terminal: false,
      last_sequence: 2,
    });
    const { events } = await getRunEvents("r1");
    expect(events[0].usage).toEqual({
      inputTokens: 200,
      outputTokens: 80,
      cacheReadInputTokens: 5,
      cacheWriteInputTokens: undefined,
    });
    expect(events[1].usage).toBeUndefined();
  });

  it("drops a usage dict with no token numbers", async () => {
    mockGetJSON.mockResolvedValueOnce({
      events: [
        {
          id: "e1", run_id: "r1", sequence: 1, type: "member_message",
          status: "ok", created_at: "t", usage: { measured: false },
        },
      ],
      terminal: true,
      last_sequence: 1,
    });
    const { events } = await getRunEvents("r1");
    expect(events[0].usage).toBeUndefined();
  });
});
