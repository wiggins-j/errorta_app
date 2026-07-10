import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWizardModels: vi.fn(),
  wizardStart: vi.fn(),
  wizardMessage: vi.fn(),
  wizardCreate: vi.fn(),
}));

vi.mock("../../lib/api/coding", () => ({
  getWizardModels: mocks.getWizardModels,
  wizardStart: mocks.wizardStart,
  wizardMessage: mocks.wizardMessage,
  wizardCreate: mocks.wizardCreate,
}));

import AiWizard from "./AiWizard";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const MODELS = [
  { routeId: "local.qwen", family: "qwen", providerClass: "local" },
  { routeId: "anthropic.sonnet", family: "claude", providerClass: "anthropic" },
];

describe("AiWizard", () => {
  it("nudges toward a stronger model, then drives chat → review → create", async () => {
    mocks.getWizardModels.mockResolvedValue(MODELS);
    mocks.wizardStart.mockResolvedValue({
      sessionId: "wiz-1", reply: "Hi, what are we building?", availableRoutes: MODELS });
    mocks.wizardMessage.mockResolvedValue({
      reply: "Great, ready.", ready: true,
      charter: { north_star: "Tip Split", modality: "static", entrypoint: "index.html",
        definition_of_done: "opens in a browser", team_recipe: "fast_cheap", autonomous: false },
      missing: [] });
    mocks.wizardCreate.mockResolvedValue({
      projectId: "tip-split", teamSize: 4, runSetupConfirmed: true, warnings: [] });

    const onCreated = vi.fn();
    render(<AiWizard onClose={() => {}} onCreated={onCreated} />);

    // picker shows the stronger-model recommendation
    await waitFor(() => expect(screen.getByLabelText("Wizard model")).toBeInTheDocument());
    expect(screen.getAllByText(/stronger model/i).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByText("Start"));
    await waitFor(() => expect(screen.getByText("Hi, what are we building?")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Message to the PM"), {
      target: { value: "a tip splitter" } });
    fireEvent.click(screen.getByText("Send"));
    await waitFor(() => expect(screen.getByText("Review")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Review"));
    await waitFor(() => expect(screen.getByLabelText("Project id")).toBeInTheDocument());
    // charter surfaced in the review
    expect(screen.getByText("Tip Split")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Create"));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("tip-split"));
  });

  it("does not enable Review until the plan is runnable", async () => {
    mocks.getWizardModels.mockResolvedValue(MODELS);
    mocks.wizardStart.mockResolvedValue({ sessionId: "w", reply: "hi", availableRoutes: MODELS });
    mocks.wizardMessage.mockResolvedValue({
      reply: "what modality?", ready: false, charter: { north_star: "x" },
      missing: ["modality", "entrypoint"] });
    render(<AiWizard onClose={() => {}} onCreated={() => {}} />);
    await waitFor(() => screen.getByText("Start"));
    fireEvent.click(screen.getByText("Start"));
    await waitFor(() => screen.getByLabelText("Message to the PM"));
    fireEvent.change(screen.getByLabelText("Message to the PM"), { target: { value: "hi" } });
    fireEvent.click(screen.getByText("Send"));
    await waitFor(() => expect(screen.getByText("what modality?")).toBeInTheDocument());
    expect(screen.queryByText("Review")).toBeNull();
  });
});
