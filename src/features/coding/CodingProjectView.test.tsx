import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/api/coding", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api/coding")>();
  return {
    ...actual,
    getFile: vi.fn(),
    getPmChat: vi.fn().mockResolvedValue([]),
    pmAsk: vi.fn(),
  };
});

// F105: the Files-touched panel now embeds the CodeMirror editor for eligible
// text files. Stub it with a plain textarea so these presentational tests stay
// fast and DOM-light; the editor's own logic is covered in FileEditor.test.tsx.
vi.mock("@uiw/react-codemirror", () => ({
  default: ({
    value,
    onChange,
    ["aria-label"]: ariaLabel,
  }: {
    value: string;
    onChange?: (v: string) => void;
    ["aria-label"]?: string;
  }) => (
    <textarea aria-label={ariaLabel} value={value} onChange={(e) => onChange?.(e.target.value)} />
  ),
}));

import { getFile } from "../../lib/api/coding";
import CodingProjectView from "./CodingProjectView";
import type { CodingProjectViewProps } from "./CodingProjectView";

const getFileMock = vi.mocked(getFile);

beforeEach(() => {
  getFileMock.mockResolvedValue({
    path: "src/cli.py",
    content: "entry",
    truncated: false,
    encoding: "utf-8",
    bytes: 5,
    onMaster: true,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function props(over: Partial<CodingProjectViewProps> = {}): CodingProjectViewProps {
  return {
    project: { id: "todo-app", northStar: "Build a todo CLI", definitionOfDone: "tests pass",
               target: "new", status: "active", revision: 1 },
    tasks: [
      { taskId: "t1", title: "impl add", role: "dev", state: "doing", assigneeMemberId: "m-dev", dependsOn: [] },
      { taskId: "t2", title: "review add", role: "reviewer", state: "todo", assigneeMemberId: null, dependsOn: ["t1"] },
      { taskId: "t3", title: "scaffold", role: "dev", state: "done", assigneeMemberId: "m-dev", dependsOn: [] },
    ],
    decisions: [{ decisionId: "d1", title: "use argparse", choice: "argparse", rationale: "stdlib", relatedTaskIds: [] }],
    artifacts: [{ path: "src/cli.py", status: "created", summary: "entry", onMaster: true }],
    toolEvents: [{
      eventId: "te1",
      taskId: "t1",
      memberId: "m-dev",
      role: "dev",
      tool: "code_write",
      status: "succeeded",
      path: "src/cli.py",
      error: null,
    }],
    ...over,
  };
}

describe("CodingProjectView test-command editor", () => {
  it("does not render the Test Commands panel (feature-flagged off), and renames Merge", () => {
    render(<CodingProjectView {...props()} />);
    // The Test Commands editor is scrapped for now (TEST_COMMANDS_ENABLED=false);
    // the tester still runs registered commands, but the panel is hidden.
    expect(screen.queryByText("Test Commands")).toBeNull();
    expect(screen.getByRole("heading", { name: "Merge" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Merge back" })).toBeNull();
  });

  it("renders the run-log turns and triggers a transcript download", () => {
    const onDownload = vi.fn();
    render(
      <CodingProjectView
        {...props({
          turns: [
            {
              turnId: "trn1", role: "dev", memberId: "m-dev", taskId: "t1",
              prompt: "PROMPT_X", response: "RESPONSE_Y", outcome: "task_done",
              reason: "", parseOk: true, durationMs: 42, at: "",
            },
          ],
          onDownloadRunLog: onDownload,
        })}
      />,
    );
    expect(screen.getByText("Run log")).toBeInTheDocument();
    expect(screen.getByText("1 turns")).toBeInTheDocument();
    expect(screen.getByText("PROMPT_X")).toBeInTheDocument();
    expect(screen.getByText("RESPONSE_Y")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Download full transcript/ }));
    expect(onDownload).toHaveBeenCalled();
  });

  it("renders pull requests with review/test/merge state", () => {
    render(
      <CodingProjectView
        {...props({
          prs: [
            { prId: "pr1", taskId: "t1", branch: "task-t1", status: "merged",
              reviewerApproved: true, testsPassed: true, conflicts: [], reviewFindings: [],
              createdAt: "2026-06-18T10:00:00Z", updatedAt: "2026-06-18T10:30:00Z" },
            { prId: "pr2", taskId: "t2", branch: "task-t2", status: "changes_requested",
              reviewerApproved: false, testsPassed: null, conflicts: [], reviewFindings: [],
              createdAt: "2026-06-18T11:00:00Z", updatedAt: "2026-06-18T11:30:00Z" },
          ],
        })}
      />,
    );
    expect(screen.getByText("Pull requests")).toBeInTheDocument();
    expect(screen.getByText("task-t1")).toBeInTheDocument();
    expect(screen.getAllByText("merged").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("changes_requested").length).toBeGreaterThanOrEqual(1);
  });

  it("shows PR review and test status badges on related task cards", () => {
    render(
      <CodingProjectView
        {...props({
          prs: [
            { prId: "pr1", taskId: "t1", branch: "task-t1", status: "mergeable",
              reviewerApproved: true, testsPassed: true, conflicts: [], reviewFindings: [],
              createdAt: "2026-06-18T10:00:00Z", updatedAt: "2026-06-18T10:30:00Z" },
            { prId: "pr2", taskId: "t3", branch: "task-t3", status: "merged",
              reviewerApproved: true, testsPassed: true, conflicts: [], reviewFindings: [],
              createdAt: "2026-06-18T11:00:00Z", updatedAt: "2026-06-18T11:30:00Z" },
          ],
        })}
      />,
    );

    const devTask = screen.getByText("scaffold").closest("li") as HTMLElement;
    expect(within(devTask).getByText("dev")).toBeInTheDocument();
    expect(within(devTask).getByText("merged")).toBeInTheDocument();
    expect(within(devTask).getByText("review approved")).toBeInTheDocument();
    expect(within(devTask).getByText("tests passed")).toBeInTheDocument();

    const reviewTask = screen.getByText("review add").closest("li") as HTMLElement;
    expect(within(reviewTask).getByText("ready to merge")).toBeInTheDocument();
    expect(within(reviewTask).getByText("tests passed")).toBeInTheDocument();
  });

  it("F127: shows a reassignment note in the task detail", () => {
    render(
      <CodingProjectView
        {...props({
          decisions: [
            {
              decisionId: "d-re", title: "task reassigned: impl add",
              choice: "task_reassigned",
              rationale: "Onyx produced 2 unusable turn(s); reassigning to a stronger member",
              relatedTaskIds: ["t1"],
            },
          ],
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Open task details for impl add" }));
    const detail = screen.getByLabelText("Task detail");
    expect(within(detail).getByText(/Reassigned/)).toBeInTheDocument();
    expect(within(detail).getByText(/reassigning to a stronger member/)).toBeInTheDocument();
  });

  it("opens a task detail panel with linked spec, plan, PR review, and acceptance details", () => {
    render(
      <CodingProjectView
        {...props({
          tasks: [
            {
              taskId: "t1",
              title: "impl add",
              role: "dev",
              state: "doing",
              assigneeMemberId: "m-dev",
              dependsOn: [],
              sourceSpecArtifactId: "spec-1",
              sourcePlanArtifactId: "plan-1",
              sourceSliceId: "slice-1",
              governanceRequired: true,
            },
          ],
          governance: {
            state: {
              mode: "strict",
              phase: "build",
              humanCodeApproval: "final_only",
              activeArtifactIds: {},
              blockOnProblems: true,
              monitor: {},
              updatedAt: "",
            },
            artifacts: [
              {
                artifactId: "spec-1",
                artifactKind: "spec",
                version: 2,
                state: "approved",
                title: "Spec: CLI behavior",
                bodyMarkdown: "Spec body with requirements",
                sourceRefs: [],
                supersedesArtifactId: null,
                createdAt: "",
              },
              {
                artifactId: "plan-1",
                artifactKind: "plan",
                version: 1,
                state: "approved",
                title: "Plan: implementation",
                bodyMarkdown: "Plan body with slices",
                sourceRefs: [],
                supersedesArtifactId: null,
                createdAt: "",
              },
            ],
            reviews: [
              {
                reviewId: "rev-1",
                artifactId: "spec-1",
                reviewerMemberId: "m-review",
                verdict: "approved",
                findings: [],
                createdAt: "",
              },
            ],
            approvals: [
              {
                approvalId: "app-1",
                kind: "spec",
                artifactId: "spec-1",
                requiredActor: "user",
                state: "approved",
                requestedByMemberId: "m-pm",
                resolvedBy: "user",
                feedback: "",
                createdAt: "",
                resolvedAt: null,
              },
            ],
            planSlices: [
              {
                sliceId: "slice-1",
                title: "Build add command",
                detail: "Implement the add command path.",
                dependsOn: [],
                doneWhen: ["CLI adds a todo"],
                tests: ["unit test passes"],
                reviewFocus: ["argument parsing"],
              },
            ],
          },
          prs: [
            {
              prId: "pr1",
              taskId: "t1",
              branch: "task-t1",
              status: "mergeable",
              reviewerApproved: true,
              testsPassed: true,
              conflicts: [], reviewFindings: [],
              createdAt: "2026-06-18T09:00:00Z",
              updatedAt: "2026-06-18T10:00:00Z",
            },
          ],
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Open task details for impl add" }));
    const detail = screen.getByLabelText("Task detail");

    expect(within(detail).getByText("Spec: CLI behavior · v2 · approved")).toBeInTheDocument();
    expect(within(detail).getByText("Spec body with requirements")).toBeInTheDocument();
    expect(within(detail).getByText("user approved")).toBeInTheDocument();
    expect(within(detail).getByText("Slice slice-1: Build add command")).toBeInTheDocument();
    expect(within(detail).getByText("CLI adds a todo")).toBeInTheDocument();
    expect(within(detail).getByText("unit test passes")).toBeInTheDocument();
    expect(within(detail).getByText("task-t1")).toBeInTheDocument();
    expect(within(detail).getByText("review: approved")).toBeInTheDocument();
    expect(within(detail).getByText("tests: passed")).toBeInTheDocument();
  });

  it("shows the reviewer's findings (the WHY) for a changes-requested PR", () => {
    render(
      <CodingProjectView
        {...props({
          prs: [
            {
              prId: "pr1", taskId: "t1", branch: "task-t1", status: "changes_requested",
              reviewerApproved: false, testsPassed: null, conflicts: [],
              createdAt: "2026-06-18T09:00:00Z", updatedAt: "2026-06-18T10:00:00Z",
              reviewFindings: [
                { severity: "blocking", title: "Missing input validation",
                  body: "parse_args accepts empty argv", path: "cli.py", blocking: true },
                { severity: "minor", title: "Nit: rename var", body: "", path: "", blocking: false },
              ],
            },
          ],
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Open task details for impl add" }));
    const detail = screen.getByLabelText("Task detail");
    expect(within(detail).getByText(/Missing input validation/)).toBeInTheDocument();
    expect(within(detail).getByText("parse_args accepts empty argv")).toBeInTheDocument();
    expect(within(detail).getByText(/Nit: rename var/)).toBeInTheDocument();
  });

  // The require-sandbox toggle lived in the Test Commands panel, which is
  // feature-flagged off (TEST_COMMANDS_ENABLED=false); its test moves with it.
});

describe("CodingProjectView", () => {
  it("renders board columns, decisions, artifacts", () => {
    render(<CodingProjectView {...props()} />);
    expect(screen.getByLabelText("Doing tasks")).toBeInTheDocument();
    expect(screen.getByText("impl add")).toBeInTheDocument();   // doing column
    expect(screen.getByText("scaffold")).toBeInTheDocument();   // done column
    expect(screen.getByText(/use argparse/)).toBeInTheDocument();
    expect(screen.getAllByText("src/cli.py").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByLabelText("Tool events")).toBeInTheDocument();
    expect(screen.getByText("code_write")).toBeInTheDocument();
  });

  it("renders the runtime slot between project controls and the board", () => {
    render(<CodingProjectView {...props({ runtimeSlot: <div>Runtime slot mounted</div> })} />);
    expect(screen.getByText("Runtime slot mounted")).toBeInTheDocument();
  });

  it("renders ticket assignee member names instead of raw member ids", () => {
    render(
      <CodingProjectView
        {...props({
          tasks: [
            {
              taskId: "t-cipher",
              title: "wire command palette",
              role: "dev",
              state: "doing",
              assigneeMemberId: "m-2",
              dependsOn: [],
            },
          ],
          memberNameById: { "m-2": "Cipher-DEV" },
        })}
      />,
    );

    const task = screen.getByText("wire command palette").closest("li") as HTMLElement;
    expect(within(task).getByText("Cipher-DEV")).toBeInTheDocument();
    expect(within(task).getByText("Cipher-DEV")).toHaveAttribute("title", "m-2");
    expect(within(task).queryByText("m-2")).toBeNull();
  });

  it("renders failed tool event errors", () => {
    render(<CodingProjectView {...props({ toolEvents: [{
      eventId: "te2",
      taskId: "t1",
      memberId: "m-dev",
      role: "dev",
      tool: "code_write",
      status: "failed",
      path: "../x",
      error: "unsafe path",
    }] })} />);
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("unsafe path")).toBeInTheDocument();
  });

  // The guardrail toggle + checkpoint cadence now live in the PM Governance
  // panel; see GovernancePanel.test.tsx.

  it("does not render the North Star editor in the project body", () => {
    render(<CodingProjectView {...props()} />);
    expect(screen.queryByLabelText("North Star")).toBeNull();
    expect(screen.queryByRole("button", { name: /Save North Star/ })).toBeNull();
  });

  it("expands decisions and tool events from collapsed, opens a touched file viewer", () => {
    render(<CodingProjectView {...props()} />);

    const decisionsPanel = screen.getByText("Decisions").closest("details") as HTMLDetailsElement;
    expect(decisionsPanel.open).toBe(false);
    fireEvent.click(screen.getByText("Decisions").closest("summary") as HTMLElement);
    expect(decisionsPanel.open).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /src\/cli.py/ }));
    expect(screen.getByLabelText("File viewer")).toBeInTheDocument();
    expect(screen.getByText("entry")).toBeInTheDocument();

    const toolPanel = screen.getByText("Tool events").closest("details") as HTMLDetailsElement;
    expect(toolPanel.open).toBe(false);
    fireEvent.click(screen.getByText("Tool events").closest("summary") as HTMLElement);
    expect(toolPanel.open).toBe(true);
  });

  it("fetches and renders selected master file contents", async () => {
    getFileMock.mockResolvedValueOnce({
      path: "src/cli.py",
      content: "def main():\n    return 0\n",
      truncated: false,
      encoding: "utf-8",
      bytes: 25,
      onMaster: true,
    });

    render(<CodingProjectView {...props()} />);
    fireEvent.click(screen.getByRole("button", { name: /src\/cli.py/ }));

    expect(getFileMock).toHaveBeenCalledWith("todo-app", "src/cli.py");
    // F105: eligible text files open in the in-app editor (labeled by path).
    expect(await screen.findByLabelText("Edit file src/cli.py")).toHaveValue(
      "def main():\n    return 0\n",
    );
  });

  it("disables artifact rows that are not on master", () => {
    render(
      <CodingProjectView
        {...props({
          artifacts: [
            { path: "src/future.py", status: "created", summary: "", onMaster: false },
          ],
        })}
      />,
    );

    expect(screen.getByRole("button", { name: /src\/future.py/ })).toBeDisabled();
    expect(screen.getByText("not on master yet")).toBeInTheDocument();
    expect(getFileMock).not.toHaveBeenCalled();
  });

  it("shows binary and truncated file notices", async () => {
    getFileMock.mockResolvedValueOnce({
      path: "src/blob.bin",
      content: null,
      truncated: false,
      encoding: "binary",
      bytes: 7,
      onMaster: true,
    });
    const { rerender } = render(
      <CodingProjectView
        {...props({
          artifacts: [{ path: "src/blob.bin", status: "created", summary: "", onMaster: true }],
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /src\/blob.bin/ }));
    expect(await screen.findByText("Binary file - not shown.")).toBeInTheDocument();

    getFileMock.mockResolvedValueOnce({
      path: "src/large.txt",
      content: "x".repeat(32),
      truncated: true,
      encoding: "utf-8",
      bytes: 300000,
      onMaster: true,
    });
    rerender(
      <CodingProjectView
        {...props({
          artifacts: [{ path: "src/large.txt", status: "created", summary: "", onMaster: true }],
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /src\/large.txt/ }));
    expect(await screen.findByText(/Showing first 256 KiB of 300000 bytes/)).toBeInTheDocument();
  });

  it("copies file contents", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    getFileMock.mockResolvedValueOnce({
      path: "src/cli.py",
      content: "print('copy')\n",
      truncated: false,
      encoding: "utf-8",
      bytes: 14,
      onMaster: true,
    });
    try {
      render(<CodingProjectView {...props()} />);
      fireEvent.click(screen.getByRole("button", { name: /src\/cli.py/ }));
      await screen.findByLabelText("Edit file src/cli.py");
      fireEvent.click(screen.getByRole("button", { name: "Copy" }));
      expect(writeText).toHaveBeenCalledWith("print('copy')\n");
    } finally {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: originalClipboard,
      });
    }
  });

  it("shows loading and file fetch error states", async () => {
    let resolveFile: (value: Awaited<ReturnType<typeof getFile>>) => void = () => {};
    getFileMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveFile = resolve;
      }),
    );
    const { rerender } = render(<CodingProjectView {...props()} />);
    fireEvent.click(screen.getByRole("button", { name: /src\/cli.py/ }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading file...");
    resolveFile({
      path: "src/cli.py",
      content: "done",
      truncated: false,
      encoding: "utf-8",
      bytes: 4,
      onMaster: true,
    });
    await waitFor(() =>
      expect(screen.getByLabelText("Edit file src/cli.py")).toHaveValue("done"),
    );

    getFileMock.mockRejectedValueOnce(new Error("sidecar down"));
    rerender(<CodingProjectView {...props({ artifacts: [
      { path: "src/error.py", status: "created", summary: "", onMaster: true },
    ] })} />);
    fireEvent.click(screen.getByRole("button", { name: /src\/error.py/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not load file."));
  });

  it("filters, sorts, and opens pull request details", () => {
    render(
      <CodingProjectView
        {...props({
          prs: [
            {
              prId: "pr-old",
              taskId: "t-old",
              branch: "feature-old",
              status: "open",
              reviewerApproved: null,
              testsPassed: null,
              conflicts: [], reviewFindings: [],
              createdAt: "2026-06-18T09:00:00Z",
              updatedAt: "2026-06-18T09:10:00Z",
            },
            {
              prId: "pr-new",
              taskId: "t-new",
              branch: "feature-new",
              status: "changes_requested",
              reviewerApproved: false,
              testsPassed: false,
              conflicts: ["src/cli.py"],
              reviewFindings: [],
              createdAt: "2026-06-18T12:00:00Z",
              updatedAt: "2026-06-18T12:30:00Z",
            },
          ],
        })}
      />,
    );

    const branchButtons = screen.getAllByRole("button", { name: /feature-/ });
    expect(branchButtons[0]).toHaveTextContent("feature-new");
    fireEvent.change(screen.getByLabelText("Sort pull requests"), { target: { value: "oldest" } });
    expect(screen.getAllByRole("button", { name: /feature-/ })[0]).toHaveTextContent("feature-old");

    fireEvent.change(screen.getByLabelText("Filter pull requests by status"), {
      target: { value: "changes_requested" },
    });
    expect(screen.queryByText("feature-old")).toBeNull();
    expect(screen.getByText("feature-new")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search pull requests"), { target: { value: "new" } });
    fireEvent.click(screen.getByRole("button", { name: /feature-new/ }));
    const detail = screen.getByLabelText("Pull request detail");
    expect(detail).toBeInTheDocument();
    expect(within(detail).getByText("pr-new")).toBeInTheDocument();
    expect(within(detail).getByText("changes requested")).toBeInTheDocument();
    expect(within(detail).getByText("src/cli.py")).toBeInTheDocument();
  });

  it("sends a message, renders the PM reply, and adds a task", async () => {
    const onInterject = vi.fn().mockResolvedValue({
      message: "How close are we to being done?",
      at: "2026-06-18T00:00:00Z",
      pmReply: {
        role: "pm",
        kind: "progress_summary",
        message: "We're 33% done by task count: 1 done task, 1 active task, 1 todo task, 0 blocked tasks.",
        progress: { total: 3, done: 1, doing: 1, todo: 1, blocked: 0, percent: 33 },
        source: "ledger.backlog.task_states",
        sourceIds: ["t1", "t2", "t3"],
        at: "2026-06-18T00:00:01Z",
      },
    });
    const onAddTask = vi.fn();
    render(<CodingProjectView {...props()} onInterject={onInterject} onAddTask={onAddTask} />);
    fireEvent.change(screen.getByLabelText("Contact the PM"),
      { target: { value: "How close are we to being done?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send directive" }));
    expect(onInterject).toHaveBeenCalledWith("How close are we to being done?");
    expect(await screen.findByText(/We're 33% done by task count/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("New task title"), { target: { value: "add --json flag" } });
    fireEvent.click(screen.getByRole("button", { name: "Add task" }));
    expect(onAddTask).toHaveBeenCalledWith("add --json flag", "dev");
  });

  it("F141 WS-J: chats with the PM and renders the immediate reply", async () => {
    const { pmAsk } = await import("../../lib/api/coding");
    vi.mocked(pmAsk).mockResolvedValue({
      reply: { role: "pm", kind: "chat", message: "We're roughly halfway.", at: "" },
      threadId: "main",
      answered: true,
    });
    render(<CodingProjectView {...props()} />);
    fireEvent.change(screen.getByLabelText("Contact the PM"), {
      target: { value: "hows it going" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask a question" }));
    expect(vi.mocked(pmAsk)).toHaveBeenCalledWith("todo-app", "hows it going");
    // optimistic user turn + the PM's model reply both render
    expect(await screen.findByText("We're roughly halfway.")).toBeInTheDocument();
    expect(screen.getByText("hows it going")).toBeInTheDocument();
  });

  it("F141 WS-J: clears the PM chat when the project changes (no cross-project bleed)", async () => {
    const { pmAsk } = await import("../../lib/api/coding");
    vi.mocked(pmAsk).mockResolvedValue({
      reply: { role: "pm", kind: "chat", message: "Project A answer.", at: "" },
      threadId: "main",
      answered: true,
    });
    const { rerender } = render(<CodingProjectView {...props()} />);
    fireEvent.change(screen.getByLabelText("Contact the PM"), {
      target: { value: "A question" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask a question" }));
    expect(await screen.findByText("Project A answer.")).toBeInTheDocument();

    // switch to a different project — the view is not remounted; the chat must reset.
    rerender(
      <CodingProjectView
        {...props({ project: { ...props().project, id: "other-app" } })}
      />,
    );
    await waitFor(() => {
      expect(screen.queryByText("Project A answer.")).toBeNull();
      expect(screen.queryByText("A question")).toBeNull();
    });
  });

  it("review-diff fires the review callback (no direct accept)", () => {
    const onReviewMergeBack = vi.fn();
    render(<CodingProjectView {...props()} onReviewMergeBack={onReviewMergeBack} />);
    fireEvent.click(screen.getByRole("button", { name: /Review diff/ }));
    expect(onReviewMergeBack).toHaveBeenCalled();
  });
});

describe("CodingProjectView run controls", () => {
  it("starts a run when idle and stops when running", () => {
    const onStartRun = vi.fn();
    const onCancelRun = vi.fn();
    const { rerender } = render(
      <CodingProjectView {...props()} running={false} onStartRun={onStartRun} onCancelRun={onCancelRun} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(onStartRun).toHaveBeenCalled();
    rerender(
      <CodingProjectView {...props()} running={true} onStartRun={onStartRun} onCancelRun={onCancelRun} />,
    );
    // F121: the static "Team working…" label became a live "Working…" affordance.
    expect(screen.getByText(/Working/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
    expect(onCancelRun).toHaveBeenCalled();
  });

  it("resumes an interrupted run instead of starting a new one", () => {
    const onStartRun = vi.fn();
    const onResumeRun = vi.fn();
    render(
      <CodingProjectView
        {...props()}
        running={false}
        runStatus={{
          running: false,
          result: { stop_reason: "interrupted" },
          state: { status: "interrupted" },
          recoverable: true,
          canResume: true,
        }}
        onStartRun={onStartRun}
        onResumeRun={onResumeRun}
      />,
    );

    expect(screen.getByText(/Interrupted - ready to resume/)).toBeInTheDocument();
    expect(screen.getByText(/In-flight tasks were returned/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume run" }));
    expect(onResumeRun).toHaveBeenCalled();
    expect(onStartRun).not.toHaveBeenCalled();
  });
});

const PR = (over: Record<string, unknown> = {}) => ({
  prId: "pr-a", taskId: "t1", branch: "task-A", status: "open",
  reviewerApproved: null, testsPassed: null, conflicts: [], reviewFindings: [],
  createdAt: "2026-06-19T00:00:00Z", updatedAt: "2026-06-19T00:00:00Z",
  ...over,
});

describe("F091 — superseded PR UI", () => {
  it("shows a 'superseded' badge and not a stale review badge", () => {
    render(<CodingProjectView {...props({
      prs: [PR({ prId: "pr-a", taskId: "t1", status: "superseded",
                 reviewerApproved: false, supersededByPrId: "pr-b" }),
            PR({ prId: "pr-b", taskId: "t9", branch: "task-B", status: "merged" })],
      tasks: [{ taskId: "t1", title: "impl add", role: "dev", state: "done",
                assigneeMemberId: "m", dependsOn: [] }],
    })} />);
    // the task badge for the superseded PR's task reads "superseded", not "review changes"
    expect(screen.getAllByText("superseded").length).toBeGreaterThan(0);
    expect(screen.queryByText("review changes")).toBeNull();
  });

  it("shows 'superseded by <branch>' in the PR row", () => {
    render(<CodingProjectView {...props({
      prs: [PR({ prId: "pr-a", status: "superseded", supersededByPrId: "pr-b" }),
            PR({ prId: "pr-b", branch: "task-B", status: "merged" })],
    })} />);
    expect(screen.getByText(/superseded by\s+task-B/)).toBeInTheDocument();
  });
});

describe("F093 — Project summary panel", () => {
  const withRun = (stopReason: string | null, over: Record<string, unknown> = {}) =>
    props({
      runStatus: stopReason == null
        ? { running: false, result: null, recoverable: false, canResume: false }
        : { running: false, result: { stop_reason: stopReason }, recoverable: false, canResume: false },
      ...over,
    });

  it("shows ✓ Complete + the completion summary on definition_of_done", () => {
    render(<CodingProjectView {...withRun("definition_of_done", {
      project: { id: "p", northStar: "n", definitionOfDone: "d", target: "new",
                 status: "done", revision: 2, completionSummary: "all reqs met" },
    })} />);
    expect(screen.getByText("✓ Complete")).toBeInTheDocument();
    expect(screen.getByText("all reqs met")).toBeInTheDocument();
  });

  it.each([
    "no_progress",
    "no_actionable_work",
    "budget_exhausted",
    "hard_blocker",
    "member_unhealthy",
    "completion_blocked",
  ])(
    "renders a ⚠ badge (not blank/Complete) for %s",
    (reason) => {
      render(<CodingProjectView {...withRun(reason)} />);
      expect(screen.getByText(new RegExp(`Stopped without completing \\(${reason}\\)`)))
        .toBeInTheDocument();
      expect(screen.queryByText("✓ Complete")).toBeNull();
    },
  );

  it("renders ⏸ for cancelled and checkpoint", () => {
    const { unmount } = render(<CodingProjectView {...withRun("cancelled")} />);
    expect(screen.getByText("⏸ Interrupted / Cancelled")).toBeInTheDocument();
    unmount();
    render(<CodingProjectView {...withRun("checkpoint")} />);
    expect(screen.getByText("⏸ Paused at checkpoint")).toBeInTheDocument();
  });

  it("renders ▶ Running while a run is active", () => {
    render(<CodingProjectView {...props({ running: true })} />);
    expect(screen.getByText("▶ Running")).toBeInTheDocument();
  });

  it("shows the greenfield no-tests note when there are no test runs", () => {
    render(<CodingProjectView {...withRun("definition_of_done", { testRuns: [] })} />);
    expect(screen.getByText(/No automated tests were configured/)).toBeInTheDocument();
  });

  it("lists test runs with pass/fail when present", () => {
    render(<CodingProjectView {...withRun("definition_of_done", {
      testRuns: [{ testRunId: "tr1", taskId: "t1", passed: true,
                   commandIds: ["unit"], sandbox: false, at: "2026-06-19T00:00:00Z" }],
    })} />);
    const tests = screen.getByLabelText("Test results");
    expect(within(tests).getByText("passed")).toBeInTheDocument();
    expect(within(tests).getByText(/unit/)).toBeInTheDocument();
  });

  it("counts merged PRs", () => {
    render(<CodingProjectView {...withRun("definition_of_done", {
      prs: [PR({ prId: "p1", status: "merged" }), PR({ prId: "p2", status: "merged" }),
            PR({ prId: "p3", status: "open" })],
    })} />);
    expect(screen.getByText("2 merged PRs")).toBeInTheDocument();
  });

  it("uses snake_case result.stop_reason (camelCase would not match)", () => {
    // a camelCase result key must NOT be read as the stop reason
    render(<CodingProjectView {...props({
      runStatus: { running: false, result: { stopReason: "definition_of_done" },
                   recoverable: false, canResume: false },
      project: { id: "p", northStar: "n", definitionOfDone: "d", target: "new",
                 status: "active", revision: 1 },
    })} />);
    expect(screen.queryByText("✓ Complete")).toBeNull();
    const summary = screen.getByLabelText("Project summary");
    expect(within(summary).getByText("Idle")).toBeInTheDocument();
  });

  it("is collapsed by default", () => {
    render(<CodingProjectView {...withRun("definition_of_done")} />);
    expect(screen.getByLabelText("Project summary")).not.toHaveAttribute("open");
  });

  it("opens the project folder from the summary actions", () => {
    const onOpen = vi.fn();
    render(<CodingProjectView {...props({
      project: { id: "p", northStar: "n", definitionOfDone: "d", target: "existing",
                 repoPath: "/tmp/project", status: "active", revision: 1 },
      onOpenProjectPath: onOpen,
    })} />);
    fireEvent.click(screen.getByText("Project summary"));
    fireEvent.click(screen.getByRole("button", { name: "Open project" }));
    expect(onOpen).toHaveBeenCalledWith("/tmp/project");
  });

  it("uses the delivered folder and runnable artifact for run actions", () => {
    const onOpenProject = vi.fn();
    const onOpenRun = vi.fn();
    render(<CodingProjectView {...props({
      delivery: { deliveredTo: "/tmp/delivered", openUrl: "file:///tmp/delivered", runHint: "open index.html" },
      artifacts: [{ path: "index.html", status: "created", summary: "entry", onMaster: true }],
      onOpenProjectPath: onOpenProject,
      onOpenRunTarget: onOpenRun,
    })} />);
    fireEvent.click(screen.getByText("Project summary"));
    fireEvent.click(screen.getByRole("button", { name: "Open project" }));
    fireEvent.click(screen.getByRole("button", { name: "Run project" }));
    expect(onOpenProject).toHaveBeenCalledWith("/tmp/delivered");
    expect(onOpenRun).toHaveBeenCalledWith("/tmp/delivered/index.html");
  });

  it("does not open a traversal artifact as a run target", () => {
    const onOpenRun = vi.fn();
    render(<CodingProjectView {...props({
      delivery: { deliveredTo: "/tmp/delivered", openUrl: "file:///tmp/delivered", runHint: "" },
      artifacts: [{ path: "../index.html", status: "created", summary: "entry", onMaster: true }],
      onOpenRunTarget: onOpenRun,
    })} />);
    fireEvent.click(screen.getByText("Project summary"));
    expect(screen.getByRole("button", { name: "Run target unavailable" })).toBeDisabled();
    expect(screen.getByText("Run target: none yet.")).toBeInTheDocument();
    expect(onOpenRun).not.toHaveBeenCalled();
  });

  it("does not open an absolute artifact path as a run target", () => {
    const onOpenRun = vi.fn();
    render(<CodingProjectView {...props({
      delivery: { deliveredTo: "/tmp/delivered", openUrl: "file:///tmp/delivered", runHint: "" },
      artifacts: [{ path: "/tmp/other/index.html", status: "created", summary: "entry", onMaster: true }],
      onOpenRunTarget: onOpenRun,
    })} />);
    fireEvent.click(screen.getByText("Project summary"));
    expect(screen.getByRole("button", { name: "Run target unavailable" })).toBeDisabled();
    expect(onOpenRun).not.toHaveBeenCalled();
  });
});
