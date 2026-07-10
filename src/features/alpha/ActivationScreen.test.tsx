import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ActivationScreen from "./ActivationScreen";
import { AlphaActivationError } from "../../lib/api/alpha";
import { SidecarUnreachableError } from "../../lib/api";
import { expectNoA11yViolations } from "../council/a11y-helpers";

afterEach(cleanup);

describe("ActivationScreen", () => {
  it("submits a trimmed code and calls onActivated on success", async () => {
    const activate = vi.fn().mockResolvedValue(undefined);
    const onActivated = vi.fn();
    render(<ActivationScreen onActivated={onActivated} activate={activate} />);

    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "  ERRT-7F3K-9Q2M " } });
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));

    await waitFor(() => expect(onActivated).toHaveBeenCalledTimes(1));
    expect(activate).toHaveBeenCalledWith("ERRT-7F3K-9Q2M");
  });

  it("maps a rejected code to friendly copy and does not call onActivated", async () => {
    const activate = vi.fn().mockRejectedValue(new AlphaActivationError("code_exhausted"));
    const onActivated = vi.fn();
    render(<ActivationScreen onActivated={onActivated} activate={activate} />);

    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "ERRT-7F3K-9Q2M" } });
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/already been used/i);
    expect(onActivated).not.toHaveBeenCalled();
  });

  it("explains that a revoked device cannot reactivate", async () => {
    const activate = vi.fn().mockRejectedValue(new AlphaActivationError("license_revoked"));
    render(<ActivationScreen onActivated={vi.fn()} activate={activate} />);
    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "ERRT-7F3K-9Q2M" } });
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/access has ended/i);
  });

  it("treats a sidecar-unreachable failure as 'still starting up' (transient), not a code rejection", async () => {
    const activate = vi.fn().mockRejectedValue(new SidecarUnreachableError());
    render(<ActivationScreen onActivated={vi.fn()} activate={activate} />);
    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "ERRT-7F3K-9Q2M" } });
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/still starting up/i);
  });

  it("surfaces the raw error code for an unmapped server rejection", async () => {
    const activate = vi.fn().mockRejectedValue(new AlphaActivationError("http_403"));
    render(<ActivationScreen onActivated={vi.fn()} activate={activate} />);
    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "ERRT-7F3K-9Q2M" } });
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/activation failed \(http_403\)/i);
  });

  it("shows a generic message for a truly unexpected error", async () => {
    const activate = vi.fn().mockRejectedValue(new Error("boom"));
    render(<ActivationScreen onActivated={vi.fn()} activate={activate} />);
    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "ERRT-7F3K-9Q2M" } });
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/activation failed\. try again/i);
  });

  it("disables Activate until a code is entered", () => {
    render(<ActivationScreen onActivated={vi.fn()} activate={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Activate" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "x" } });
    expect(screen.getByRole("button", { name: "Activate" })).toBeEnabled();
  });

  it("has no serious/critical a11y violations", async () => {
    const { container } = render(<ActivationScreen onActivated={vi.fn()} activate={vi.fn()} />);
    await expectNoA11yViolations(container);
  });
});
