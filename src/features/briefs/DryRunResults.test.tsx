import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DryRunResults from "./DryRunResults";
import type { DryRunSourceProjection } from "../../lib/api/briefs";

function makeProjection(
  overrides: Partial<DryRunSourceProjection> = {},
): DryRunSourceProjection {
  return {
    connector_name: "arxiv-connector",
    candidates_seen: 100,
    compliance_pass: 90,
    compliance_refused: 10,
    sample_refusal_reasons: [],
    ...overrides,
  };
}

describe("DryRunResults", () => {
  it("renders nothing when projections is null", () => {
    const { container } = render(<DryRunResults projections={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when projections is undefined", () => {
    const { container } = render(<DryRunResults projections={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when projections is an empty object", () => {
    const { container } = render(<DryRunResults projections={{}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a ProjectionCard with the source name and connector name", () => {
    render(
      <DryRunResults
        projections={{
          arxiv: makeProjection({ connector_name: "ArxivConnectorV1" }),
        }}
      />,
    );
    expect(screen.getByText("arxiv")).toBeInTheDocument();
    expect(screen.getByText("ArxivConnectorV1")).toBeInTheDocument();
    expect(screen.getByLabelText(/dry-run projection/i)).toBeInTheDocument();
  });

  it("classifies pass% >= 80 as the good band (#1b8f3a)", () => {
    render(
      <DryRunResults
        projections={{
          arxiv: makeProjection({ compliance_pass: 80, compliance_refused: 20, candidates_seen: 100 }),
        }}
      />,
    );
    const passEl = screen.getByTestId("dryrun-pass-arxiv");
    expect(passEl).toHaveStyle({ color: "#1b8f3a" });
  });

  it("classifies pass% between 40 and 79 as the warn band (#b88217)", () => {
    render(
      <DryRunResults
        projections={{
          arxiv: makeProjection({ compliance_pass: 50, compliance_refused: 50, candidates_seen: 100 }),
        }}
      />,
    );
    const passEl = screen.getByTestId("dryrun-pass-arxiv");
    expect(passEl).toHaveStyle({ color: "#b88217" });
  });

  it("classifies pass% < 40 as the bad band (#b3261e)", () => {
    render(
      <DryRunResults
        projections={{
          arxiv: makeProjection({ compliance_pass: 10, compliance_refused: 90, candidates_seen: 100 }),
        }}
      />,
    );
    const passEl = screen.getByTestId("dryrun-pass-arxiv");
    expect(passEl).toHaveStyle({ color: "#b3261e" });
  });

  it("renders singular 'candidate' label when candidates_seen is exactly 1", () => {
    render(
      <DryRunResults
        projections={{
          src: makeProjection({ candidates_seen: 1, compliance_pass: 1, compliance_refused: 0 }),
        }}
      />,
    );
    expect(screen.getByText(/^1 candidate sampled$/)).toBeInTheDocument();
  });

  it("renders plural 'candidates' label when candidates_seen is not 1", () => {
    render(
      <DryRunResults
        projections={{
          src: makeProjection({ candidates_seen: 5 }),
        }}
      />,
    );
    expect(screen.getByText(/^5 candidates sampled$/)).toBeInTheDocument();
  });

  it("does NOT render a refusal-reasons toggle when sample_refusal_reasons is empty", () => {
    render(
      <DryRunResults
        projections={{
          src: makeProjection({ sample_refusal_reasons: [] }),
        }}
      />,
    );
    expect(screen.queryByTestId("dryrun-toggle-src")).not.toBeInTheDocument();
    expect(screen.queryByTestId("dryrun-reasons-src")).not.toBeInTheDocument();
  });

  it("toggles the refusal reasons list open and closed", async () => {
    const user = userEvent.setup();
    render(
      <DryRunResults
        projections={{
          src: makeProjection({
            sample_refusal_reasons: ["bad mime", "too large"],
          }),
        }}
      />,
    );
    // Closed by default — reasons list not rendered.
    expect(screen.queryByTestId("dryrun-reasons-src")).not.toBeInTheDocument();
    const toggle = screen.getByTestId("dryrun-toggle-src");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle.textContent).toMatch(/^Show sample refusal reasons \(2\)$/);

    await user.click(toggle);
    expect(screen.getByTestId("dryrun-reasons-src")).toBeInTheDocument();
    expect(screen.getByText("bad mime")).toBeInTheDocument();
    expect(screen.getByText("too large")).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle.textContent).toMatch(/^Hide sample refusal reasons \(2\)$/);

    await user.click(toggle);
    expect(screen.queryByTestId("dryrun-reasons-src")).not.toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("renders one card per source key in the projections object", () => {
    render(
      <DryRunResults
        projections={{
          arxiv: makeProjection(),
          nasa: makeProjection({ connector_name: "NasaNtrs" }),
          faa: makeProjection({ connector_name: "FaaRegs" }),
        }}
      />,
    );
    expect(screen.getByTestId("dryrun-card-arxiv")).toBeInTheDocument();
    expect(screen.getByTestId("dryrun-card-nasa")).toBeInTheDocument();
    expect(screen.getByTestId("dryrun-card-faa")).toBeInTheDocument();
  });
});
