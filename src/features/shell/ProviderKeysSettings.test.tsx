// F034-10 — ProviderKeysSettings tests.
//
// Mocks the providerKeys API client; verifies that the UI:
// - Loads the masked summary on mount.
// - Shows "no key" for unconfigured providers; "configured" + last-4
//   preview for keyed ones.
// - PUT round-trips swap the UI state to the server's masked
//   response (the raw key never re-appears post-save).
// - Custom entries can be added and deleted.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../../lib/api/providerKeys", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api/providerKeys")>(
      "../../lib/api/providerKeys",
    );
  return {
    ...actual,
    getProviderKeys: vi.fn(),
    listGatewayProviders: vi.fn(),
    putFixedProviderKey: vi.fn(),
    deleteFixedProviderKey: vi.fn(),
    putCustomProviderEntry: vi.fn(),
    deleteCustomProviderEntry: vi.fn(),
    testProvider: vi.fn(),
    getCliStatus: vi.fn(),
    setCliBinary: vi.fn(),
    clearCliBinary: vi.fn(),
    getCliLoginCommand: vi.fn(),
    cliLoginLaunchAvailable: vi.fn(),
    launchCliLogin: vi.fn(),
  };
});

// Tauri shell-open + file picker — mock so Install / Locate don't touch the OS.
const _shellOpen = vi.fn();
vi.mock("@tauri-apps/plugin-shell", () => ({ open: (...a: unknown[]) => _shellOpen(...a) }));
const _pickPaths = vi.fn();
vi.mock("./FilePickerDialog", async () => {
  const actual = await vi.importActual<typeof import("./FilePickerDialog")>(
    "./FilePickerDialog",
  );
  return { ...actual, pickPaths: (...a: unknown[]) => _pickPaths(...a) };
});

import * as providerKeysApi from "../../lib/api/providerKeys";
import ProviderKeysSettings from "./ProviderKeysSettings";

const _mocked = providerKeysApi as unknown as {
  getProviderKeys: ReturnType<typeof vi.fn>;
  listGatewayProviders: ReturnType<typeof vi.fn>;
  putFixedProviderKey: ReturnType<typeof vi.fn>;
  deleteFixedProviderKey: ReturnType<typeof vi.fn>;
  putCustomProviderEntry: ReturnType<typeof vi.fn>;
  deleteCustomProviderEntry: ReturnType<typeof vi.fn>;
  testProvider: ReturnType<typeof vi.fn>;
  getCliStatus: ReturnType<typeof vi.fn>;
  setCliBinary: ReturnType<typeof vi.fn>;
  clearCliBinary: ReturnType<typeof vi.fn>;
  getCliLoginCommand: ReturnType<typeof vi.fn>;
  cliLoginLaunchAvailable: ReturnType<typeof vi.fn>;
  launchCliLogin: ReturnType<typeof vi.fn>;
};

function cliStatus(over: Partial<import("../../lib/api/providerKeys").CliStatus> = {}) {
  return {
    provider: "cursor_cli",
    state: "installed" as const,
    found: true,
    path: "/opt/homebrew/bin/agent",
    nameUsed: "agent",
    source: "path",
    version: "1.2.3",
    connected: null,
    login: "",
    verifiedAt: null,
    ...over,
  };
}

const LOGIN_META = {
  loginArgv: ["agent", "login"],
  installUrl: "https://cursor.com/cli",
  installCommand: "curl https://cursor.com/install -fsS | bash",
};

const EMPTY_STATE = {
  anthropic: { configured: false, key_preview: null },
  openai: { configured: false, key_preview: null },
  google: { configured: false, key_preview: null },
  custom: [],
};

const ANTHROPIC_CONFIGURED = {
  ...EMPTY_STATE,
  anthropic: { configured: true, key_preview: "…1234" },
};

beforeEach(() => {
  _mocked.getProviderKeys.mockReset();
  _mocked.listGatewayProviders.mockReset();
  _mocked.putFixedProviderKey.mockReset();
  _mocked.deleteFixedProviderKey.mockReset();
  _mocked.putCustomProviderEntry.mockReset();
  _mocked.deleteCustomProviderEntry.mockReset();
  _mocked.testProvider.mockReset();
  _mocked.getCliStatus.mockReset();
  _mocked.setCliBinary.mockReset();
  _mocked.clearCliBinary.mockReset();
  _mocked.getCliLoginCommand.mockReset();
  _mocked.cliLoginLaunchAvailable.mockReset();
  _mocked.launchCliLogin.mockReset();
  _shellOpen.mockReset();
  _pickPaths.mockReset();
  _mocked.getProviderKeys.mockResolvedValue(EMPTY_STATE);
  _mocked.listGatewayProviders.mockResolvedValue({
    providers: [
      { provider_class: "claude_cli", display_name: "Claude CLI", configured: true, connected: null },
      { provider_class: "codex_cli", display_name: "Codex CLI", configured: false, connected: null },
      { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: true, connected: null },
    ],
  });
  _mocked.testProvider.mockResolvedValue({
    ok: true,
    detail: "subscription CLI ready",
    latency_ms: 12,
  });
  // Default: every CLI resolves installed (auth unknown).
  _mocked.getCliStatus.mockImplementation((p: string) =>
    Promise.resolve(cliStatus({ provider: p })),
  );
  _mocked.getCliLoginCommand.mockResolvedValue(LOGIN_META);
  _mocked.setCliBinary.mockImplementation((p: string, path: string) =>
    Promise.resolve(cliStatus({ provider: p, path, source: "override_settings" })),
  );
  _mocked.clearCliBinary.mockImplementation((p: string) =>
    Promise.resolve(cliStatus({ provider: p })),
  );
  // F040-01 S5a: launcher unavailable by default → copy-command floor.
  _mocked.cliLoginLaunchAvailable.mockResolvedValue(false);
  _mocked.launchCliLogin.mockResolvedValue({
    launched: true,
    transport: "terminal",
    detail: "Login opened in a terminal",
  });
  // Clipboard stub (navigator.clipboard is a getter under happy-dom).
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  cleanup();
});

describe("ProviderKeysSettings", () => {
  it("loads + shows 'no key' for all fixed providers when nothing configured", async () => {
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByText("Anthropic (Claude)"));
    expect(screen.getAllByText("no key").length).toBe(3);
  });

  it("shows subscription CLI setup rows, including Cursor", async () => {
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByText("Cursor CLI"));
    expect(screen.getByText("Claude CLI")).toBeInTheDocument();
    expect(screen.getByText("Codex CLI")).toBeInTheDocument();
    expect(screen.getByText("Cursor CLI")).toBeInTheDocument();
  });

  // ---- F040-01: 4-state CLI connection model -------------------------

  it("calls getCliStatus on mount but never the billable probe", async () => {
    render(<ProviderKeysSettings />);
    await waitFor(() =>
      expect(_mocked.getCliStatus).toHaveBeenCalledWith("cursor_cli"),
    );
    expect(_mocked.getCliStatus).toHaveBeenCalledWith("claude_cli");
    expect(_mocked.getCliStatus).toHaveBeenCalledWith("codex_cli");
    // The expensive probe is NEVER auto-run on mount.
    expect(_mocked.testProvider).not.toHaveBeenCalled();
  });

  it("re-detects (cheap) on window focus, still never the probe", async () => {
    render(<ProviderKeysSettings />);
    await waitFor(() =>
      expect(_mocked.getCliStatus).toHaveBeenCalledWith("cursor_cli"),
    );
    _mocked.getCliStatus.mockClear();
    fireEvent.focus(window);
    await waitFor(() =>
      expect(_mocked.getCliStatus).toHaveBeenCalledWith("cursor_cli"),
    );
    expect(_mocked.testProvider).not.toHaveBeenCalled();
  });

  it("not_installed: shows 'Not found' + Install (opens URL) + copy command + Locate", async () => {
    _mocked.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve(
        cliStatus({ provider: p, state: "not_installed", found: false, path: "", version: "" }),
      ),
    );
    render(<ProviderKeysSettings />);
    await waitFor(() =>
      expect(screen.getByTestId("cli-state-cursor_cli")).toHaveTextContent(
        "Not found",
      ),
    );

    fireEvent.click(screen.getByTestId("cli-install-cursor_cli"));
    await waitFor(() =>
      expect(_shellOpen).toHaveBeenCalledWith("https://cursor.com/cli"),
    );

    fireEvent.click(screen.getByTestId("cli-copy-install-cursor_cli"));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        LOGIN_META.installCommand,
      ),
    );
    // No probe.
    expect(_mocked.testProvider).not.toHaveBeenCalled();
  });

  it("installed (logged out): shows Detected text + Log in (copy) + Test", async () => {
    _mocked.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve(cliStatus({ provider: p, connected: false })),
    );
    render(<ProviderKeysSettings />);
    await waitFor(() =>
      expect(screen.getByTestId("cli-state-cursor_cli")).toHaveTextContent(
        "Detected",
      ),
    );
    const line = screen.getByTestId("cli-state-cursor_cli");
    expect(line).toHaveTextContent("Detected");
    expect(line).toHaveTextContent("/opt/homebrew/bin/agent");
    expect(line).toHaveTextContent("v1.2.3");
    expect(line).toHaveTextContent("not logged in");

    fireEvent.click(screen.getByTestId("cli-login-cursor_cli"));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("agent login"),
    );
  });

  // ---- F040-01 S5a: native login launcher ----------------------------

  it("Log in invokes launch_cli_login with {provider, binaryPath} when available", async () => {
    _mocked.cliLoginLaunchAvailable.mockResolvedValue(true);
    _mocked.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve(cliStatus({ provider: p, connected: false })),
    );
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByTestId("cli-login-cursor_cli"));
    // Wait for the cached availability probe to flip the label to "Log in".
    await waitFor(() =>
      expect(screen.getByTestId("cli-login-cursor_cli")).toHaveTextContent(
        /^Log in$/,
      ),
    );

    fireEvent.click(screen.getByTestId("cli-login-cursor_cli"));
    await waitFor(() =>
      // provider_class is passed through; the API client strips "_cli".
      expect(_mocked.launchCliLogin).toHaveBeenCalledWith(
        "cursor_cli",
        "/opt/homebrew/bin/agent",
      ),
    );
    // On a successful launch a "finish in Terminal" notice appears…
    await waitFor(() => screen.getByTestId("cli-login-notice-cursor_cli"));
    // …and the copy-command path was NOT used.
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
  });

  it("falls back to copy-command when launcher is unavailable", async () => {
    _mocked.cliLoginLaunchAvailable.mockResolvedValue(false);
    _mocked.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve(cliStatus({ provider: p, connected: false })),
    );
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByTestId("cli-login-cursor_cli"));
    expect(screen.getByTestId("cli-login-cursor_cli")).toHaveTextContent(
      "Log in (copy command)",
    );

    fireEvent.click(screen.getByTestId("cli-login-cursor_cli"));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("agent login"),
    );
    expect(_mocked.launchCliLogin).not.toHaveBeenCalled();
  });

  it("falls back to copy-command when launch_cli_login rejects", async () => {
    _mocked.cliLoginLaunchAvailable.mockResolvedValue(true);
    _mocked.launchCliLogin.mockRejectedValue(new Error("no terminal"));
    _mocked.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve(cliStatus({ provider: p, connected: false })),
    );
    render(<ProviderKeysSettings />);
    await waitFor(() =>
      expect(screen.getByTestId("cli-login-cursor_cli")).toHaveTextContent(
        /^Log in$/,
      ),
    );

    fireEvent.click(screen.getByTestId("cli-login-cursor_cli"));
    await waitFor(() => expect(_mocked.launchCliLogin).toHaveBeenCalled());
    // The reject path falls through to the copy-command floor.
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("agent login"),
    );
    // No probe, no token in the DOM.
    expect(_mocked.testProvider).not.toHaveBeenCalled();
    expect(document.body.innerHTML).not.toContain("sk-ant-");
  });

  it("Test button runs the billable probe and re-detects after", async () => {
    _mocked.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve(cliStatus({ provider: p, connected: false })),
    );
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByTestId("test-cursor_cli"));
    _mocked.getCliStatus.mockClear();
    fireEvent.click(screen.getByTestId("test-cursor_cli"));
    await waitFor(() =>
      expect(_mocked.testProvider).toHaveBeenCalledWith("cursor_cli"),
    );
    await waitFor(() => screen.getByText(/✓ Connected/));
    // After a successful Test it re-detects (cheap) so the cache flows in.
    await waitFor(() =>
      expect(_mocked.getCliStatus).toHaveBeenCalledWith("cursor_cli"),
    );
  });

  it("connected: shows ✓ Connected + login + Re-check", async () => {
    _mocked.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve(
        cliStatus({ provider: p, connected: true, login: "user@example.com", verifiedAt: "1718000000" }),
      ),
    );
    render(<ProviderKeysSettings />);
    await waitFor(() =>
      expect(screen.getAllByTestId("cli-state-cursor_cli")[0]).toHaveTextContent(
        "✓ Connected",
      ),
    );
    const line = screen.getAllByTestId("cli-state-cursor_cli")[0];
    expect(line).toHaveTextContent("user@example.com");
    expect(screen.getByTestId("test-cursor_cli")).toHaveTextContent("Re-check");
  });

  it("Locate binary opens the picker, sets the override, and re-detects", async () => {
    _pickPaths.mockResolvedValue(["/custom/path/agent"]);
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByTestId("cli-locate-cursor_cli"));
    fireEvent.click(screen.getByTestId("cli-locate-cursor_cli"));
    await waitFor(() => expect(_pickPaths).toHaveBeenCalled());
    await waitFor(() =>
      expect(_mocked.setCliBinary).toHaveBeenCalledWith(
        "cursor_cli",
        "/custom/path/agent",
      ),
    );
    await waitFor(() =>
      screen.getAllByTestId("cli-state-cursor_cli")[0].textContent?.includes(
        "/custom/path/agent",
      ),
    );
  });

  it("never renders a token in any CLI state", async () => {
    _mocked.getCliStatus.mockImplementation((p: string) =>
      Promise.resolve(
        cliStatus({ provider: p, connected: true, login: "user@example.com" }),
      ),
    );
    _mocked.testProvider.mockResolvedValue({
      ok: false,
      detail: "redacted: <token>",
      latency_ms: 5,
    });
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByTestId("cli-state-cursor_cli"));
    expect(document.body.innerHTML).not.toContain("sk-ant-");
    expect(document.body.innerHTML).not.toContain("CURSOR_API_KEY=");
  });

  it("shows masked preview when a provider is configured", async () => {
    _mocked.getProviderKeys.mockResolvedValue(ANTHROPIC_CONFIGURED);
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByText("Anthropic (Claude)"));
    expect(screen.getByText(/configured:/)).toBeInTheDocument();
    expect(screen.getByText("…1234")).toBeInTheDocument();
  });

  it("PUT round-trips and never re-shows the raw key", async () => {
    _mocked.putFixedProviderKey.mockResolvedValue(ANTHROPIC_CONFIGURED);
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByText("Anthropic (Claude)"));

    fireEvent.click(screen.getByTestId("edit-anthropic"));
    const input = await screen.findByPlaceholderText(/Anthropic.*API key/i);
    fireEvent.change(input, {
      target: { value: "sk-ant-DO-NOT-LEAK-1234" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() =>
      expect(_mocked.putFixedProviderKey).toHaveBeenCalledWith(
        "anthropic",
        "sk-ant-DO-NOT-LEAK-1234",
      ),
    );

    // After save the editor closes and the masked state takes over.
    await waitFor(() => screen.getByText("…1234"));
    // Raw key never present in the DOM after save.
    expect(document.body.innerHTML).not.toContain("DO-NOT-LEAK");
  });

  it("Replace button reopens the editor pre-cleared", async () => {
    _mocked.getProviderKeys.mockResolvedValue(ANTHROPIC_CONFIGURED);
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByText("…1234"));

    fireEvent.click(screen.getByTestId("edit-anthropic"));
    const input = await screen.findByPlaceholderText(/Anthropic.*API key/i);
    expect((input as HTMLInputElement).value).toBe("");
  });

  it("Clear deletes the key", async () => {
    _mocked.getProviderKeys.mockResolvedValue(ANTHROPIC_CONFIGURED);
    _mocked.deleteFixedProviderKey.mockResolvedValue(EMPTY_STATE);
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByText("…1234"));

    fireEvent.click(screen.getByText("Clear"));
    await waitFor(() =>
      expect(_mocked.deleteFixedProviderKey).toHaveBeenCalledWith(
        "anthropic",
      ),
    );
    await waitFor(() => expect(screen.getAllByText("no key").length).toBe(3));
  });

  it("Add custom provider form posts to the API", async () => {
    _mocked.putCustomProviderEntry.mockResolvedValue({
      ...EMPTY_STATE,
      custom: [{
        alias: "lmstudio",
        base_url: "http://127.0.0.1:1234/v1",
        api_style: "openai_chat_completions",
        auth_header: "Authorization",
        auth_prefix: "Bearer ",
        model: "",
        configured: true,
        key_preview: "…cret",
      }],
    });

    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByTestId("custom-add-button"));

    fireEvent.click(screen.getByTestId("custom-add-button"));
    const form = await screen.findByTestId("custom-add-form");
    expect(form).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("lmstudio"), {
      target: { value: "lmstudio" },
    });
    fireEvent.change(
      screen.getByPlaceholderText("http://127.0.0.1:1234/v1"),
      { target: { value: "http://127.0.0.1:1234/v1" } },
    );
    fireEvent.change(screen.getByPlaceholderText("(secret)"), {
      target: { value: "lm-secret" },
    });

    fireEvent.click(screen.getByText("Add"));

    await waitFor(() =>
      expect(_mocked.putCustomProviderEntry).toHaveBeenCalledWith({
        alias: "lmstudio",
        base_url: "http://127.0.0.1:1234/v1",
        api_key: "lm-secret",
        api_style: "openai_chat_completions",
      }),
    );

    // After save the entry shows up in the list.
    await waitFor(() => screen.getByText("custom.lmstudio"));
  });

  it("Custom delete calls the API", async () => {
    _mocked.getProviderKeys.mockResolvedValue({
      ...EMPTY_STATE,
      custom: [{
        alias: "lmstudio",
        base_url: "http://127.0.0.1:1234/v1",
        api_style: "openai_chat_completions",
        auth_header: "Authorization",
        auth_prefix: "Bearer ",
        model: "",
        configured: true,
        key_preview: "…cret",
      }],
    });
    _mocked.deleteCustomProviderEntry.mockResolvedValue(EMPTY_STATE);
    render(<ProviderKeysSettings />);
    await waitFor(() => screen.getByText("custom.lmstudio"));

    fireEvent.click(screen.getByText("Delete"));
    await waitFor(() =>
      expect(_mocked.deleteCustomProviderEntry).toHaveBeenCalledWith(
        "lmstudio",
      ),
    );
  });

  it("Surfaces a load error inline", async () => {
    _mocked.getProviderKeys.mockRejectedValue(new Error("boom"));
    render(<ProviderKeysSettings />);
    await waitFor(() =>
      screen.getByText(/Failed to load provider keys/),
    );
  });

  it("normalizes partial provider-key payloads before rendering", async () => {
    _mocked.getProviderKeys.mockResolvedValue({
      custom: "not-an-array",
    });

    render(<ProviderKeysSettings />);

    await waitFor(() => screen.getByText("Anthropic (Claude)"));
    expect(screen.getAllByText("no key").length).toBe(3);
    expect(screen.getByText("No custom providers configured.")).toBeInTheDocument();
  });
});
