// F135 — per-task assignment chip on the task card.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import CodingProjectView from "./CodingProjectView";

const project = {
  id: "p",
  northStar: "n",
  definitionOfDone: "d",
  target: "new" as const,
  status: "active",
  revision: 1,
};

afterEach(() => cleanup());

describe("CodingProjectView — assignment chip", () => {
  it("renders the model/tier/source chip from a task's model_assignment", () => {
    render(
      <CodingProjectView
        project={project}
        tasks={[
          {
            taskId: "t1",
            title: "scaffold",
            role: "dev",
            state: "doing",
            assigneeMemberId: "m",
            dependsOn: [],
            modelAssignment: {
              route_id: "claude_cli.haiku",
              difficulty_tier: "light",
              source: "pm",
              rationale: "cheapest capable route",
            },
          },
        ]}
        decisions={[]}
        artifacts={[]}
        toolEvents={[]}
      />,
    );
    expect(screen.getByText(/claude_cli\.haiku · light · pm/)).toBeInTheDocument();
  });

  it("renders no chip when the task has no assignment", () => {
    render(
      <CodingProjectView
        project={project}
        tasks={[
          {
            taskId: "t2",
            title: "unassigned",
            role: "dev",
            state: "todo",
            assigneeMemberId: null,
            dependsOn: [],
          },
        ]}
        decisions={[]}
        artifacts={[]}
        toolEvents={[]}
      />,
    );
    expect(screen.queryByText(/·.*pm|·.*selector/)).toBeNull();
  });
});
