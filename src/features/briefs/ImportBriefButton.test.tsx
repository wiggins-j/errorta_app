import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ImportBriefButton from "./ImportBriefButton";

// Mock the sidecar API helper. We don't care about other exports here.
vi.mock("../../lib/api/briefs", () => ({
  createBrief: vi.fn(),
  validateMarkdown: vi.fn(),
}));

// Mock the Tauri plugins. We hand back configurable spies on each call.
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(),
}));
vi.mock("@tauri-apps/plugin-fs", () => ({
  readTextFile: vi.fn(),
}));

import { createBrief, validateMarkdown } from "../../lib/api/briefs";
import { open as dialogOpen } from "@tauri-apps/plugin-dialog";
import { readTextFile } from "@tauri-apps/plugin-fs";

const createBriefMock = vi.mocked(createBrief);
const validateMarkdownMock = vi.mocked(validateMarkdown);
const dialogOpenMock = vi.mocked(dialogOpen);
const readTextFileMock = vi.mocked(readTextFile);

/** Default validator response: ok=true with no connectors. Tests that need
 * a failing validation override this in-test. */
function okValidation() {
  return {
    ok: true,
    errors: [],
    connectors: {},
    compliance_projection: null,
    parsed: null,
  };
}

const VALID_BRIEF = `---
project: Demo
corpus: demo
sensitivity: Public
sources: []
---

# Demo

Body.
`;

function setTauri(present: boolean) {
  if (present) {
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
  } else {
    delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  }
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  createBriefMock.mockReset();
  validateMarkdownMock.mockReset();
  validateMarkdownMock.mockResolvedValue(okValidation());
  dialogOpenMock.mockReset();
  readTextFileMock.mockReset();
});

afterEach(() => {
  setTauri(false);
});

describe("ImportBriefButton (Tauri path)", () => {
  beforeEach(() => setTauri(true));

  it("reads the selected file, POSTs to /briefs, and fires onCreated with the new id", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    dialogOpenMock.mockResolvedValue("/tmp/demo.md");
    readTextFileMock.mockResolvedValue(VALID_BRIEF);
    createBriefMock.mockResolvedValue({
      brief_id: "imported-1",
      corpus_name: "demo",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });

    render(<ImportBriefButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import/i }));
    await flush();

    expect(dialogOpenMock).toHaveBeenCalledWith(
      expect.objectContaining({
        multiple: false,
        filters: [{ name: "Markdown", extensions: ["md", "markdown"] }],
      }),
    );
    expect(readTextFileMock).toHaveBeenCalledWith("/tmp/demo.md");
    expect(createBriefMock).toHaveBeenCalledWith(VALID_BRIEF);
    expect(onCreated).toHaveBeenCalledWith("imported-1");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("leaves UI state untouched when the user cancels the file dialog", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    dialogOpenMock.mockResolvedValue(null);

    render(<ImportBriefButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import/i }));
    await flush();

    expect(readTextFileMock).not.toHaveBeenCalled();
    expect(createBriefMock).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // Button is enabled again (no stuck-loading state).
    expect(screen.getByRole("button", { name: /import/i })).not.toBeDisabled();
  });

  it("shows a friendly local error and never POSTs when frontmatter is missing", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    dialogOpenMock.mockResolvedValue("/tmp/plain.md");
    readTextFileMock.mockResolvedValue("# Just a heading, no frontmatter\n");

    render(<ImportBriefButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import/i }));
    await flush();

    expect(createBriefMock).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/yaml frontmatter/i);
  });

  it("surfaces the first field message from a structured 422 BriefParseError", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    dialogOpenMock.mockResolvedValue("/tmp/bad.md");
    readTextFileMock.mockResolvedValue(VALID_BRIEF);
    const body = JSON.stringify({
      detail: {
        errors: [
          { msg: "corpus is required", loc: ["body", "corpus"] },
          { msg: "sources must be a list", loc: ["body", "sources"] },
        ],
      },
    });
    createBriefMock.mockRejectedValue(
      new Error(`HTTP 422 on /briefs: ${body}`),
    );

    render(<ImportBriefButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import/i }));
    await flush();

    expect(createBriefMock).toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/corpus is required/i);
    // The banner is readable, not the raw "HTTP 422 on /briefs" prefix.
    expect(alert.textContent ?? "").not.toMatch(/HTTP 422/);
  });

  it("renders inline validation errors and never calls createBrief when validate-markdown returns ok=false", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    dialogOpenMock.mockResolvedValue("/tmp/demo.md");
    readTextFileMock.mockResolvedValue(VALID_BRIEF);
    // 200 response, but the validator says no.
    validateMarkdownMock.mockResolvedValue({
      ok: false,
      errors: [
        { msg: "corpus is required", loc: ["body", "corpus"] },
        { msg: "sources must be a list", loc: ["body", "sources"] },
      ],
      connectors: {},
      compliance_projection: null,
      parsed: null,
    });

    render(<ImportBriefButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import/i }));
    await flush();

    expect(validateMarkdownMock).toHaveBeenCalledWith(VALID_BRIEF);
    expect(createBriefMock).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/corpus is required/i);
    expect(alert).toHaveTextContent(/sources must be a list/i);
  });

  it("proceeds to createBrief when validate-markdown returns ok=true", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    dialogOpenMock.mockResolvedValue("/tmp/demo.md");
    readTextFileMock.mockResolvedValue(VALID_BRIEF);
    validateMarkdownMock.mockResolvedValue({
      ok: true,
      errors: [],
      connectors: { ntrs: { ok: true } },
      compliance_projection: null,
      parsed: null,
    });
    createBriefMock.mockResolvedValue({
      brief_id: "validated-1",
      corpus_name: "demo",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });

    render(<ImportBriefButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import/i }));
    await flush();

    expect(validateMarkdownMock).toHaveBeenCalledWith(VALID_BRIEF);
    expect(createBriefMock).toHaveBeenCalledWith(VALID_BRIEF);
    expect(onCreated).toHaveBeenCalledWith("validated-1");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders a readable banner for a 409 slug conflict", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    dialogOpenMock.mockResolvedValue("/tmp/dup.md");
    readTextFileMock.mockResolvedValue(VALID_BRIEF);
    const body = JSON.stringify({
      detail: "Brief with corpus 'demo' already exists",
    });
    createBriefMock.mockRejectedValue(
      new Error(`HTTP 409 on /briefs: ${body}`),
    );

    render(<ImportBriefButton onCreated={onCreated} />);
    await user.click(screen.getByRole("button", { name: /import/i }));
    await flush();

    expect(onCreated).not.toHaveBeenCalled();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/already exists/i);
    expect(alert.textContent ?? "").not.toMatch(/HTTP 409/);
  });
});

describe("ImportBriefButton (browser fallback)", () => {
  beforeEach(() => setTauri(false));

  it("uses the hidden file input when Tauri internals are unavailable", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    createBriefMock.mockResolvedValue({
      brief_id: "browser-1",
      corpus_name: "demo",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });

    render(<ImportBriefButton onCreated={onCreated} />);
    // The Tauri dialog should never be invoked in browser mode.
    await user.click(screen.getByRole("button", { name: /import/i }));
    expect(dialogOpenMock).not.toHaveBeenCalled();

    const input = screen.getByTestId("import-brief-file-input") as HTMLInputElement;
    const file = new File([VALID_BRIEF], "demo.md", { type: "text/markdown" });
    await user.upload(input, file);
    await flush();

    expect(createBriefMock).toHaveBeenCalledWith(VALID_BRIEF);
    expect(onCreated).toHaveBeenCalledWith("browser-1");
  });
});
