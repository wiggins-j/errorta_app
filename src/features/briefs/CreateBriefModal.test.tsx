import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CreateBriefModal from "./CreateBriefModal";

vi.mock("../../lib/api/briefs", () => ({
  createBrief: vi.fn(),
  fetchBriefTemplates: vi.fn(),
}));

import { createBrief, fetchBriefTemplates } from "../../lib/api/briefs";
import type { BriefSummary, BriefTemplate } from "../../lib/api/briefs";

const createBriefMock = vi.mocked(createBrief);
const fetchBriefTemplatesMock = vi.mocked(fetchBriefTemplates);

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  createBriefMock.mockReset();
  fetchBriefTemplatesMock.mockReset();
  fetchBriefTemplatesMock.mockResolvedValue([]);
});

describe("CreateBriefModal", () => {
  it("renders all five template buttons and a textarea seeded with the Blank template", () => {
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    expect(screen.getByRole("button", { name: "Blank" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aerospace" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regulations" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Python" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Medical" })).toBeInTheDocument();

    const textarea = screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement;
    expect(textarea.value).toContain("My Project");
  });

  it("updates the textarea content when a template button is clicked", async () => {
    const user = userEvent.setup();
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Aerospace" }));
    const textarea = screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement;
    expect(textarea.value).toContain("Aerospace Mini");
  });

  it("calls createBrief and onCreated with the new brief_id on submit", async () => {
    const user = userEvent.setup();
    createBriefMock.mockResolvedValue({
      brief_id: "new-brief-id",
      corpus_name: "demo",
      state: "DRAFT",
      created_at: "2026-06-01T00:00:00Z",
      last_run_at: null,
    });
    const onCreated = vi.fn();
    render(<CreateBriefModal onCreated={onCreated} onCancel={() => {}} />);
    await user.click(screen.getByRole("button", { name: /^create$/i }));
    expect(createBriefMock).toHaveBeenCalledTimes(1);
    expect(onCreated).toHaveBeenCalledWith("new-brief-id", "demo");
  });

  it("seeds the selected corpus into template frontmatter", async () => {
    render(
      <CreateBriefModal
        onCreated={() => {}}
        onCancel={() => {}}
        initialCorpusName="pricing-spec-test"
      />,
    );
    const textarea = screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement;
    expect(textarea.value).toContain("corpus: pricing-spec-test");
  });

  it("displays an error banner when createBrief rejects", async () => {
    const user = userEvent.setup();
    createBriefMock.mockRejectedValue(new Error("server boom"));
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    await user.click(screen.getByRole("button", { name: /^create$/i }));
    expect(await screen.findByText(/server boom/)).toBeInTheDocument();
  });

  it("invokes onCancel when the backdrop is clicked (event target == currentTarget)", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <CreateBriefModal onCreated={() => {}} onCancel={onCancel} />,
    );
    const backdrop = container.querySelector(".briefs-modal-backdrop") as HTMLElement;
    expect(backdrop).not.toBeNull();
    // Fire a click whose target is the backdrop itself.
    fireEvent.click(backdrop);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("does NOT invoke onCancel when an inner element bubbles a click up", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<CreateBriefModal onCreated={() => {}} onCancel={onCancel} />);
    // Clicking the textarea bubbles to the backdrop but target !== currentTarget.
    await user.click(screen.getByLabelText(/brief markdown/i));
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("invokes onCancel when the Cancel button is clicked", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<CreateBriefModal onCreated={() => {}} onCancel={onCancel} />);
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("disables Cancel and Create while the submission is in flight", async () => {
    let resolveCreate: (v: BriefSummary) => void = () => {};
    createBriefMock.mockReturnValue(
      new Promise<BriefSummary>((res) => {
        resolveCreate = res;
      }),
    );
    const user = userEvent.setup();
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    await user.click(screen.getByRole("button", { name: /^create…|^creating…|^create$/i }));
    // While in flight, Cancel is disabled and Create label flips to "Creating…".
    expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /creating…/i })).toBeDisabled();
    await act(async () => {
      resolveCreate({
        brief_id: "x",
        corpus_name: "demo",
        state: "DRAFT",
        created_at: "2026-06-01T00:00:00Z",
        last_run_at: null,
      });
    });
  });

  it("merges fetched remote templates with Blank first in the list", async () => {
    const remote: BriefTemplate[] = [
      {
        id: "aerospace",
        title: "Aerospace Remote",
        description: "Remote description",
        markdown: "remote-aerospace-body",
        markdown_preview: "preview",
        mtime: 0,
      },
      {
        id: "custom-remote",
        title: "Custom Remote",
        description: "",
        markdown: "remote-custom-body",
        markdown_preview: "preview",
        mtime: 0,
      },
    ];
    fetchBriefTemplatesMock.mockResolvedValue(remote);
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    await flush();
    // Blank must remain first
    const buttons = screen
      .getAllByRole("button")
      .filter((b) =>
        ["Blank", "Aerospace Remote", "Custom Remote", "Regulations", "Python", "Medical"].includes(
          b.textContent ?? "",
        ),
      );
    expect(buttons[0].textContent).toBe("Blank");
    expect(screen.getByRole("button", { name: "Aerospace Remote" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Custom Remote" })).toBeInTheDocument();
  });

  it("seeds the textarea from a remote template when initialTemplateId matches a remote id", async () => {
    const remote: BriefTemplate[] = [
      {
        id: "custom-remote",
        title: "Custom Remote",
        description: "",
        markdown: "REMOTE_BODY_CONTENT",
        markdown_preview: "preview",
        mtime: 0,
      },
    ];
    fetchBriefTemplatesMock.mockResolvedValue(remote);
    render(
      <CreateBriefModal
        onCreated={() => {}}
        onCancel={() => {}}
        initialTemplateId="custom-remote"
      />,
    );
    await flush();
    const textarea = screen.getByLabelText(/brief markdown/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("REMOTE_BODY_CONTENT");
  });

  it("falls back to FALLBACK_ENTRIES when fetchBriefTemplates rejects", async () => {
    fetchBriefTemplatesMock.mockRejectedValue(new Error("offline"));
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    await flush();
    // Built-in fallback labels remain visible.
    expect(screen.getByRole("button", { name: "Blank" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aerospace" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regulations" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Python" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Medical" })).toBeInTheDocument();
    // No error banner is shown — fallback is silent.
    expect(screen.queryByText(/offline/)).not.toBeInTheDocument();
  });

  it("renders Creating… while a slow createBrief promise is pending without crashing", async () => {
    let resolveCreate: (v: BriefSummary) => void = () => {};
    createBriefMock.mockReturnValue(
      new Promise<BriefSummary>((res) => {
        resolveCreate = res;
      }),
    );
    const user = userEvent.setup();
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    await user.click(screen.getByRole("button", { name: /^create$/i }));
    expect(screen.getByRole("button", { name: /creating…/i })).toBeInTheDocument();
    // Verify nothing crashed during the pending window
    expect(screen.getByLabelText(/brief markdown/i)).toBeInTheDocument();
    await act(async () => {
      resolveCreate({
        brief_id: "z",
        corpus_name: "demo",
        state: "DRAFT",
        created_at: "2026-06-01T00:00:00Z",
        last_run_at: null,
      });
    });
  });

  it("renders String(err) for non-Error throws from createBrief", async () => {
    const user = userEvent.setup();
    // Throw a non-Error to exercise the `String(err)` branch.
    createBriefMock.mockRejectedValue("plain-string-failure");
    render(<CreateBriefModal onCreated={() => {}} onCancel={() => {}} />);
    await user.click(screen.getByRole("button", { name: /^create$/i }));
    expect(await screen.findByText(/plain-string-failure/)).toBeInTheDocument();
  });
});
