import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SendFeedback from "./SendFeedback";
import * as api from "../../lib/api/alpha";
import { expectNoA11yViolations } from "../council/a11y-helpers";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const PREVIEW = {
  preparedId: "prep-1",
  kind: "bug" as const,
  message: "it broke",
  bundle: { sha256: "a".repeat(64), files: ["diagnostics.json"], redaction: { ips: 2, tokens: 1 } },
};

async function openAndCompose() {
  fireEvent.click(screen.getByRole("button", { name: "Send feedback" }));
  fireEvent.change(await screen.findByLabelText("What happened?"), {
    target: { value: "it broke" },
  });
}

describe("SendFeedback", () => {
  it("requires the review step before it can send (show-before-send)", async () => {
    const previewSpy = vi.spyOn(api, "previewFeedback").mockResolvedValue(PREVIEW);
    render(<SendFeedback />);
    await openAndCompose();

    // No send button yet — must review first.
    expect(screen.queryByRole("button", { name: "Send it" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /review before sending/i }));

    await waitFor(() => expect(previewSpy).toHaveBeenCalledWith("bug", "it broke"));
    // The review shows exactly what will be sent.
    expect(screen.getByText(/exactly what will be sent/i)).toBeInTheDocument();
    expect(screen.getByText("diagnostics.json")).toBeInTheDocument();
    expect(screen.getByText(/ips: 2/)).toBeInTheDocument();
  });

  it("sends only after the tester confirms, then shows the ticket id", async () => {
    vi.spyOn(api, "previewFeedback").mockResolvedValue(PREVIEW);
    const submitSpy = vi.spyOn(api, "submitFeedback").mockResolvedValue("tkt_42");
    render(<SendFeedback />);
    await openAndCompose();
    fireEvent.click(screen.getByRole("button", { name: /review before sending/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Send it" }));

    await waitFor(() => expect(submitSpy).toHaveBeenCalledWith("prep-1"));
    expect(await screen.findByText(/your report is in/i)).toBeInTheDocument();
    expect(screen.getByText("tkt_42")).toBeInTheDocument();
  });

  it("disables review until a message is entered", async () => {
    render(<SendFeedback />);
    fireEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    expect(screen.getByRole("button", { name: /review before sending/i })).toBeDisabled();
  });

  it("surfaces a send error without leaving the review", async () => {
    vi.spyOn(api, "previewFeedback").mockResolvedValue(PREVIEW);
    vi.spyOn(api, "submitFeedback").mockRejectedValue(new Error("Load failed"));
    render(<SendFeedback />);
    await openAndCompose();
    fireEvent.click(screen.getByRole("button", { name: /review before sending/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Send it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't send/i);
  });

  it("has no serious/critical a11y violations (compose + review)", async () => {
    vi.spyOn(api, "previewFeedback").mockResolvedValue(PREVIEW);
    const { container } = render(<SendFeedback />);
    await openAndCompose();
    await expectNoA11yViolations(container);
    fireEvent.click(screen.getByRole("button", { name: /review before sending/i }));
    await screen.findByText(/exactly what will be sent/i);
    await expectNoA11yViolations(container);
  });
});
