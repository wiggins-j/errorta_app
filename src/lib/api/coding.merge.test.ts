// F087-13 WS-1/WS-5 — coding.ts merge-gate + test-command adapters.
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({ sidecarFetch: vi.fn() }));
import { sidecarFetch } from "../api";
import {
  acceptWorktree,
  getTestCommands,
  getWorktreePreview,
  putTestCommands,
} from "./coding";

const mockFetch = sidecarFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

afterEach(() => vi.clearAllMocks());

describe("getWorktreePreview", () => {
  it("adapts file_diffs + gate from the backend", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        diff: "diff --git a/x b/x",
        conflicts: [],
        file_diffs: [
          { path: "x.py", oldPath: null, changeType: "added", addedLines: 3, removedLines: 0 },
        ],
        gate: {
          allowed: false,
          allowOverride: true,
          blockers: [{ code: "open_tasks", detail: "1 task(s) not done" }],
        },
      }),
    );
    const p = await getWorktreePreview("proj");
    expect(p.fileDiffs[0].path).toBe("x.py");
    expect(p.fileDiffs[0].addedLines).toBe(3);
    expect(p.gate.allowed).toBe(false);
    expect(p.gate.blockers[0].code).toBe("open_tasks");
  });

  it("adapts the F104 grounding signal", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        diff: "",
        conflicts: [],
        file_diffs: [],
        gate: { allowed: true, allowOverride: true, blockers: [] },
        grounding: { corpus_bound: true, implementer_grounded: true, policy: "warn" },
      }),
    );
    const p = await getWorktreePreview("proj");
    expect(p.grounding).toEqual({
      corpusBound: true,
      implementerGrounded: true,
      policy: "warn",
    });
  });

  it("grounding is null when the backend omits it", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        diff: "",
        conflicts: [],
        file_diffs: [],
        gate: { allowed: true, allowOverride: true, blockers: [] },
      }),
    );
    const p = await getWorktreePreview("proj");
    expect(p.grounding).toBeNull();
  });
});

describe("acceptWorktree", () => {
  it("sends confirm true and the explicit override flag", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ applied: true }));
    await acceptWorktree("proj", { override: true });
    const body = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string);
    expect(body.confirm).toBe(true);
    expect(body.override).toBe(true);
  });

  it("defaults override to false", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ applied: true }));
    await acceptWorktree("proj");
    const body = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string);
    expect(body.override).toBe(false);
  });
});

describe("test command registry", () => {
  it("round-trips snake/camel and posts a normalized payload", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ commands: { unit: { argv: ["pytest"], cwd: ".", timeout_seconds: 60 } } }),
    );
    const cmds = await getTestCommands("proj");
    expect(cmds.unit.argv).toEqual(["pytest"]);
    expect(cmds.unit.timeoutSeconds).toBe(60);

    mockFetch.mockResolvedValueOnce(jsonResponse({ commands: {} }));
    await putTestCommands("proj", { unit: { argv: ["pytest", "-q"], timeoutSeconds: 30 } });
    const body = JSON.parse((mockFetch.mock.calls[1][1] as RequestInit).body as string);
    expect(body.commands.unit.argv).toEqual(["pytest", "-q"]);
    expect(body.commands.unit.timeout_seconds).toBe(30);
  });
});
