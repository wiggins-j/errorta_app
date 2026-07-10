import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, it } from "vitest";

import CodingProjectView from "./CodingProjectView";
import { expectNoA11yViolations } from "../council/a11y-helpers";

afterEach(() => cleanup());

describe("CodingProjectView a11y", () => {
  it("has no serious/critical axe violations", async () => {
    const { container } = render(
      <CodingProjectView
        project={{ id: "p", northStar: "n", definitionOfDone: "d", target: "new", status: "active", revision: 1 }}
        tasks={[{ taskId: "t1", title: "impl", role: "dev", state: "doing", assigneeMemberId: "m", dependsOn: [] }]}
        decisions={[]}
        artifacts={[]}
        toolEvents={[]}
      />,
    );
    await expectNoA11yViolations(container);
  });
});
