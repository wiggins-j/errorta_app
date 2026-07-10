import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, it, vi } from "vitest";

import GroundingPanel from "./GroundingPanel";
import * as api from "../../lib/api/coding";
import { expectNoA11yViolations } from "../council/a11y-helpers";

vi.mock("../../lib/api/coding");
const mocked = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  mocked.getCorpusBinding.mockResolvedValue({
    projectId: "p", mode: "existing", corpusId: "aerospace-mini", sourceRoot: null,
    indexVersion: 3, lastRefreshAt: "2026-06-18T12:00:00Z", healthState: "ready",
    healthReason: "412 ready files", bootstrapJobId: null,
  });
  mocked.getGroundingCapabilities.mockResolvedValue({
    available: true, version: "0.2.3", source: "remote", supportsCorpusIds: true,
    supportsFileIngest: true, supportsRecordIngest: true, supportsMetadataFilters: false,
    supportsProvenanceMetadata: true, supportsIncrementalRefresh: true, supportsSupersession: false,
    supportsExportImport: false, localOnlyEmbedding: true, notes: [],
  });
  mocked.getPmWorkingMemoryStatus.mockResolvedValue({
    projectId: "p",
    status: "local",
    memoryRef: "mem:pm",
    corpusId: null,
    aiarMirrorStatus: "not_attempted",
    aiarRetrievalStatus: "unknown",
    lastGeneratedAt: null,
    lastMirroredAt: null,
    warnings: [],
  });
  mocked.listGroundingCorpora.mockResolvedValue([]);
});

afterEach(() => cleanup());

describe("GroundingPanel a11y", () => {
  it("has no serious/critical axe violations when expanded", async () => {
    const { container } = render(<GroundingPanel projectId="p" />);
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    await screen.findByLabelText("Corpus binding");
    await expectNoA11yViolations(container);
  });
});
