// F143 — token usage API adapters + formatTokens.
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ sidecarFetch: vi.fn() }));
import { sidecarFetch } from "../api";
import { getProjectUsageSummary, getTurns } from "./coding";
import { formatTokens } from "../../features/coding/formatTokens";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("formatTokens", () => {
  it("keeps small numbers exact and abbreviates large ones", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(940)).toBe("940");
    expect(formatTokens(12345)).toBe("12,345");
    expect(formatTokens(1_200_000)).toBe("1.2M");
    expect(formatTokens(3_400_000_000)).toBe("3.4B");
    expect(formatTokens(-5)).toBe("0");
    expect(formatTokens(Number.NaN)).toBe("0");
    // boundary: must not render "1000.0M"
    expect(formatTokens(999_999_999)).toBe("1.0B");
    expect(formatTokens(999_000_000)).toBe("999.0M");
  });
});

describe("getTurns usage adaptation", () => {
  it("maps a measured usage block to camelCase", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        turns: [
          {
            turn_id: "trn-1",
            member_id: "m-dev-1",
            usage: {
              measured: true,
              input_tokens: 100,
              output_tokens: 40,
              cache_read_input_tokens: 7,
            },
          },
          { turn_id: "trn-2", member_id: "m-dev-1" }, // no usage
        ],
      }),
    );
    const turns = await getTurns("p1");
    expect(turns[0].usage).toEqual({
      measured: true,
      inputTokens: 100,
      outputTokens: 40,
      cacheReadInputTokens: 7,
      cacheWriteInputTokens: null,
    });
    expect(turns[1].usage).toBeNull();
  });
});

describe("getProjectUsageSummary", () => {
  it("adapts by_member / by_route / total to camelCase buckets", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        usage: {
          by_member: {
            "m-dev-1": {
              input: 100, output: 40, cache_read: 7, cache_write: 0,
              turns: 2, measured_turns: 1, unreported_turns: 1,
            },
          },
          by_route: {
            "claude_cli.sonnet": {
              input: 100, output: 40, cache_read: 7, cache_write: 0,
              turns: 1, measured_turns: 1, unreported_turns: 0,
            },
          },
          total: {
            input: 100, output: 40, cache_read: 7, cache_write: 0,
            turns: 2, measured_turns: 1, unreported_turns: 1,
          },
        },
      }),
    );
    const usage = await getProjectUsageSummary("p1");
    expect(usage.total.input).toBe(100);
    expect(usage.total.unreportedTurns).toBe(1);
    expect(usage.byMember["m-dev-1"].cacheRead).toBe(7);
    expect(usage.byRoute["claude_cli.sonnet"].output).toBe(40);
  });

  it("degrades to zeroed buckets on a malformed body", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ usage: {} }));
    const usage = await getProjectUsageSummary("p1");
    expect(usage.total).toEqual({
      input: 0, output: 0, cacheRead: 0, cacheWrite: 0,
      turns: 0, measuredTurns: 0, unreportedTurns: 0,
    });
    expect(usage.byMember).toEqual({});
  });
});
