import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  runSetupPreflight: vi.fn(),
  confirmRunSetup: vi.fn(),
  getCliLoginCommand: vi.fn(),
}));

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    runSetupPreflight: mocks.runSetupPreflight,
    confirmRunSetup: mocks.confirmRunSetup,
  };
});

vi.mock("../../lib/api/providerKeys", () => ({
  getCliLoginCommand: mocks.getCliLoginCommand,
}));

import RunSetupGate from "./RunSetupGate";
import type { RunSetupGateProps } from "./RunSetupGate";
import {
  AUTONOMOUS_PRESET,
  CAREFUL_PRESET,
  RUN_SETUP_COVERED_FIELDS,
  withRunSetupDefaults,
} from "./runSetupPresets";
import type { CouncilRoomSummary } from "../council/types";

const ROOMS: CouncilRoomSummary[] = [
  { id: "room-1", name: "Build team", updatedAt: "", revision: 1, statusHint: "" },
];

function props(over: Partial<RunSetupGateProps> = {}): RunSetupGateProps {
  return {
    projectId: "p1",
    rooms: ROOMS,
    teamRoomId: "room-1",
    onTeamRoomChange: vi.fn(),
    initialConfig: { ...CAREFUL_PRESET },
    onClose: vi.fn(),
    onConfirmed: vi.fn(),
    ...over,
  };
}

beforeEach(() => {
  mocks.runSetupPreflight.mockResolvedValue([]); // healthy by default
  mocks.confirmRunSetup.mockResolvedValue(true);
  mocks.getCliLoginCommand.mockResolvedValue({
    loginArgv: ["claude", "setup-token"],
    installUrl: "",
    installCommand: "",
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RunSetupGate (F121 Part B)", () => {
  it("pre-fills from the active preset and labels it Careful", async () => {
    render(<RunSetupGate {...props()} />);
    await waitFor(() => expect(mocks.runSetupPreflight).toHaveBeenCalled());
    expect(screen.getByTestId("active-preset")).toHaveTextContent("careful");
    // The "Human in the loop" (governance mode) select reflects the Careful preset.
    expect(
      (screen.getByRole("combobox", { name: /Human in the loop/i }) as HTMLSelectElement).value,
    ).toBe("light");
    const approval = screen.getByRole("combobox", {
      name: "Human code approval",
    }) as HTMLSelectElement;
    expect(approval.value).toBe("per_milestone");
    expect(Array.from(approval.options, (option) => option.value)).toEqual([
      "none",
      "per_slice",
      "per_milestone",
      "final_only",
    ]);
  });

  it("flips the label to Custom when a field is edited (values kept)", async () => {
    render(<RunSetupGate {...props()} />);
    await waitFor(() => expect(mocks.runSetupPreflight).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("spinbutton", { name: /Max iterations/i }), {
      target: { value: "999" },
    });
    expect(screen.getByTestId("active-preset")).toHaveTextContent("custom");
    // The edited value is preserved (not reset by the relabel).
    expect((screen.getByRole("spinbutton", { name: /Max iterations/i }) as HTMLInputElement).value).toBe(
      "999",
    );
  });

  it("surfaces every covered setting in the gate (criterion 9)", async () => {
    render(<RunSetupGate {...props()} />);
    await waitFor(() => expect(mocks.runSetupPreflight).toHaveBeenCalled());
    // List-driven: each covered field must have a labeled control in the DOM, so
    // a new knob can't be added to RunSetupConfig without surfacing it here.
    // Exact label strings (not partials) so a covered field maps to exactly one
    // control — the assertion is "this knob is surfaced", one-to-one.
    const labelFor: Record<(typeof RUN_SETUP_COVERED_FIELDS)[number], string> = {
      governanceMode: "Human in the loop",
      blockOnProblems: "Block on showstoppers",
      humanCodeApproval: "Human code approval",
      maxReviewRounds: "Max review rounds",
      checkpointCadence: "Cadence",
      checkpointN: "Every N tasks",
      guardrailEnabled: "On",
      maxIterations: "Max iterations",
      maxModelCalls: "Max model calls",
      maxParallelWorkers: "Max parallel workers",
      memberFailureLimit: "Member failure limit",
      preflightEnabled: "Provider auth preflight",
    };
    for (const field of RUN_SETUP_COVERED_FIELDS) {
      expect(
        screen.getByLabelText(labelFor[field], { exact: true }),
        `covered field "${field}" must render a control`,
      ).toBeInTheDocument();
    }
  });

  it("disables Ready to run while a required member is unauthenticated, then enables on re-check", async () => {
    mocks.runSetupPreflight
      .mockResolvedValueOnce([
        {
          provider: "claude_cli",
          route: "claude_cli.opus",
          reason: "auth_failed",
          detail: "not logged in",
          remediation: "Run the login command for this provider …",
          memberIds: ["m-pm"],
        },
      ])
      .mockResolvedValueOnce([]); // healthy after the user logs in + re-checks

    render(<RunSetupGate {...props()} />);

    // First preflight: logged-out -> badge + corrected login command + ready off.
    await waitFor(() =>
      expect(screen.getByText(/not logged in/i)).toBeInTheDocument(),
    );
    expect(screen.getByText("claude setup-token")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Ready to run/i })).toBeDisabled();

    // Re-check after login -> healthy -> ready enabled.
    fireEvent.click(screen.getByRole("button", { name: /Re-check/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Ready to run/i })).toBeEnabled(),
    );
  });

  it("keeps Ready to run disabled until the current team preflight completes", async () => {
    let resolvePreflight: (value: unknown[]) => void = () => {};
    mocks.runSetupPreflight.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePreflight = resolve;
      }),
    );

    render(<RunSetupGate {...props()} />);

    expect(screen.getByRole("button", { name: /Ready to run/i })).toBeDisabled();
    resolvePreflight([]);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Ready to run/i })).toBeEnabled(),
    );
  });

  it("confirms with the resolved config and fires onConfirmed", async () => {
    const onConfirmed = vi.fn();
    render(<RunSetupGate {...props({ onConfirmed })} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Ready to run/i })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Ready to run/i }));
    await waitFor(() => expect(mocks.confirmRunSetup).toHaveBeenCalled());
    const [, cfg] = mocks.confirmRunSetup.mock.calls[0];
    expect(cfg.teamRoomId).toBe("room-1");
    expect(cfg.governanceMode).toBe("light");
    expect(onConfirmed).toHaveBeenCalled();
  });

  it("applies untouched settings from a sparse seed (no phantom defaults)", async () => {
    // Regression: a seed missing governanceMode used to render the dropdown's
    // display fallback ("light") while the wire skipped the undefined field, so
    // confirm silently DID NOT apply governance — the backend kept its "off"
    // default. The gate must normalize the seed so what it shows is sent.
    const sparse = { teamRoomId: "room-1" } as Partial<typeof CAREFUL_PRESET>;
    render(<RunSetupGate {...props({ initialConfig: sparse })} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Ready to run/i })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Ready to run/i }));
    await waitFor(() => expect(mocks.confirmRunSetup).toHaveBeenCalled());
    const [, cfg] = mocks.confirmRunSetup.mock.calls[0];
    // Untouched settings are concrete + sent, not undefined.
    expect(cfg.governanceMode).toBe("light");
    expect(cfg.blockOnProblems).toBe(true);
    expect(cfg.checkpointCadence).toBe("per_milestone");
    for (const f of RUN_SETUP_COVERED_FIELDS) {
      expect(cfg[f], `${f} must be sent, not silently dropped`).not.toBeUndefined();
    }
  });
});

describe("runSetupPresets coverage contract", () => {
  it("every preset pins every covered field", () => {
    for (const [name, preset] of Object.entries({
      Careful: CAREFUL_PRESET,
      Autonomous: AUTONOMOUS_PRESET,
    })) {
      for (const field of RUN_SETUP_COVERED_FIELDS) {
        expect(preset[field], `${name} must pin ${field}`).not.toBeUndefined();
      }
    }
  });

  it("withRunSetupDefaults fills every undefined covered field (no phantom)", () => {
    const filled = withRunSetupDefaults({ teamRoomId: "room-1" });
    for (const field of RUN_SETUP_COVERED_FIELDS) {
      expect(filled[field], `${field} must be filled`).toBe(CAREFUL_PRESET[field]);
    }
    expect(filled.teamRoomId).toBe("room-1"); // preserved, not a covered field
    // A defined field is NOT overwritten.
    expect(withRunSetupDefaults({ governanceMode: "strict" }).governanceMode).toBe("strict");
  });
});
