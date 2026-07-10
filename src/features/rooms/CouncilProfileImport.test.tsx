import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

vi.mock("../../lib/api/councilProfile", () => ({
  listProfileExamples: vi.fn(),
  validateProfile: vi.fn(),
}));
vi.mock("../../lib/api/councilRoom", () => ({
  createRoom: vi.fn(),
}));

import {
  listProfileExamples,
  validateProfile,
} from "../../lib/api/councilProfile";
import { createRoom } from "../../lib/api/councilRoom";
import CouncilProfileImport from "./CouncilProfileImport";

const _examples = listProfileExamples as unknown as ReturnType<typeof vi.fn>;
const _validate = validateProfile as unknown as ReturnType<typeof vi.fn>;
const _create = createRoom as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CouncilProfileImport", () => {
  it("previews a profile and surfaces warnings, then creates a draft room", async () => {
    _examples.mockResolvedValue([
      { slug: "coding-council", profile: {}, yaml: "name: Coding Council\n" },
    ]);
    _validate.mockResolvedValue({
      room: {
        id: "room-draft",
        name: "Coding Council",
        description: "propose-only",
        members: [{ id: "p", name: "Programmer" }],
      },
      validation: {
        ok: true,
        errors: [],
        missing_providers: [],
        requested_tools: ["code_read"],
        missing_tools: ["code_exec"],
        warnings: [
          { code: "tool_not_available", detail: "profile requests tool 'code_exec' ..." },
        ],
      },
    });
    _create.mockResolvedValue({ room: { id: "room-draft" }, validation: {} });
    const onCreated = vi.fn();

    render(<CouncilProfileImport onClose={vi.fn()} onCreated={onCreated} />);

    // Load the example into the textarea.
    await waitFor(() => screen.getByTestId("example-coding-council"));
    fireEvent.click(screen.getByTestId("example-coding-council"));
    fireEvent.click(screen.getByTestId("preview-profile"));

    // Preview shows the member + the warning.
    await waitFor(() => screen.getByText(/Coding Council/));
    expect(screen.getByText(/Programmer/)).toBeInTheDocument();
    expect(screen.getByText(/code_exec/)).toBeInTheDocument();

    // Create the draft.
    fireEvent.click(screen.getByTestId("create-draft"));
    await waitFor(() => expect(_create).toHaveBeenCalled());
    expect(onCreated).toHaveBeenCalledWith("room-draft");
  });

  it("shows an error when preview validation fails", async () => {
    _examples.mockResolvedValue([]);
    _validate.mockRejectedValue(new Error("invalid_yaml"));
    render(<CouncilProfileImport onClose={vi.fn()} onCreated={vi.fn()} />);
    fireEvent.change(screen.getByTestId("profile-yaml"), {
      target: { value: "{bad" },
    });
    fireEvent.click(screen.getByTestId("preview-profile"));
    await waitFor(() => screen.getByRole("alert"));
    expect(screen.getByText(/invalid_yaml/)).toBeInTheDocument();
  });
});
