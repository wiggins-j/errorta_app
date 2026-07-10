import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Mock the API module: CodingFileUpdateError must stay a real class so
// `instanceof` checks in FileEditor work.
const mocks = vi.hoisted(() => ({
  getFile: vi.fn(),
  updateFile: vi.fn(),
}));

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    getFile: mocks.getFile,
    updateFile: mocks.updateFile,
  };
});

// Stub CodeMirror with a plain textarea so the test exercises FileEditor's logic
// (save/dirty/disabled/stale) without the editor's DOM weight in happy-dom.
vi.mock("@uiw/react-codemirror", () => ({
  default: ({
    value,
    onChange,
    editable,
    readOnly,
    ["aria-label"]: ariaLabel,
  }: {
    value: string;
    onChange?: (v: string) => void;
    editable?: boolean;
    readOnly?: boolean;
    ["aria-label"]?: string;
  }) => (
    <textarea
      aria-label={ariaLabel}
      value={value}
      readOnly={Boolean(readOnly) || editable === false}
      onChange={(e) => onChange?.(e.target.value)}
    />
  ),
}));

// Language grammars are dynamically imported; stub them so no real grammar loads.
vi.mock("@codemirror/lang-python", () => ({ python: () => [] }));

import { CodingFileUpdateError, type CodingFile } from "../../lib/api/coding";
import FileEditor from "./FileEditor";

const TEXT_FILE: CodingFile = {
  path: "src/app.py",
  content: "print('hi')\n",
  truncated: false,
  encoding: "utf-8",
  bytes: 12,
  onMaster: true,
  contentSha256: "a".repeat(64),
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FileEditor", () => {
  it("loads content and shows a dirty indicator after an edit", async () => {
    render(<FileEditor projectId="p1" file={TEXT_FILE} running={false} onSaved={() => {}} />);
    const editor = screen.getByLabelText("Edit file src/app.py") as HTMLTextAreaElement;
    expect(editor.value).toBe("print('hi')\n");
    expect(screen.getByText("No unsaved changes")).toBeInTheDocument();

    fireEvent.change(editor, { target: { value: "print('bye')\n" } });
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("Save calls updateFile with the expected sha and clears dirty", async () => {
    const onSaved = vi.fn();
    mocks.updateFile.mockResolvedValue({
      path: "src/app.py",
      contentSha256: "b".repeat(64),
      bytes: 13,
      head: "deadbeef",
      onMaster: true,
    });
    render(<FileEditor projectId="p1" file={TEXT_FILE} running={false} onSaved={onSaved} />);
    const editor = screen.getByLabelText("Edit file src/app.py");
    fireEvent.change(editor, { target: { value: "print('bye')\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mocks.updateFile).toHaveBeenCalledWith(
        "p1",
        "src/app.py",
        "print('bye')\n",
        "a".repeat(64),
      ),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("No unsaved changes")).toBeInTheDocument());
  });

  it("disables Save while a run is active", () => {
    render(<FileEditor projectId="p1" file={TEXT_FILE} running={true} onSaved={() => {}} />);
    const editor = screen.getByLabelText("Edit file src/app.py");
    // Even after typing, Save stays disabled during a run.
    fireEvent.change(editor, { target: { value: "changed\n" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(
      screen.getByText(/Saving is disabled while a Coding run is active/),
    ).toBeInTheDocument();
  });

  it("keeps Save disabled with no changes", () => {
    render(<FileEditor projectId="p1" file={TEXT_FILE} running={false} onSaved={() => {}} />);
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("is read-only and Save disabled for a truncated file", () => {
    render(
      <FileEditor
        projectId="p1"
        file={{ ...TEXT_FILE, truncated: true }}
        running={false}
        onSaved={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText("Edit file src/app.py"), {
      target: { value: "edited\n" },
    });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("shows a reload prompt when the save is stale (409)", async () => {
    mocks.updateFile.mockRejectedValue(
      new CodingFileUpdateError("stale_file", "changed under you", "c".repeat(64)),
    );
    render(<FileEditor projectId="p1" file={TEXT_FILE} running={false} onSaved={() => {}} />);
    fireEvent.change(screen.getByLabelText("Edit file src/app.py"), {
      target: { value: "print('bye')\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent(/changed since you opened it/i);

    // Reload re-fetches the committed file.
    mocks.getFile.mockResolvedValue({
      ...TEXT_FILE,
      content: "print('latest')\n",
      contentSha256: "c".repeat(64),
    });
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(mocks.getFile).toHaveBeenCalledWith("p1", "src/app.py"));
  });
});
