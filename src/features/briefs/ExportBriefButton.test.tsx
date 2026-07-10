import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ExportBriefButton from "./ExportBriefButton";

// Mock the Tauri plugins. Dynamic imports inside the component resolve
// to these mocks at test time. In browser-mode tests setTauri(false)
// short-circuits before the dynamic import runs.
vi.mock("@tauri-apps/plugin-dialog", () => ({
  save: vi.fn(),
}));
vi.mock("@tauri-apps/plugin-fs", () => ({
  writeTextFile: vi.fn(),
}));
vi.mock("@tauri-apps/api/path", () => ({
  dirname: vi.fn(),
}));

import { save as dialogSave } from "@tauri-apps/plugin-dialog";
import { writeTextFile } from "@tauri-apps/plugin-fs";
import { dirname } from "@tauri-apps/api/path";

const dialogSaveMock = vi.mocked(dialogSave);
const writeTextFileMock = vi.mocked(writeTextFile);
const dirnameMock = vi.mocked(dirname);

const BRIEF_ID = "my-brief";
const MARKDOWN = `---
project: Demo
corpus: demo
sensitivity: Public
sources: []
---

# Demo
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
  dialogSaveMock.mockReset();
  writeTextFileMock.mockReset();
  dirnameMock.mockReset();
});

afterEach(() => {
  setTauri(false);
});

describe("ExportBriefButton (Tauri path)", () => {
  beforeEach(() => setTauri(true));

  it("writes the markdown to the selected path and fires onExported", async () => {
    const user = userEvent.setup();
    const onExported = vi.fn();
    dialogSaveMock.mockResolvedValue("/tmp/foo/my-brief.md");
    writeTextFileMock.mockResolvedValue(undefined);
    dirnameMock.mockResolvedValue("/tmp/foo");

    render(
      <ExportBriefButton
        briefId={BRIEF_ID}
        markdown={MARKDOWN}
        onExported={onExported}
      />,
    );
    await user.click(screen.getByRole("button", { name: /export/i }));
    await flush();

    expect(dialogSaveMock).toHaveBeenCalledWith(
      expect.objectContaining({
        filters: [{ name: "Brief markdown", extensions: ["md"] }],
        defaultPath: `${BRIEF_ID}.md`,
      }),
    );
    expect(writeTextFileMock).toHaveBeenCalledWith(
      "/tmp/foo/my-brief.md",
      MARKDOWN,
    );
    expect(onExported).toHaveBeenCalledWith({
      slug: BRIEF_ID,
      dir: "/tmp/foo",
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does nothing on cancel — no write, no error, no onExported", async () => {
    const user = userEvent.setup();
    const onExported = vi.fn();
    dialogSaveMock.mockResolvedValue(null);

    render(
      <ExportBriefButton
        briefId={BRIEF_ID}
        markdown={MARKDOWN}
        onExported={onExported}
      />,
    );
    await user.click(screen.getByRole("button", { name: /export/i }));
    await flush();

    expect(writeTextFileMock).not.toHaveBeenCalled();
    expect(onExported).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /export/i })).not.toBeDisabled();
  });

  it("renders an inline error banner when writeTextFile rejects", async () => {
    const user = userEvent.setup();
    const onExported = vi.fn();
    dialogSaveMock.mockResolvedValue("/tmp/foo/my-brief.md");
    writeTextFileMock.mockRejectedValue(new Error("permission denied"));

    render(
      <ExportBriefButton
        briefId={BRIEF_ID}
        markdown={MARKDOWN}
        onExported={onExported}
      />,
    );
    await user.click(screen.getByRole("button", { name: /export/i }));
    await flush();

    expect(writeTextFileMock).toHaveBeenCalled();
    expect(onExported).not.toHaveBeenCalled();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/permission denied/i);
  });
});

describe("ExportBriefButton (browser fallback)", () => {
  beforeEach(() => setTauri(false));

  it("triggers a Blob download with the brief id as the filename", async () => {
    const user = userEvent.setup();
    const onExported = vi.fn();

    const createObjectURL = vi.fn(() => "blob:fake-url");
    const revokeObjectURL = vi.fn();
    const originalCreate = URL.createObjectURL;
    const originalRevoke = URL.revokeObjectURL;
    URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL;

    // Spy on anchor.click — jsdom's default would navigate.
    let capturedAnchor: HTMLAnchorElement | null = null;
    const originalCreateElement = document.createElement.bind(document);
    const createElementSpy = vi
      .spyOn(document, "createElement")
      .mockImplementation(((tag: string) => {
        const el = originalCreateElement(tag);
        if (tag.toLowerCase() === "a") {
          capturedAnchor = el as HTMLAnchorElement;
          (el as HTMLAnchorElement).click = vi.fn();
        }
        return el;
      }) as typeof document.createElement);

    try {
      render(
        <ExportBriefButton
          briefId={BRIEF_ID}
          markdown={MARKDOWN}
          onExported={onExported}
        />,
      );
      await user.click(screen.getByRole("button", { name: /export/i }));
      await flush();

      expect(createObjectURL).toHaveBeenCalledTimes(1);
      const callArgs = createObjectURL.mock.calls[0] as unknown as [Blob];
      const blobArg = callArgs[0];
      expect(blobArg).toBeInstanceOf(Blob);
      expect(blobArg.type).toBe("text/markdown");

      expect(capturedAnchor).not.toBeNull();
      const a = capturedAnchor as unknown as HTMLAnchorElement;
      expect(a.download).toBe(`${BRIEF_ID}.md`);
      expect(a.click).toHaveBeenCalledTimes(1);

      expect(revokeObjectURL).toHaveBeenCalledWith("blob:fake-url");
      expect(onExported).toHaveBeenCalledWith({
        slug: BRIEF_ID,
        dir: "(browser download)",
      });
      expect(dialogSaveMock).not.toHaveBeenCalled();
      expect(writeTextFileMock).not.toHaveBeenCalled();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      createElementSpy.mockRestore();
      URL.createObjectURL = originalCreate;
      URL.revokeObjectURL = originalRevoke;
    }
  });
});
