// F040-02 — StepConnectAI tests.
//
// Verifies the "Connect your AI" onboarding step:
//   - renders AIAR runtime / API keys / Subscription CLIs / Local AI sections;
//   - shows a live "N connected" summary from a mocked listGatewayProviders +
//     getProviderKeys;
//   - Continue / Skip for now / Skip onboarding each set the seen sentinel and
//     call the right handler;
//   - no token-shaped text is ever rendered.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

vi.mock("../../lib/api/providerKeys", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api/providerKeys")>(
      "../../lib/api/providerKeys",
    );
  return {
    ...actual,
    getProviderKeys: vi.fn(),
    listGatewayProviders: vi.fn(),
    getCliStatus: vi.fn(),
    getCliLoginCommand: vi.fn(),
    cliLoginLaunchAvailable: vi.fn(),
    testProvider: vi.fn(),
  };
});

vi.mock("../../lib/api/ollama", () => ({
  health: vi.fn(),
  install: vi.fn(),
  getModels: vi.fn(),
  streamPull: vi.fn(),
}));
vi.mock("../../lib/api/hardware", () => ({
  report: vi.fn(),
  scan: vi.fn(),
}));

// Tauri shell-open + file picker — mock so nothing touches the OS.
vi.mock("@tauri-apps/plugin-shell", () => ({ open: vi.fn() }));
vi.mock("../shell/FilePickerDialog", async () => {
  const actual = await vi.importActual<
    typeof import("../shell/FilePickerDialog")
  >("../shell/FilePickerDialog");
  return { ...actual, pickPaths: vi.fn() };
});

import * as providerKeysApi from "../../lib/api/providerKeys";
import * as ollamaApi from "../../lib/api/ollama";
import * as hardwareApi from "../../lib/api/hardware";
import StepConnectAI from "./StepConnectAI";

const _hardware = hardwareApi as unknown as {
  report: ReturnType<typeof vi.fn>;
  scan: ReturnType<typeof vi.fn>;
};

const _pk = providerKeysApi as unknown as {
  getProviderKeys: ReturnType<typeof vi.fn>;
  listGatewayProviders: ReturnType<typeof vi.fn>;
  getCliStatus: ReturnType<typeof vi.fn>;
  getCliLoginCommand: ReturnType<typeof vi.fn>;
  cliLoginLaunchAvailable: ReturnType<typeof vi.fn>;
  testProvider: ReturnType<typeof vi.fn>;
};
const _ollama = ollamaApi as unknown as {
  health: ReturnType<typeof vi.fn>;
  install: ReturnType<typeof vi.fn>;
  getModels: ReturnType<typeof vi.fn>;
  streamPull: ReturnType<typeof vi.fn>;
};

const EMPTY_KEYS = {
  anthropic: { configured: false, key_preview: null },
  openai: { configured: false, key_preview: null },
  google: { configured: false, key_preview: null },
  custom: [],
};

const KEYS_WITH_ANTHROPIC = {
  ...EMPTY_KEYS,
  anthropic: { configured: true, key_preview: "…1234" },
};

const CLI_PROVIDERS = {
  providers: [
    { provider_class: "claude_cli", display_name: "Claude CLI", configured: false, connected: null },
    { provider_class: "codex_cli", display_name: "Codex CLI", configured: false, connected: null },
    { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: false, connected: null },
  ],
};

function installLocalStorageShim(): void {
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      clear: () => store.clear(),
      getItem: (key: string) => store.get(key) ?? null,
      removeItem: (key: string) => store.delete(key),
      setItem: (key: string, value: string) => store.set(key, String(value)),
      get length() {
        return store.size;
      },
      key: (i: number) => Array.from(store.keys())[i] ?? null,
    },
  });
}

function seen(): boolean {
  return localStorage.getItem("errorta.onboarding.connect-ai.seen") === "1";
}

beforeEach(() => {
  installLocalStorageShim();
  _pk.getProviderKeys.mockReset();
  _pk.listGatewayProviders.mockReset();
  _pk.getCliStatus.mockReset();
  _pk.getCliLoginCommand.mockReset();
  _pk.cliLoginLaunchAvailable.mockReset();
  _pk.testProvider.mockReset();
  _ollama.health.mockReset();
  _ollama.install.mockReset();
  _ollama.getModels.mockReset();
  _ollama.streamPull.mockReset();
  _ollama.getModels.mockResolvedValue({
    models: [],
    queried: null,
    installed: false,
  });
  _ollama.streamPull.mockReturnValue(() => {});
  _hardware.report.mockReset();
  // Default: no recommendation available (probe fails) → Settings-pointer fallback.
  _hardware.report.mockRejectedValue(new Error("no hardware report"));

  _pk.getProviderKeys.mockResolvedValue(EMPTY_KEYS);
  _pk.listGatewayProviders.mockResolvedValue(CLI_PROVIDERS);
  _pk.getCliStatus.mockImplementation((p: string) =>
    Promise.resolve({
      provider: p,
      state: "not_installed",
      found: false,
      path: "",
      nameUsed: "",
      source: "",
      version: "",
      connected: null,
      login: "",
      verifiedAt: null,
    }),
  );
  _pk.getCliLoginCommand.mockResolvedValue({
    loginArgv: ["claude", "login"],
    installUrl: "https://claude.com/cli",
    installCommand: "curl https://claude.com/install | bash",
  });
  _pk.cliLoginLaunchAvailable.mockResolvedValue(false);
  _ollama.health.mockResolvedValue({
    reachable: true,
    host: "http://127.0.0.1:11434",
    version: "0.1.0",
    error: null,
    managed_by_errorta: false,
    needs_install: false,
    platform_supported: true,
  });
});

afterEach(() => {
  cleanup();
});

describe("StepConnectAI", () => {
  it("renders the model-connection sections", async () => {
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByTestId("connect-ai-keys"));
    expect(screen.getByTestId("connect-ai-keys")).toBeInTheDocument();
    expect(screen.getByTestId("connect-ai-clis")).toBeInTheDocument();
    expect(screen.getByTestId("connect-ai-local")).toBeInTheDocument();
    expect(screen.getByText("Provider API keys")).toBeInTheDocument();
    expect(screen.getByText("Subscription CLIs")).toBeInTheDocument();
    expect(screen.getByText("Local AI (Ollama)")).toBeInTheDocument();
  });

  it("does NOT render the AIAR runtime card in onboarding (moved to Settings)", async () => {
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByTestId("connect-ai-keys"));
    expect(screen.queryByTestId("connect-ai-aiar")).toBeNull();
    expect(screen.queryByTestId("aiar-connection-card")).toBeNull();
    expect(screen.queryByText("AIAR runtime")).toBeNull();
    // …but points the user to Settings for AIAR/knowledge/residency.
    expect(screen.getByTestId("connect-ai-settings-note")).toBeInTheDocument();
  });

  it("Scan for CLIs re-loads provider status", async () => {
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByTestId("connect-ai-scan-clis"));
    const before = _pk.listGatewayProviders.mock.calls.length;
    screen.getByTestId("connect-ai-scan-clis").click();
    await waitFor(() =>
      expect(_pk.listGatewayProviders.mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("renders the API-key fixed rows and the three CLI rows", async () => {
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByText("Anthropic (Claude)"));
    expect(screen.getByText("OpenAI (ChatGPT)")).toBeInTheDocument();
    expect(screen.getByText("Google (Gemini)")).toBeInTheDocument();
    await waitFor(() => screen.getByText("Cursor CLI"));
    expect(screen.getByText("Claude CLI")).toBeInTheDocument();
    expect(screen.getByText("Codex CLI")).toBeInTheDocument();
  });

  it("shows 'No models connected yet.' when nothing is configured", async () => {
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId("connect-ai-summary")).toHaveTextContent(
        "No models connected yet.",
      ),
    );
  });

  it("shows a live connected count from the gateway status", async () => {
    _pk.getProviderKeys.mockResolvedValue(KEYS_WITH_ANTHROPIC);
    _pk.listGatewayProviders.mockResolvedValue({
      providers: [
        { provider_class: "claude_cli", display_name: "Claude CLI", configured: true, connected: true },
        { provider_class: "codex_cli", display_name: "Codex CLI", configured: false, connected: null },
        { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: false, connected: null },
      ],
    });
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    // 1 anthropic key + 1 connected claude_cli = 2 connected.
    await waitFor(() =>
      expect(screen.getByTestId("connect-ai-summary")).toHaveTextContent(
        "2 connected",
      ),
    );
  });

  it("does not count installed-but-unverified CLIs as connected", async () => {
    _pk.listGatewayProviders.mockResolvedValue({
      providers: [
        { provider_class: "claude_cli", display_name: "Claude CLI", configured: true, connected: null },
        { provider_class: "codex_cli", display_name: "Codex CLI", configured: true, connected: false },
        { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: false, connected: null },
      ],
    });
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId("connect-ai-summary")).toHaveTextContent(
        "No models connected yet.",
      ),
    );
  });

  it("renders the Ollama detect status in the Local AI section", async () => {
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId("connect-ai-ollama-status")).toHaveTextContent(
        /Ollama is running/,
      ),
    );
    expect(_ollama.health).toHaveBeenCalled();
  });

  it("Continue sets the seen sentinel and calls onAdvance", async () => {
    const onAdvance = vi.fn();
    const onSkip = vi.fn();
    render(<StepConnectAI onAdvance={onAdvance} onSkip={onSkip} />);
    await waitFor(() => screen.getByTestId("connect-ai-continue"));
    fireEvent.click(screen.getByTestId("connect-ai-continue"));
    expect(seen()).toBe(true);
    expect(onAdvance).toHaveBeenCalledTimes(1);
    expect(onSkip).not.toHaveBeenCalled();
  });

  it("Skip sets the seen sentinel and calls onSkip", async () => {
    const onAdvance = vi.fn();
    const onSkip = vi.fn();
    render(<StepConnectAI onAdvance={onAdvance} onSkip={onSkip} />);
    await waitFor(() => screen.getByTestId("connect-ai-skip"));
    fireEvent.click(screen.getByTestId("connect-ai-skip"));
    expect(seen()).toBe(true);
    expect(onSkip).toHaveBeenCalledTimes(1);
    expect(onAdvance).not.toHaveBeenCalled();
  });

  it("never renders a token-shaped string", async () => {
    _pk.getProviderKeys.mockResolvedValue(KEYS_WITH_ANTHROPIC);
    _pk.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve({
        provider: p,
        state: "installed",
        found: true,
        path: "/opt/homebrew/bin/claude",
        nameUsed: "claude",
        source: "path",
        version: "1.0.0",
        connected: true,
        login: "user@example.com",
        verifiedAt: null,
      }),
    );
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByText("Cursor CLI"));
    expect(document.body.innerHTML).not.toContain("sk-ant-");
    expect(document.body.innerHTML).not.toContain("CURSOR_API_KEY=");
  });
});

describe("StepConnectAI — F110 recommended-model download", () => {
  it("falls back to a Settings pointer when no model + no recommendation", async () => {
    // hardware.report rejects (beforeEach default) → no recommendation.
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByTestId("connect-ai-model-none"));
    expect(_hardware.report).toHaveBeenCalled();
    expect(_ollama.getModels).not.toHaveBeenCalled();
  });

  it("recommends a compatible model via the hardware probe when none is picked", async () => {
    _hardware.report.mockResolvedValue({
      recommendation: {
        primary: { id: "qwen2.5:3b", compatible: true },
      },
    });
    _ollama.getModels.mockResolvedValue({
      models: [],
      queried: "qwen2.5:3b",
      installed: false,
    });
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    // The recommended model is offered for download without a Hardware step.
    const btn = await screen.findByTestId("connect-ai-model-pull");
    expect(btn).toHaveTextContent("Download qwen2.5:3b");
    await waitFor(() =>
      expect(_ollama.getModels).toHaveBeenCalledWith("qwen2.5:3b"),
    );
  });

  it("falls back to Settings when the recommended primary is incompatible", async () => {
    _hardware.report.mockResolvedValue({
      recommendation: {
        primary: { id: "llama3.1:70b", compatible: false },
      },
    });
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByTestId("connect-ai-model-none"));
    // Incompatible → we don't offer or probe it.
    expect(_ollama.getModels).not.toHaveBeenCalled();
  });

  it("offers a sized download CTA when the selected model isn't installed", async () => {
    localStorage.setItem("errorta.selectedModel", "llama3.2:3b");
    _ollama.getModels.mockResolvedValue({
      models: [],
      queried: "llama3.2:3b",
      installed: false,
    });
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByTestId("connect-ai-model-pull"));
    const btn = screen.getByTestId("connect-ai-model-pull");
    // Reads selectedModel + checks installed.
    await waitFor(() =>
      expect(_ollama.getModels).toHaveBeenCalledWith("llama3.2:3b"),
    );
    // Size hint from the static map.
    expect(btn).toHaveTextContent("Download llama3.2:3b");
    expect(btn).toHaveTextContent("~2 GB");
  });

  it("clicking download streams progress and marks done", async () => {
    localStorage.setItem("errorta.selectedModel", "qwen2.5:7b");
    _ollama.getModels.mockResolvedValue({
      models: [],
      queried: "qwen2.5:7b",
      installed: false,
    });
    // Capture the onEvent callback so we can drive frames.
    let emit: ((e: unknown) => void) | null = null;
    _ollama.streamPull.mockImplementation((_model: string, onEvent: (e: unknown) => void) => {
      emit = onEvent;
      return () => {};
    });
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    const btn = await screen.findByTestId("connect-ai-model-pull");
    await waitFor(() =>
      expect(_ollama.getModels).toHaveBeenCalledWith("qwen2.5:7b"),
    );
    await waitFor(() => expect(btn).not.toBeDisabled());
    fireEvent.click(btn);
    await waitFor(() =>
      expect(_ollama.streamPull).toHaveBeenCalledWith(
        "qwen2.5:7b",
        expect.any(Function),
      ),
    );
    // Drive a progress frame then done.
    emit!({ event: "progress", status: "pulling 42%", percent: 42 });
    await waitFor(() =>
      expect(screen.getByTestId("connect-ai-model-progress")).toHaveTextContent(
        "42%",
      ),
    );
    emit!({ event: "done", model: "qwen2.5:7b", message: "Pulled qwen2.5:7b." });
    await waitFor(() => screen.getByTestId("connect-ai-model-ready"));
  });

  it("already-installed → 'ready', never calls streamPull", async () => {
    localStorage.setItem("errorta.selectedModel", "llama3.2:3b");
    _ollama.getModels.mockResolvedValue({
      models: ["llama3.2:3b"],
      queried: "llama3.2:3b",
      installed: true,
    });
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByTestId("connect-ai-model-ready"));
    expect(screen.queryByTestId("connect-ai-model-pull")).toBeNull();
    expect(_ollama.streamPull).not.toHaveBeenCalled();
  });

  it("does not pull while the runtime is unreachable", async () => {
    localStorage.setItem("errorta.selectedModel", "llama3.2:3b");
    _ollama.health.mockResolvedValue({
      reachable: false,
      host: "http://127.0.0.1:11434",
      version: null,
      error: null,
      managed_by_errorta: false,
      needs_install: true,
      platform_supported: true,
    });
    render(<StepConnectAI onAdvance={vi.fn()} onSkip={vi.fn()} />);
    await waitFor(() => screen.getByTestId("connect-ai-model-waiting"));
    expect(_ollama.getModels).not.toHaveBeenCalled();
    expect(_ollama.streamPull).not.toHaveBeenCalled();
  });
});
