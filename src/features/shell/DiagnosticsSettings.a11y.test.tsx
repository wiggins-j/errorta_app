import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, it, vi } from "vitest";
import { expectNoA11yViolations } from "../council/a11y-helpers";

vi.mock("../../lib/api/settings", () => ({
  getSettings: vi.fn(),
  setLogLevel: vi.fn(),
}));

vi.mock("../../lib/api/diagnosticsLog", () => ({
  tailLog: vi.fn(),
  streamLog: vi.fn(),
}));

import { streamLog, tailLog } from "../../lib/api/diagnosticsLog";
import { getSettings, setLogLevel } from "../../lib/api/settings";
import { DiagnosticsSettings } from "./DiagnosticsSettings";

const mockedGetSettings = getSettings as unknown as ReturnType<typeof vi.fn>;
const mockedSetLogLevel = setLogLevel as unknown as ReturnType<typeof vi.fn>;
const mockedTailLog = tailLog as unknown as ReturnType<typeof vi.fn>;
const mockedStreamLog = streamLog as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockedGetSettings.mockResolvedValue({ log_level: "info" });
  mockedSetLogLevel.mockResolvedValue({ log_level: "debug" });
  mockedTailLog.mockResolvedValue(["INFO errorta: ready"]);
  mockedStreamLog.mockResolvedValue({ close: vi.fn() });
});

describe("DiagnosticsSettings a11y", () => {
  it("has no serious or critical axe violations collapsed or expanded", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <main>
        <DiagnosticsSettings />
      </main>,
    );

    await screen.findByLabelText("Debug logging");
    await expectNoA11yViolations(container);

    await user.click(screen.getByText("Live log"));
    await screen.findByRole("log", { name: "Live sidecar log" });
    await screen.findByText(/INFO errorta: ready/);
    await expectNoA11yViolations(container);
  });
});
