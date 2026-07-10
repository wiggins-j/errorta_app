// F143-01 Slice F — MemberContextReport renders the Layer-1 composition bars and,
// for a CLI-backed member, the Layer-2 caveat note + the vendor-overhead band. A
// direct-API member shows neither.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return { ...actual, getTurnComposition: vi.fn() };
});

import { getTurnComposition, type TurnComposition } from "../../lib/api/coding";
import MemberContextReport from "./MemberContextReport";

const mockGet = getTurnComposition as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const COMPOSITION = {
  sentTotal: 4200,
  categories: [
    { class_: "role_instructions", tokens: 380 },
    { class_: "work_request", tokens: 1200 },
    { class_: "project_context", tokens: 900 },
    { class_: "repo_snapshot", tokens: 1400 },
    { class_: "pr_diff", tokens: 320 },
  ],
  estimatorMethod: "calibrated_heuristic_v1",
};

function cliData(): TurnComposition {
  return {
    composition: COMPOSITION,
    cliOverheadTokens: 3800,
    note:
      "This shows what Errorta sent into the claude_cli.sonnet CLI. The CLI adds its " +
      "own system prompt, tools, and skills on top (~3800 tokens, vendor-managed) " +
      "that Errorta can't itemize (Layer-2, not shown here).",
  };
}

function directData(): TurnComposition {
  return { composition: COMPOSITION, cliOverheadTokens: null, note: null };
}

describe("MemberContextReport", () => {
  it("renders a labeled bar per category with token counts (prop-provided)", () => {
    render(
      <MemberContextReport
        projectId="p"
        taskId="t"
        turnId="trn-1"
        label="dev"
        composition={directData()}
      />,
    );
    // every category label + its token count renders
    expect(screen.getByText("Role instructions")).toBeInTheDocument();
    expect(screen.getByText("Task / work request")).toBeInTheDocument();
    expect(screen.getByText("Project context (retrieved)")).toBeInTheDocument();
    expect(screen.getByText("Repo snapshot")).toBeInTheDocument();
    expect(screen.getByText("PR diff")).toBeInTheDocument();
    // the sent total is shown in the header
    expect(screen.getByText(/4,200 tokens/)).toBeInTheDocument();
    // one proportional fill bar per category
    const fills = document.querySelectorAll(".coding-ctxreport-fill");
    expect(fills.length).toBe(COMPOSITION.categories.length);
  });

  it("marks the retrieved project-context category as the hero", () => {
    render(
      <MemberContextReport
        projectId="p"
        taskId="t"
        turnId="trn-1"
        composition={directData()}
      />,
    );
    const hero = document.querySelector(".coding-ctxreport-hero");
    expect(hero).not.toBeNull();
    expect(hero?.textContent).toContain("Project context (retrieved)");
  });

  it("renders the Layer-2 CLI caveat note + vendor-overhead band for a CLI member", () => {
    render(
      <MemberContextReport
        projectId="p"
        taskId="t"
        turnId="trn-1"
        composition={cliData()}
      />,
    );
    // the note names the route + overhead
    expect(screen.getByText(/claude_cli\.sonnet CLI/)).toBeInTheDocument();
    // the distinct vendor-added band renders with the overhead magnitude
    const band = document.querySelector(".coding-ctxreport-layer2-band");
    expect(band).not.toBeNull();
    expect(band?.textContent).toContain("CLI-added context");
    expect(band?.textContent).toContain("3,800");
  });

  it("does NOT render the Layer-2 note/band for a direct-API member", () => {
    render(
      <MemberContextReport
        projectId="p"
        taskId="t"
        turnId="trn-1"
        composition={directData()}
      />,
    );
    expect(document.querySelector(".coding-ctxreport-layer2")).toBeNull();
    expect(document.querySelector(".coding-ctxreport-layer2-band")).toBeNull();
    expect(screen.queryByText(/vendor-managed/)).toBeNull();
  });

  it("fetches its own composition when none is provided", async () => {
    mockGet.mockResolvedValueOnce(cliData());
    render(<MemberContextReport projectId="p" taskId="t" turnId="trn-42" />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("p", "t", "trn-42"));
    await screen.findByText("Role instructions");
    expect(document.querySelector(".coding-ctxreport-layer2-band")).not.toBeNull();
  });
});
