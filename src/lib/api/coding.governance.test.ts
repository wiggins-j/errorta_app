// F100 governance API adapters.
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ sidecarFetch: vi.fn() }));
import { sidecarFetch } from "../api";
import { acceptGovernanceArtifact, getGovernanceFull, interject } from "./coding";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("coding governance status API", () => {
  it("preserves the backend building step state", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        governance: {
          state: {
            mode: "strict",
            phase: "development",
            human_code_approval: "final_only",
            active_artifact_ids: {},
            updated_at: "2026-06-22T00:00:00Z",
          },
          artifacts: [],
          reviews: [],
          approvals: [],
          plan_slices: [],
        },
        status: {
          mode: "strict",
          stage: "build",
          status: "building",
          headline: "Building",
          actor_member_id: null,
          actor_label: null,
          review_pass: null,
          steps: [
            { stage: "brainstorm", state: "approved" },
            { stage: "spec", state: "approved" },
            { stage: "plan", state: "approved" },
            { stage: "build", state: "building" },
            { stage: "done", state: "pending" },
          ],
          build_progress: { done: 2, total: 5 },
        },
      }),
    );

    const out = await getGovernanceFull("proj");

    expect(mockFetch).toHaveBeenCalledWith("/coding/projects/proj/governance");
    expect(out.status.stage).toBe("build");
    expect(out.status.status).toBe("building");
    expect(out.status.steps.find((s) => s.stage === "build")?.state).toBe("building");
    expect(out.status.buildProgress).toEqual({ done: 2, total: 5 });
  });

  it("preserves the backend stuck step state", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        governance: {
          state: {
            mode: "strict",
            phase: "brainstorming",
            human_code_approval: "final_only",
            active_artifact_ids: {},
            updated_at: "2026-06-22T00:00:00Z",
          },
          artifacts: [],
          reviews: [],
          approvals: [],
          plan_slices: [],
        },
        status: {
          mode: "strict",
          stage: "brainstorm",
          status: "stuck",
          headline: "Brainstorm — needs you · stuck after 3 rounds",
          actor_member_id: null,
          actor_label: null,
          review_pass: null,
          needs_human: true,
          review_round: 3,
          steps: [
            { stage: "brainstorm", state: "stuck" },
            { stage: "spec", state: "pending" },
            { stage: "plan", state: "pending" },
            { stage: "build", state: "pending" },
            { stage: "done", state: "pending" },
          ],
          build_progress: null,
        },
      }),
    );

    const out = await getGovernanceFull("proj");

    expect(out.status.status).toBe("stuck");
    expect(out.status.needsHuman).toBe(true);
    expect(out.status.reviewRound).toBe(3);
    expect(out.status.steps.find((s) => s.stage === "brainstorm")?.state).toBe("stuck");
  });
});

describe("acceptGovernanceArtifact", () => {
  it("POSTs the accept route with confirm:true and a Tauri-origin header", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        governance: {
          state: { mode: "strict", phase: "drafting_spec" },
          artifacts: [],
          reviews: [],
          approvals: [],
          plan_slices: [],
        },
      }),
    );
    const g = await acceptGovernanceArtifact("proj", "a-bs-2");
    expect(g.state.phase).toBe("drafting_spec");
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/coding/projects/proj/governance/artifacts/a-bs-2/accept");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ confirm: true });
    expect(init.headers).toMatchObject({ "x-errorta-origin": "tauri-ui" });
  });

  it("maps a 409 to a clear superseded error", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ detail: { code: "artifact_superseded" } }, 409),
    );
    await expect(acceptGovernanceArtifact("proj", "a-old")).rejects.toThrow(/superseded/i);
  });

  it("maps a 400 to a governance-off error", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ detail: {} }, 400));
    await expect(acceptGovernanceArtifact("proj", "a-x")).rejects.toThrow(
      /governance is off/i,
    );
  });
});

describe("interject artifact tag", () => {
  it("includes artifact_id when provided", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ interjection: { message: "hi", at: "" } }));
    await interject("proj", "hi", "a-bs-2");
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ message: "hi", artifact_id: "a-bs-2" });
  });

  it("omits artifact_id when not provided", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ interjection: { message: "hi", at: "" } }));
    await interject("proj", "hi");
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ message: "hi" });
  });
});
