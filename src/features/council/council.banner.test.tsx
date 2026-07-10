// CouncilRunStatusBanner — locks the new awaiting_decision label rendering.
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import CouncilRunStatusBanner from "./CouncilRunStatusBanner";

describe("CouncilRunStatusBanner", () => {
  it("renders 'Awaiting decision' label for awaiting_decision state", () => {
    render(
      <CouncilRunStatusBanner
        status={{
          runId: "r-1",
          state: "awaiting_decision",
          backendStatus: "awaiting_user_decision",
        }}
      />,
    );
    expect(screen.getByText(/Awaiting decision/i)).toBeInTheDocument();
  });

  it("renders 'Unknown state' with copyable detail for unmapped status", () => {
    render(
      <CouncilRunStatusBanner
        status={{
          runId: "r-1",
          state: "unknown",
          backendStatus: "teleporting",
        }}
      />,
    );
    expect(screen.getByText(/Unknown state/i)).toBeInTheDocument();
    expect(screen.getByText(/teleporting/)).toBeInTheDocument();
  });
});
