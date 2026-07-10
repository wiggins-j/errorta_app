import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import GroundingPanel from "./GroundingPanel";
import * as api from "../../lib/api/coding";
import { listCorpora } from "../../lib/api/corpus";

vi.mock("../../lib/api/coding");
vi.mock("../../lib/api/corpus", () => ({
  listCorpora: vi.fn(),
}));

const mocked = vi.mocked(api);
const mockedListCorpora = vi.mocked(listCorpora);

const READY_BINDING: api.ProjectCorpusBinding = {
  projectId: "p",
  mode: "existing",
  corpusId: "aerospace-mini",
  sourceRoot: null,
  indexVersion: 3,
  lastRefreshAt: "2026-06-18T12:00:00Z",
  healthState: "ready",
  healthReason: "412 ready files",
  bootstrapJobId: null,
};

const CAPS: api.ProjectGroundingCapabilities = {
  available: true,
  version: "0.2.3",
  source: "remote",
  supportsCorpusIds: true,
  supportsFileIngest: true,
  supportsRecordIngest: true,
  supportsMetadataFilters: false,
  supportsProvenanceMetadata: true,
  supportsIncrementalRefresh: true,
  supportsSupersession: false,
  supportsExportImport: false,
  localOnlyEmbedding: true,
  notes: ["remote instance"],
};

beforeEach(() => {
  vi.clearAllMocks();
  mocked.getCorpusBinding.mockResolvedValue(READY_BINDING);
  mocked.getGroundingCapabilities.mockResolvedValue(CAPS);
  mocked.getPmWorkingMemoryStatus.mockResolvedValue({
    projectId: "p",
    status: "local",
    memoryRef: "mem:mem_pm_working_memory_p",
    corpusId: null,
    aiarMirrorStatus: "not_attempted",
    aiarRetrievalStatus: "unknown",
    lastGeneratedAt: "2026-06-18T12:05:00Z",
    lastMirroredAt: null,
    warnings: [],
  });
  mocked.listGroundingCorpora.mockResolvedValue([
    { name: "aerospace-mini", fileCount: 412, readyCount: 412 },
    { name: "legal-mini", fileCount: 20, readyCount: 18 },
  ]);
  mockedListCorpora.mockResolvedValue([
    { name: "aerospace-mini", fileCount: 412, readyCount: 412, status: "ready", source: "remote" },
    { name: "legal-mini", fileCount: 20, readyCount: 18, status: "indexing", source: "remote" },
  ]);
});

afterEach(() => cleanup());

describe("GroundingPanel binding summary", () => {
  it("renders mode + corpus id + ready health in the collapsed summary", async () => {
    render(<GroundingPanel projectId="p" />);
    const toggle = await screen.findByRole("button", { expanded: false });
    expect(toggle).toHaveAccessibleName(/Project grounding/);
    expect(within(toggle).getByText("Settings")).toBeInTheDocument();
    expect(screen.getByText("existing corpus aerospace-mini")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });

  it("shows the unbound state when mode is none", async () => {
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING,
      mode: "none",
      corpusId: null,
      healthState: "missing",
      healthReason: "no corpus bound",
    });
    render(<GroundingPanel projectId="p" />);
    expect(await screen.findByText("No corpus bound")).toBeInTheDocument();
  });

  it("renders an inline error + Retry when the binding load fails", async () => {
    mocked.getCorpusBinding.mockRejectedValueOnce(new Error("boom"));
    render(<GroundingPanel projectId="p" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    mocked.getCorpusBinding.mockResolvedValue(READY_BINDING);
    fireEvent.click(screen.getByText("Retry"));
    await screen.findByText("Project grounding");
  });
});

async function expand() {
  render(<GroundingPanel projectId="p" />);
  const toggle = await screen.findByRole("button", { expanded: false });
  fireEvent.click(toggle);
}

describe("GroundingPanel expanded blocks", () => {
  it("shows binding details + honest capabilities", async () => {
    await expand();
    expect(await screen.findByLabelText("Corpus binding")).toBeInTheDocument();
    expect(await screen.findByLabelText("PM working memory")).toBeInTheDocument();
    expect(screen.getByText("412 ready files")).toBeInTheDocument();
    const caps = await screen.findByLabelText("Grounding capabilities");
    expect(within(caps).getByText(/Available/)).toBeInTheDocument();
    expect(within(caps).getByText(/remote 0.2.3/)).toBeInTheDocument();
  });

  it("shows PM memory mirror status without raw memory text", async () => {
    mocked.getPmWorkingMemoryStatus.mockResolvedValue({
      projectId: "p",
      status: "mirrored",
      memoryRef: "mem:pm",
      corpusId: "project-p",
      aiarMirrorStatus: "mirrored",
      aiarRetrievalStatus: "available",
      lastGeneratedAt: "2026-06-18T12:05:00Z",
      lastMirroredAt: "2026-06-18T12:06:00Z",
      warnings: ["pm_working_memory_corpus_miss"],
    });
    await expand();
    const block = await screen.findByLabelText("PM working memory");
    expect(within(block).getAllByText("mirrored").length).toBeGreaterThan(0);
    expect(within(block).getByText(/corpus project-p/)).toBeInTheDocument();
    expect(within(block).getByText("pm_working_memory_corpus_miss")).toBeInTheDocument();
    expect(screen.queryByText(/north star/i)).not.toBeInTheDocument();
  });

  it("edits the binding: pick an existing corpus and Save calls putCorpusBinding", async () => {
    mocked.putCorpusBinding.mockResolvedValue({ ...READY_BINDING, corpusId: "legal-mini" });
    await expand();
    fireEvent.click(await screen.findByText("Edit binding"));
    const editor = await screen.findByLabelText("Edit binding");
    // READY_BINDING is mode "existing" -> editor opens on "Use an existing
    // corpus"; the corpus dropdown is labeled "Existing corpus".
    fireEvent.change(within(editor).getByLabelText("Existing corpus"), {
      target: { value: "legal-mini" },
    });
    fireEvent.click(within(editor).getByText("Save binding"));
    await waitFor(() =>
      expect(mocked.putCorpusBinding).toHaveBeenCalledWith("p", {
        mode: "existing",
        corpusId: "legal-mini",
        sourceRoot: null,
      }),
    );
  });

  it("disables Edit while a run is active", async () => {
    render(<GroundingPanel projectId="p" running />);
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    expect(await screen.findByText("Edit binding")).toBeDisabled();
  });

  it("builds a corpus from the project when not bound", async () => {
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING, mode: "none", corpusId: null, healthState: "missing",
    });
    mocked.buildCorpusFromProject.mockResolvedValue({
      ...READY_BINDING, mode: "build_from_project", corpusId: "project-p",
    });
    await expand();
    fireEvent.click(await screen.findByText("Build a corpus from this project"));
    await waitFor(() => expect(mocked.buildCorpusFromProject).toHaveBeenCalledWith("p"));
  });

  it("refreshes a project corpus from the project", async () => {
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING, mode: "build_from_project", corpusId: "project-p",
    });
    mocked.refreshProjectCorpus.mockResolvedValue(undefined);
    await expand();
    fireEvent.click(await screen.findByText("Refresh corpus from project"));
    await waitFor(() => expect(mocked.refreshProjectCorpus).toHaveBeenCalledWith("p"));
  });

  it("offers exactly two corpus choices: create-new and use-existing", async () => {
    await expand();
    fireEvent.click(await screen.findByText("Edit binding"));
    const editor = await screen.findByLabelText("Edit binding");
    const modeSelect = within(editor).getByLabelText("Corpus source") as HTMLSelectElement;
    const values = Array.from(modeSelect.options).map((o) => o.value);
    expect(values).toEqual(["build_from_project", "existing"]);
  });

  it("switching to existing requires picking a corpus before Save", async () => {
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING, mode: "none", corpusId: null, healthState: "missing",
    });
    await expand();
    // Unbound -> the editor opens via "Attach an existing corpus".
    fireEvent.click(await screen.findByText("Attach an existing corpus"));
    const editor = await screen.findByLabelText("Edit binding");
    // Defaults to "create new from this project" with a prefilled id -> enabled.
    expect(within(editor).getByText("Save binding")).toBeEnabled();
    fireEvent.change(within(editor).getByLabelText("Corpus source"), {
      target: { value: "existing" },
    });
    // No corpus picked yet -> Save disabled.
    expect(within(editor).getByText("Save binding")).toBeDisabled();
    fireEvent.change(within(editor).getByLabelText("Existing corpus"), {
      target: { value: "legal-mini" },
    });
    expect(within(editor).getByText("Save binding")).toBeEnabled();
  });

  it("removes the corpus binding from the editor", async () => {
    mocked.putCorpusBinding.mockResolvedValue({
      ...READY_BINDING, mode: "none", corpusId: null,
    });
    await expand();
    fireEvent.click(await screen.findByText("Edit binding"));
    const editor = await screen.findByLabelText("Edit binding");
    fireEvent.click(within(editor).getByText("Remove corpus"));
    await waitFor(() =>
      expect(mocked.putCorpusBinding).toHaveBeenCalledWith("p", {
        mode: "none",
        corpusId: null,
        sourceRoot: null,
      }),
    );
  });

  it("edits build_from_project without a source root and Save sends sourceRoot:null", async () => {
    // Regression: the editor used to lack build_from_project entirely, so opening
    // it on such a binding submitted an incoherent payload -> 422. It must now be
    // a first-class mode that needs NO source root (source = the project itself).
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING, mode: "build_from_project", corpusId: "project-p",
      healthState: "missing", healthReason: "corpus not built on the remote AIAR yet",
    });
    mocked.putCorpusBinding.mockResolvedValue({
      ...READY_BINDING, mode: "build_from_project", corpusId: "project-p",
    });
    await expand();
    fireEvent.click(await screen.findByText("Edit binding"));
    const editor = await screen.findByLabelText("Edit binding");
    // The mode is already build_from_project and the corpus id is prefilled.
    expect(within(editor).getByText("Save binding")).toBeEnabled();
    fireEvent.click(within(editor).getByText("Save binding"));
    await waitFor(() =>
      expect(mocked.putCorpusBinding).toHaveBeenCalledWith("p", {
        mode: "build_from_project",
        corpusId: "project-p",
        sourceRoot: null,
      }),
    );
  });

  it("offers Build for a bound build_from_project corpus that is not yet built", async () => {
    // A 404/unbuilt remote corpus (health != ready) must still expose the Build
    // action so the user can populate it once the team has merged code.
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING, mode: "build_from_project", corpusId: "project-p",
      healthState: "missing", healthReason: "corpus not built on the remote AIAR yet",
    });
    mocked.buildCorpusFromProject.mockResolvedValue({
      ...READY_BINDING, mode: "build_from_project", corpusId: "project-p",
    });
    await expand();
    fireEvent.click(await screen.findByText("Build a corpus from this project"));
    await waitFor(() => expect(mocked.buildCorpusFromProject).toHaveBeenCalledWith("p"));
    });
  });

  it("feeds the existing-corpus picker from the unified corpus catalog", async () => {
    await expand();
    fireEvent.click(await screen.findByText("Edit binding"));
    const editor = await screen.findByLabelText("Edit binding");
    await waitFor(() => expect(mockedListCorpora).toHaveBeenCalled());
    const picker = within(editor).getByLabelText("Existing corpus") as HTMLSelectElement;
    expect(Array.from(picker.options).map((o) => o.value)).toContain("legal-mini");
    expect(within(editor).getByText("remote")).toBeInTheDocument();
  });

describe("GroundingPanel retrieval probe", () => {
  it("renders hits with source + score for an ok result", async () => {
    mocked.retrieveProjectCorpus.mockResolvedValue({
      status: "ok",
      hits: [
        { content: "Apache-2.0 licensed framework", corpusId: "aerospace-mini", chunkId: "c1", score: 0.91 },
      ],
    });
    await expand();
    fireEvent.change(await screen.findByLabelText("Retrieval query"), {
      target: { value: "license" },
    });
    fireEvent.click(screen.getByText("Retrieve"));
    const results = await screen.findByLabelText("Retrieval results");
    expect(within(results).getByText(/Apache-2.0 licensed/)).toBeInTheDocument();
    expect(within(results).getByText(/score 0.910/)).toBeInTheDocument();
  });

  it("shows the no-match empty state and leaks no content", async () => {
    mocked.retrieveProjectCorpus.mockResolvedValue({ status: "empty", hits: [] });
    await expand();
    fireEvent.change(await screen.findByLabelText("Retrieval query"), {
      target: { value: "nothing" },
    });
    fireEvent.click(screen.getByText("Retrieve"));
    expect(await screen.findByText(/No matches in the corpus/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Retrieval results")).not.toBeInTheDocument();
  });

  it("shows the unavailable state when retrieval throws", async () => {
    mocked.retrieveProjectCorpus.mockRejectedValue(new Error("remote down"));
    await expand();
    fireEvent.change(await screen.findByLabelText("Retrieval query"), {
      target: { value: "q" },
    });
    fireEvent.click(screen.getByText("Retrieve"));
    expect(await screen.findByText(/Retrieval is unavailable/)).toBeInTheDocument();
  });

  it("disables the probe and prompts to bind when no corpus is bound", async () => {
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING,
      mode: "none",
      corpusId: null,
    });
    render(<GroundingPanel projectId="p" />);
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    expect(await screen.findByText(/Bind a corpus to test retrieval/)).toBeInTheDocument();
    expect(screen.getByText("Retrieve")).toBeDisabled();
  });
});

describe("GroundingPanel build progress", () => {
  it("polls the bootstrap job and stops once terminal", async () => {
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING,
      healthState: "indexing",
      healthReason: "building",
      bootstrapJobId: "job-1",
    });
    mocked.getBootstrapJob.mockResolvedValue({
      jobId: "job-1", corpusId: "aerospace-mini", status: "done",
      adapterSource: "remote", documentsIngested: 10, chunksAdded: 42, errors: [], endedAt: "2026-06-18T13:00:00Z",
    });
    render(<GroundingPanel projectId="p" />);
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    const block = await screen.findByLabelText("Build progress");
    await waitFor(() => expect(within(block).getByText(/done/)).toBeInTheDocument());
    expect(mocked.getBootstrapJob).toHaveBeenCalled();
  });

  it("stops polling and surfaces 'job not found' when the job 404s", async () => {
    mocked.getCorpusBinding.mockResolvedValue({
      ...READY_BINDING,
      healthState: "indexing",
      healthReason: "building",
      bootstrapJobId: "job-gone",
    });
    mocked.getBootstrapJob.mockResolvedValue(null); // vanished job
    render(<GroundingPanel projectId="p" />);
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    const block = await screen.findByLabelText("Build progress");
    await waitFor(() => expect(within(block).getByText(/job not found/)).toBeInTheDocument());
    // Polled once and stopped (no forever-poll on a 404).
    expect(mocked.getBootstrapJob).toHaveBeenCalledTimes(1);
  });
});
