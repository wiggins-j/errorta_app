import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { VerdictResponse } from "../../../lib/api/judge";
import PromptRunner from "../PromptRunner";
import { ToastProvider } from "../toast";

// Stub the api module so runVerdict / fetchPriorVerdicts are controllable
// without crossing the fetch boundary.
vi.mock("../../../lib/api/judge", async () => {
  const actual =
    await vi.importActual<typeof import("../../../lib/api/judge")>(
      "../../../lib/api/judge",
    );
  return {
    ...actual,
    runVerdict: vi.fn(),
    fetchPriorVerdicts: vi.fn().mockResolvedValue({ signature: "", priors: [] }),
  };
});

import { fetchPriorVerdicts, runVerdict } from "../../../lib/api/judge";

const mockedRun = runVerdict as unknown as ReturnType<typeof vi.fn>;
const mockedPriors = fetchPriorVerdicts as unknown as ReturnType<typeof vi.fn>;

function ok(overrides: Partial<VerdictResponse> = {}): VerdictResponse {
  return {
    id: "v1",
    prompt: "hi",
    answer: "hello",
    verdict: { rating: "pass", failure_tags: [] },
    prompt_signature: null,
    ...overrides,
  };
}

function renderRunner(props: Partial<React.ComponentProps<typeof PromptRunner>> = {}) {
  return render(
    <ToastProvider>
      <PromptRunner onResult={() => {}} {...props} />
    </ToastProvider>,
  );
}

beforeEach(() => {
  mockedRun.mockReset();
  mockedPriors.mockReset().mockResolvedValue({ signature: "", priors: [] });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PromptRunner — corpus hint", () => {
  it("shows non-blocking corpus hint and Run stays enabled when prompt set", async () => {
    renderRunner({ corpus: null });
    expect(
      screen.getByText(/pick a corpus on the corpus tab first/i),
    ).toBeInTheDocument();
    const textarea = screen.getByLabelText(/prompt/i);
    await userEvent.type(textarea, "what time is it?");
    const runBtn = screen.getByRole("button", { name: /run/i });
    expect(runBtn).not.toBeDisabled();
  });
});

describe("PromptRunner — F109 initialPrompt seed", () => {
  it("seeds the prompt field from initialPrompt without auto-running", () => {
    renderRunner({ initialPrompt: "What is AIAR licensed under?" });
    const textarea = screen.getByLabelText(/prompt/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("What is AIAR licensed under?");
    // Prefill only — no model call fired on mount.
    expect(mockedRun).not.toHaveBeenCalled();
  });

  it("starts empty when no initialPrompt is supplied (default unchanged)", () => {
    renderRunner();
    const textarea = screen.getByLabelText(/prompt/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("");
  });
});

describe("PromptRunner — pending skeleton", () => {
  it("skeleton during pending runVerdict", async () => {
    let resolve: (r: VerdictResponse) => void = () => {};
    mockedRun.mockReturnValue(
      new Promise<VerdictResponse>((res) => {
        resolve = res;
      }),
    );
    renderRunner();
    await userEvent.type(screen.getByLabelText(/prompt/i), "hi");
    await userEvent.click(screen.getByRole("button", { name: /run/i }));
    // Skeleton appears while pending.
    await waitFor(() => {
      expect(screen.getAllByTestId("skeleton-row").length).toBeGreaterThan(0);
    });
    resolve(ok());
    await waitFor(() => {
      expect(screen.queryByTestId("skeleton-row")).toBeNull();
    });
  });
});

describe("PromptRunner — manual retry", () => {
  it("RetryBanner appears on judge_timeout tag and requires manual click", async () => {
    mockedRun.mockResolvedValue(
      ok({ verdict: { rating: "fail", failure_tags: ["judge_timeout"] } }),
    );
    renderRunner();
    await userEvent.type(screen.getByLabelText(/prompt/i), "hi");
    await userEvent.click(screen.getByRole("button", { name: /run/i }));
    // Banner present after first run.
    await waitFor(() => {
      expect(
        screen.getByText(/judge model took too long/i),
      ).toBeInTheDocument();
    });
    // Manual retry: button must exist; clicking it re-invokes runVerdict.
    const retryBtn = screen.getByRole("button", { name: /^retry$/i });
    expect(retryBtn).toBeInTheDocument();
    expect(mockedRun).toHaveBeenCalledTimes(1);
    await userEvent.click(retryBtn);
    await waitFor(() => {
      expect(mockedRun).toHaveBeenCalledTimes(2);
    });
  });

  it("RetryBanner server copy on 5xx rejection", async () => {
    mockedRun.mockRejectedValue(new Error("HTTP 503 on /judge/verdict: down"));
    renderRunner();
    await userEvent.type(screen.getByLabelText(/prompt/i), "hi");
    await userEvent.click(screen.getByRole("button", { name: /run/i }));
    await waitFor(() => {
      expect(
        screen.getByText(/local sidecar returned a server error/i),
      ).toBeInTheDocument();
    });
  });
});
