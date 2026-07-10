// F135 — PM model-assignment insight API adapters.
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ sidecarFetch: vi.fn() }));
import { sidecarFetch } from "../api";
import { getBacklog, getModelLearning, getProjectModelUsage } from "./coding";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("F135 taskFrom preserves model_assignment", () => {
  it("round-trips the backend model_assignment onto CodingTask.modelAssignment", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        tasks: [
          {
            task_id: "t1",
            title: "scaffold",
            role: "dev",
            state: "todo",
            model_assignment: {
              route_id: "claude_cli.haiku",
              difficulty_tier: "light",
              source: "pm",
              rationale: "cheapest capable",
              escalation_count: 0,
            },
          },
          { task_id: "t2", title: "unassigned", role: "dev", state: "todo" },
        ],
      }),
    );
    const tasks = await getBacklog("p");
    expect(tasks[0].modelAssignment?.route_id).toBe("claude_cli.haiku");
    expect(tasks[0].modelAssignment?.source).toBe("pm");
    // A task without an assignment degrades to null, not undefined-with-default.
    expect(tasks[1].modelAssignment).toBeNull();
  });
});

describe("F135 getModelLearning adapter", () => {
  it("maps snake_case digest to camelCase with standings", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        learning: {
          summary: {
            total_attempts: 40,
            distinct_routes: 2,
            window_days: 90,
            generated_at: "2026-07-02T20:00:00+00:00",
            corpus_available: true,
          },
          thresholds: { min_attempts: 5, demotion_rate: 0.6, preferred_rate: 0.8 },
          routes: [
            {
              route_id: "claude_cli.haiku",
              capability_tier: "light",
              cost_tier: 1,
              tiers_unset: false,
              buckets: [
                {
                  task_type: "implementation",
                  difficulty_tier: "mid",
                  attempts: 9,
                  accepted: 3,
                  accepted_rate: 0.333,
                  gateway_failure_rate: 0,
                  p50_latency_ms: 15000,
                  avg_cost_tier: 1,
                  standing: "demoted",
                },
              ],
            },
          ],
        },
      }),
    );
    const d = await getModelLearning();
    expect(d.summary.totalAttempts).toBe(40);
    expect(d.summary.corpusAvailable).toBe(true);
    expect(d.thresholds.demotionRate).toBe(0.6);
    expect(d.routes[0].routeId).toBe("claude_cli.haiku");
    expect(d.routes[0].buckets[0].standing).toBe("demoted");
    expect(d.routes[0].buckets[0].p50LatencyMs).toBe(15000);
  });
});

describe("F135 getProjectModelUsage adapter", () => {
  it("maps multi and single members", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        usage: {
          multi_members: [
            {
              member_id: "m-dev-1",
              role: "dev",
              model_mode: "multi",
              pool: ["claude_cli.haiku", "claude_cli.sonnet"],
              assignments: [
                {
                  route_id: "claude_cli.sonnet",
                  difficulty_tier: "mid",
                  source: "selector",
                  count: 2,
                  max_escalation: 0,
                },
              ],
              escalations: [],
            },
          ],
          single_members: [
            { member_id: "m-review-1", route_id: "claude_cli.sonnet" },
          ],
        },
      }),
    );
    const u = await getProjectModelUsage("p");
    expect(u.multiMembers[0].memberId).toBe("m-dev-1");
    expect(u.multiMembers[0].pool).toEqual(["claude_cli.haiku", "claude_cli.sonnet"]);
    expect(u.multiMembers[0].assignments[0].count).toBe(2);
    expect(u.singleMembers[0].routeId).toBe("claude_cli.sonnet");
  });
});
