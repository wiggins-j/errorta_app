// F033 — CouncilRoomEditor coverage.
//
// Locks the contract:
// - Loads room + provider catalog + per-provider routes on mount.
// - Renders one row per member.
// - Add member appends a draft row.
// - Delete removes the row.
// - Up/Down reorder.
// - Provider change resets the route to that provider's first known route.
// - Save PUTs the updated members + speaker_order.
// - 4xx surface inline; dirty cancel prompts confirm.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

vi.mock("../../lib/api/providerKeys", () => ({
  listGatewayProviders: vi.fn(),
  listGatewayRoutes: vi.fn(),
  listModelAvailability: vi.fn(),
}));
vi.mock("../../lib/api/councilRoom", () => ({
  getRoomFull: vi.fn(),
  putRoom: vi.fn(),
  validateRoom: vi.fn(),
}));
vi.mock("../../lib/api/corpus", () => ({
  listCorpora: vi.fn(),
}));
// F129 Slice 4/7: the editor gates the "Multi" option behind sidecarHealth's
// model_assignment_ready flag. Tests default it to true (Slice 7 ships safe).
vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, sidecarHealth: vi.fn() };
});
import * as coreApi from "../../lib/api";
const _coreApi = coreApi as unknown as {
  sidecarHealth: ReturnType<typeof vi.fn>;
};

import * as providerKeysApi from "../../lib/api/providerKeys";
import * as councilRoomApi from "../../lib/api/councilRoom";
import { listCorpora } from "../../lib/api/corpus";
import CouncilRoomEditor, {
  computeBudgetFloor,
  fallbackRouteId,
  groupRoutesByFamily,
  isRemoteProviderKind,
  isRouteStale,
  isProviderSelectable,
  isCliNeedsSetup,
  reasonLabel,
  poolGroupRank,
  groupPooledRoutes,
} from "./CouncilRoomEditor";
import { expectNoA11yViolations } from "../council/a11y-helpers";

const _pkApi = providerKeysApi as unknown as {
  listGatewayProviders: ReturnType<typeof vi.fn>;
  listGatewayRoutes: ReturnType<typeof vi.fn>;
  listModelAvailability: ReturnType<typeof vi.fn>;
};
const _crApi = councilRoomApi as unknown as {
  getRoomFull: ReturnType<typeof vi.fn>;
  putRoom: ReturnType<typeof vi.fn>;
  validateRoom: ReturnType<typeof vi.fn>;
};
const _corpusApi = {
  listCorpora: listCorpora as unknown as ReturnType<typeof vi.fn>,
};

function sampleRoom(): Record<string, unknown> {
  return {
    id: "r-1",
    name: "Demo",
    revision: 3,
    members: [
      {
        id: "m-1",
        name: "Alice",
        enabled: true,
        provider_kind: "anthropic",
        gateway_route_id: "anthropic.claude-sonnet-4-6",
        context_access: "full_context",
        transcript_access: "all_messages",
        system_prompt: "Be careful.",
        // arbitrary preserved field:
        metadata: { weight: 1 },
      },
      {
        id: "m-2",
        name: "Bob",
        enabled: false,
        provider_kind: "openai",
        gateway_route_id: "openai.gpt-4o",
        context_access: "redacted_summary",
        transcript_access: "own_messages",
        system_prompt: "",
      },
    ],
    topology: {
      kind: "round_robin",
      max_rounds: 1,
      speaker_order: ["m-1", "m-2"],
    },
    budget_policy: {
      max_total_model_calls: 2,
      max_remote_calls_per_run: 1,
      max_output_tokens_per_turn: 512,
      max_input_tokens_per_turn: 4096,
    },
  };
}

beforeEach(() => {
  Object.values(_pkApi).forEach((fn) => fn.mockReset());
  Object.values(_crApi).forEach((fn) => fn.mockReset());
  _corpusApi.listCorpora.mockReset();
  _coreApi.sidecarHealth.mockReset();
  _coreApi.sidecarHealth.mockResolvedValue({
    service: "s", version: "v", now: "n", aiar_available: true,
    features: { model_assignment_ready: true },
  });

  _crApi.getRoomFull.mockResolvedValue({
    room: sampleRoom(),
    validation: { status: "ready", errors: [] },
  });
  _pkApi.listGatewayProviders.mockResolvedValue({
    providers: [
      { provider_class: "local",     display_name: "Local",       configured: true },
      { provider_class: "anthropic", display_name: "Anthropic",   configured: true },
      { provider_class: "openai",    display_name: "OpenAI",      configured: true },
      { provider_class: "google",    display_name: "Google API",  configured: false },
      { provider_class: "custom",    display_name: "Custom",      configured: false },
      { provider_class: "claude_cli", display_name: "Claude CLI", configured: true },
      { provider_class: "codex_cli",  display_name: "Codex CLI",  configured: true },
      { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: true },
    ],
  });
  _pkApi.listGatewayRoutes.mockImplementation(async (p?: string) => {
    if (p === "local") return { routes: [{ route_id: "local.ollama.llama3.2:3b", label: "llama3.2", family: "ollama" }] };
    if (p === "anthropic") return { routes: [
      { route_id: "anthropic.claude-sonnet-4-6", label: "Sonnet", family: "sonnet" },
      { route_id: "anthropic.claude-opus-4-8",   label: "Opus",   family: "opus" },
    ] };
    if (p === "openai") return { routes: [{ route_id: "openai.gpt-4o", label: "4o", family: "gpt-4o" }] };
    if (p === "google") return { routes: [{ route_id: "google.gemini-1.5-pro", label: "Pro", family: "gemini" }] };
    if (p === "claude_cli") return { routes: [{ route_id: "claude_cli.opus", label: "Claude Opus", family: "opus" }] };
    if (p === "codex_cli") return { routes: [{ route_id: "codex_cli.default", label: "Codex", family: "codex" }] };
    if (p === "cursor_cli") return { routes: [
      { route_id: "cursor_cli.default", label: "Cursor Agent (account default)", family: "cursor" },
      { route_id: "cursor_cli.composer-2.5", label: "Cursor Composer 2.5", family: "cursor" },
      { route_id: "cursor_cli.composer-2.5-fast", label: "Cursor Composer 2.5 Fast", family: "cursor" },
      { route_id: "cursor_cli.gpt-5.3-codex", label: "Cursor Codex 5.3", family: "gpt" },
      { route_id: "cursor_cli.gpt-5.2", label: "Cursor GPT-5.2", family: "gpt" },
    ] };
    return { routes: [] };
  });
  _pkApi.listModelAvailability.mockResolvedValue({
    routes: [
      "local.ollama.llama3.2:3b",
      "anthropic.claude-sonnet-4-6",
      "anthropic.claude-opus-4-8",
      "openai.gpt-4o",
      "claude_cli.opus",
      "codex_cli.default",
      "cursor_cli.default",
      "cursor_cli.composer-2.5",
    ].map((route_id) => ({
      route_id,
      provider_family: route_id.split(".")[0],
      available: true,
      reason: "",
    })),
  });
  _corpusApi.listCorpora.mockResolvedValue([
    { name: "welcome", fileCount: 3, readyCount: 3, status: "ready", source: "local" },
    { name: "legal-mini", fileCount: 20, readyCount: 18, status: "indexing", source: "remote" },
  ]);
});

afterEach(() => cleanup());

describe("CouncilRoomEditor", () => {
  it("disables the Multi model-mode option when model_assignment_ready is false", async () => {
    _coreApi.sidecarHealth.mockResolvedValue({
      service: "s", version: "v", now: "n", aiar_available: true,
      features: { model_assignment_ready: false },
    });
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    const mode = await screen.findByLabelText("Model mode for Alice");
    // Wait for the flag to arrive.
    await waitFor(() => {
      const opt = mode.querySelector('option[value="multi"]') as HTMLOptionElement;
      expect(opt.disabled).toBe(true);
    });
    // And the hint copy is present alongside the still-Single member.
    expect(screen.getAllByText(/Multi-model assignment isn't available/i).length).toBeGreaterThan(0);
  });

  it("keeps a saved Multi pool that references an unavailable route visible (stale route)", async () => {
    // A room previously saved with an Anthropic Opus pool entry; the provider
    // is now unavailable at load time. The editor must NOT silently erase the
    // saved pool — it must surface the stale route so the user can decide.
    const staleRoom = sampleRoom() as Record<string, unknown>;
    const members = staleRoom.members as Array<Record<string, unknown>>;
    members[0].model_mode = "multi";
    members[0].model_pool = ["anthropic.claude-opus-4-8"];
    // Mark the saved route as unreachable in the availability projection.
    _pkApi.listModelAvailability.mockResolvedValue({
      routes: [{
        route_id: "anthropic.claude-opus-4-8",
        provider_class: "anthropic", available: false, reason: "no_api_key",
      }],
    });
    _crApi.getRoomFull.mockResolvedValue({
      room: staleRoom,
      validation: { status: "ready", errors: [] },
    });
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    // The Multi model-mode select is preserved on the loaded member.
    await waitFor(() => {
      const mode = screen.getByLabelText("Model mode for Alice") as HTMLSelectElement;
      expect(mode.value).toBe("multi");
    });
    // The pool checkbox for the saved-but-unavailable route stays rendered
    // (looked up by the route_id substring) — so the user isn't silently
    // stripped of their prior configuration.
    const pool = await screen.findByLabelText(/anthropic \/ Opus/);
    expect(pool).toBeInTheDocument();
  });

  it("round-trips an explicit Multi model pool", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    const mode = await screen.findByLabelText("Model mode for Alice");
    fireEvent.change(mode, { target: { value: "multi" } });
    const opus = await screen.findByLabelText(/anthropic \/ Opus/);
    fireEvent.click(opus);
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]);
    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const saved = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const member = (saved.members as Array<Record<string, unknown>>)[0];
    expect(member.model_mode).toBe("multi");
    expect(member.model_pool).toEqual(["anthropic.claude-opus-4-8"]);
    expect(member.metadata).toEqual({ weight: 1 });
  });

  // ---- F135: Multi CLI picker + redesigned pool ----------------------------

  it("reasonLabel maps known availability codes and passes unknown through", () => {
    expect(reasonLabel("no_api_key")).toBe("needs an API key");
    expect(reasonLabel("family_disabled")).toBe("family disabled in Settings");
    expect(reasonLabel("cli_not_connected")).toBe("CLI not connected");
    expect(reasonLabel("model_not_installed")).toBe("not installed in Ollama");
    expect(reasonLabel("something_new")).toBe("something_new");
    expect(reasonLabel("")).toBe("unavailable");
    expect(reasonLabel(undefined)).toBe("unavailable");
  });

  it("groupPooledRoutes orders CLIs first and local last", () => {
    const groups = groupPooledRoutes([
      { route_id: "local.x", label: "x", family: null, providerClass: "local" },
      { route_id: "anthropic.y", label: "y", family: null, providerClass: "anthropic" },
      { route_id: "cursor_cli.z", label: "z", family: null, providerClass: "cursor_cli" },
    ]);
    expect(groups.map((g) => g.providerClass)).toEqual([
      "cursor_cli",
      "anthropic",
      "local",
    ]);
    expect(poolGroupRank("claude_cli")).toBeLessThan(poolGroupRank("anthropic"));
    expect(poolGroupRank("anthropic")).toBeLessThan(poolGroupRank("local"));
  });

  it("CLI picker adds only the connected CLI's available models to the pool", async () => {
    _pkApi.listGatewayProviders.mockResolvedValue({
      providers: [
        { provider_class: "anthropic", display_name: "Anthropic", configured: true },
        { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: true, connected: true },
      ],
    });
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    const mode = await screen.findByLabelText("Model mode for Alice");
    fireEvent.change(mode, { target: { value: "multi" } });
    const add = await screen.findByTestId("cli-add-0");
    fireEvent.change(add, { target: { value: "cursor_cli" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]);
    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const member = (
      _crApi.putRoom.mock.calls[0][2].members as Array<Record<string, unknown>>
    )[0];
    // cursor_cli.default + cursor_cli.composer-2.5 are available in the default
    // availability mock; the .fast / .gpt routes are not and must be skipped.
    expect(new Set(member.model_pool as string[])).toEqual(
      new Set(["cursor_cli.default", "cursor_cli.composer-2.5"]),
    );
  });

  it("a CLI chip removes all of that CLI's routes; individual uncheck keeps the chip while one remains", async () => {
    _pkApi.listGatewayProviders.mockResolvedValue({
      providers: [
        { provider_class: "anthropic", display_name: "Anthropic", configured: true },
        { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: true, connected: true },
      ],
    });
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    const mode = await screen.findByLabelText("Model mode for Alice");
    fireEvent.change(mode, { target: { value: "multi" } });
    const add = await screen.findByTestId("cli-add-0");
    fireEvent.change(add, { target: { value: "cursor_cli" } });
    // Chip appears once the CLI's routes are pooled.
    await screen.findByTestId("cli-chip-remove-0-cursor_cli");
    // Uncheck one of the two pooled routes — the chip must remain (one left).
    const composer = screen.getByLabelText(/Cursor Composer 2\.5$/);
    fireEvent.click(composer);
    expect(
      screen.queryByTestId("cli-chip-remove-0-cursor_cli"),
    ).toBeInTheDocument();
    // Remove the chip — all remaining cursor routes leave the pool, chip gone.
    fireEvent.click(screen.getByTestId("cli-chip-remove-0-cursor_cli"));
    await waitFor(() =>
      expect(
        screen.queryByTestId("cli-chip-remove-0-cursor_cli"),
      ).not.toBeInTheDocument(),
    );
  });

  it("offers Set up (not add) and hides the CLI dropdown when no CLI is connected", async () => {
    _pkApi.listGatewayProviders.mockResolvedValue({
      providers: [
        { provider_class: "anthropic", display_name: "Anthropic", configured: true },
        { provider_class: "claude_cli", display_name: "Claude CLI", configured: true, connected: false },
      ],
    });
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    const mode = await screen.findByLabelText("Model mode for Alice");
    fireEvent.change(mode, { target: { value: "multi" } });
    await screen.findByText(/No subscription CLI is connected/);
    expect(screen.queryByTestId("cli-add-0")).not.toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /Set up subscription CLIs/ }).length,
    ).toBeGreaterThan(0);
  });

  it("collapses unavailable models by default and Clear all empties the pool", async () => {
    _pkApi.listGatewayProviders.mockResolvedValue({
      providers: [
        { provider_class: "anthropic", display_name: "Anthropic", configured: true },
        { provider_class: "google", display_name: "Google API", configured: false },
      ],
    });
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    const mode = await screen.findByLabelText("Model mode for Alice");
    fireEvent.change(mode, { target: { value: "multi" } });
    // Pick an available anthropic model so there's something to clear.
    const opus = await screen.findByLabelText(/anthropic \/ Opus/);
    fireEvent.click(opus);
    // The Google route is unavailable → hidden behind a collapsed <details>.
    const summary = await screen.findByText(/Show 1 unavailable/);
    const details = summary.closest("details") as HTMLDetailsElement | null;
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    // Clear all empties the pool.
    fireEvent.click(screen.getByTestId("pool-clear-0"));
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]);
    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const member = (
      _crApi.putRoom.mock.calls[0][2].members as Array<Record<string, unknown>>
    )[0];
    expect((member.model_pool as string[] | undefined) ?? []).toEqual([]);
  });

  it("has no serious/critical a11y violations in the Multi member editor", async () => {
    _pkApi.listGatewayProviders.mockResolvedValue({
      providers: [
        { provider_class: "anthropic", display_name: "Anthropic", configured: true },
        { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: true, connected: true },
      ],
    });
    const { container } = render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    const mode = await screen.findByLabelText("Model mode for Alice");
    fireEvent.change(mode, { target: { value: "multi" } });
    const add = await screen.findByTestId("cli-add-0");
    fireEvent.change(add, { target: { value: "cursor_cli" } });
    const primary = container.querySelector(".cre-member-primary");
    expect(primary).not.toBeNull();
    await expectNoA11yViolations(primary as HTMLElement);
  });

  it("computeBudgetFloor includes external steward headroom", () => {
    expect(
      computeBudgetFloor({
        enabledCount: 3,
        maxRounds: 2,
        remoteCount: 1,
        maxStewardCallsPerRun: 2,
        stewardIsRemote: true,
        maxCalloutsPerRun: 1,
        remoteCalloutTargetCount: 1,
      }),
    ).toEqual({
      maxTotalModelCallsFloor: 9,
      // remoteCount(1) * rounds(2) + remote steward(2) + remote callout(1) = 5
      maxRemoteCallsPerRunFloor: 5,
    });
  });

  it("isRemoteProviderKind counts API + subscription CLI providers, not local/fake", () => {
    expect(isRemoteProviderKind("anthropic")).toBe(true);
    expect(isRemoteProviderKind("openai")).toBe(true);
    expect(isRemoteProviderKind("google")).toBe(true);
    expect(isRemoteProviderKind("custom")).toBe(true);
    expect(isRemoteProviderKind("claude_cli")).toBe(true);
    expect(isRemoteProviderKind("codex_cli")).toBe(true);
    expect(isRemoteProviderKind("cursor_cli")).toBe(true);
    expect(isRemoteProviderKind("local")).toBe(false);
    expect(isRemoteProviderKind("fake")).toBe(false);
    expect(isRemoteProviderKind("")).toBe(false);
  });

  it("groupRoutesByFamily surfaces the account default and buckets by family", () => {
    const routes = [
      { route_id: "cursor_cli.default", label: "Cursor Agent (account default)", family: "cursor" },
      { route_id: "cursor_cli.gpt-5.3-codex", label: "Cursor Codex 5.3", family: "gpt" },
      { route_id: "cursor_cli.gpt-5.2", label: "Cursor GPT-5.2", family: "gpt" },
      { route_id: "cursor_cli.claude-4.5-sonnet", label: "Cursor Sonnet 4.5", family: "claude" },
      { route_id: "cursor_cli.gemini-3.1-pro", label: "Cursor Gemini 3.1 Pro", family: "gemini" },
      { route_id: "cursor_cli.grok-4.3", label: "Cursor Grok 4.3", family: "grok" },
      { route_id: "cursor_cli.kimi-k2.5", label: "Cursor Kimi K2.5", family: "cursor" },
    ];
    const { leading, groups } = groupRoutesByFamily(routes);
    // Account default is pulled out as a top-level option, never inside a group.
    expect(leading.map((r) => r.route_id)).toEqual(["cursor_cli.default"]);
    const labels = groups.map((g) => g.label);
    expect(labels).toEqual(["GPT / Codex", "Claude", "Gemini", "Grok", "Other"]);
    // GPT and Codex collapse into one group; unknown families fall to "Other".
    const gpt = groups.find((g) => g.label === "GPT / Codex")!;
    expect(gpt.routes.map((r) => r.route_id)).toEqual([
      "cursor_cli.gpt-5.3-codex",
      "cursor_cli.gpt-5.2",
    ]);
    expect(groups.find((g) => g.label === "Other")!.routes[0].route_id).toBe(
      "cursor_cli.kimi-k2.5",
    );
  });

  it("isRouteStale flags removed models on authoritative providers only", () => {
    const cat = {
      cursor_cli: [
        { route_id: "cursor_cli.default", label: "Default", family: "cursor" },
        { route_id: "cursor_cli.composer-2.5", label: "Composer", family: "cursor" },
      ],
      anthropic: [{ route_id: "anthropic.claude-opus-4-8", label: "Opus", family: "opus" }],
    };
    // Removed Cursor model → stale.
    expect(isRouteStale("cursor_cli.gpt-5", "cursor_cli", cat)).toBe(true);
    // Present model + the always-valid default → not stale.
    expect(isRouteStale("cursor_cli.composer-2.5", "cursor_cli", cat)).toBe(false);
    expect(isRouteStale("cursor_cli.default", "cursor_cli", cat)).toBe(false);
    // Non-authoritative provider (HTTP, free-form ids allowed) → never flagged.
    expect(isRouteStale("anthropic.some-future-model", "anthropic", cat)).toBe(false);
    // Empty catalog (discovery failed) → can't conclude, not flagged.
    expect(isRouteStale("cursor_cli.gpt-5", "cursor_cli", {})).toBe(false);
    expect(fallbackRouteId("cursor_cli", cat)).toBe("cursor_cli.default");
  });

  it("surfaces a stale member route and heals it to the account default", async () => {
    _crApi.getRoomFull.mockResolvedValue({
      room: {
        ...sampleRoom(),
        members: [
          {
            id: "m-1",
            name: "Dev",
            enabled: true,
            provider_kind: "cursor_cli",
            // A model Cursor removed — exactly the freeze-gap case.
            gateway_route_id: "cursor_cli.gpt-5-codex",
            context_access: "full_context",
            transcript_access: "all_messages",
          },
        ],
        speaker_order: ["m-1"],
      },
      validation: { status: "ready", errors: [] },
    });

    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    const fix = await waitFor(() => screen.getByTestId("route-stale-fix-0"));
    // The notice names the dead model and offers the live account default.
    expect(fix.textContent).toContain("cursor_cli.default");
    fireEvent.click(fix);
    // After healing, the stale notice is gone and the select shows the default.
    await waitFor(() =>
      expect(screen.queryByTestId("route-stale-fix-0")).toBeNull(),
    );
    expect((screen.getByTestId("route-0") as HTMLSelectElement).value).toBe(
      "cursor_cli.default",
    );
  });

  it("F040-01: CLI providers gate on connected, not configured", () => {
    // Connected CLI → selectable.
    expect(
      isProviderSelectable({
        provider_class: "claude_cli",
        display_name: "Claude CLI",
        configured: true,
        connected: true,
      }),
    ).toBe(true);
    // Installed but not connected (null or false) → NOT blindly selectable.
    expect(
      isProviderSelectable({
        provider_class: "claude_cli",
        display_name: "Claude CLI",
        configured: true,
        connected: null,
      }),
    ).toBe(false);
    expect(
      isProviderSelectable({
        provider_class: "claude_cli",
        display_name: "Claude CLI",
        configured: true,
        connected: false,
      }),
    ).toBe(false);
    // The "Set up" case is exactly installed-but-not-connected.
    expect(
      isCliNeedsSetup({
        provider_class: "claude_cli",
        display_name: "Claude CLI",
        configured: true,
        connected: null,
      }),
    ).toBe(true);
    // Non-CLI providers are unchanged — gate on configured only.
    expect(
      isProviderSelectable({
        provider_class: "anthropic",
        display_name: "Anthropic",
        configured: true,
      }),
    ).toBe(true);
    expect(
      isProviderSelectable({
        provider_class: "google",
        display_name: "Google",
        configured: false,
      }),
    ).toBe(false);
    expect(
      isCliNeedsSetup({
        provider_class: "anthropic",
        display_name: "Anthropic",
        configured: true,
      }),
    ).toBe(false);
  });

  it("F040-01: an installed-but-not-connected CLI option is disabled + shows Set up, connected is enabled", async () => {
    _pkApi.listGatewayProviders.mockResolvedValue({
      providers: [
        { provider_class: "local", display_name: "Local", configured: true },
        { provider_class: "anthropic", display_name: "Anthropic", configured: true },
        // installed but never verified → Set up
        { provider_class: "claude_cli", display_name: "Claude CLI", configured: true, connected: null },
        // verified → selectable
        { provider_class: "cursor_cli", display_name: "Cursor CLI", configured: true, connected: true },
      ],
    });
    const dispatched: string[] = [];
    const onNav = (e: Event) => {
      const d = (e as CustomEvent<{ view?: string }>).detail;
      if (d?.view) dispatched.push(d.view);
    };
    window.addEventListener("errorta:navigate", onNav);
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => screen.getByTestId("provider-0"));

    const select = screen.getByTestId("provider-0") as HTMLSelectElement;
    const opts = Array.from(select.options);
    const claude = opts.find((o) => o.value === "claude_cli")!;
    const cursor = opts.find((o) => o.value === "cursor_cli")!;
    expect(claude.disabled).toBe(true);
    expect(claude.textContent).toContain("Set up →");
    expect(cursor.disabled).toBe(false);
    expect(cursor.textContent).not.toContain("Set up");

    // The point-of-use Set up link deep-links to Settings.
    fireEvent.click(screen.getByTestId("provider-setup-link-0"));
    expect(dispatched).toContain("settings");
    window.removeEventListener("errorta:navigate", onNav);
  });

  it("renders one row per existing member", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByText(/Edit room/));
    expect(screen.getByTestId("member-row-0")).toBeInTheDocument();
    expect(screen.getByTestId("member-row-1")).toBeInTheDocument();
    const idInputs = screen.getAllByLabelText("Member id") as HTMLInputElement[];
    expect(idInputs.map((i) => i.value)).toEqual(["m-1", "m-2"]);
  });

  it("Add member appends a draft row", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("add-member"));
    fireEvent.click(screen.getByTestId("add-member"));
    await waitFor(() => screen.getByTestId("member-row-2"));
  });

  it("Add member applies open defaults and a unique name", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("add-member"));
    fireEvent.click(screen.getByTestId("add-member"));
    const row = await screen.findByTestId("member-row-2");
    const scoped = within(row);

    // (1) random, non-empty, unique name (not the fixture's Alice/Bob)
    const nameInput = scoped.getByLabelText("Member name") as HTMLInputElement;
    expect(nameInput.value).not.toBe("");
    expect(nameInput.value).not.toMatch(/^Member /);
    const allNames = (
      screen.getAllByLabelText("Member name") as HTMLInputElement[]
    ).map((i) => i.value);
    expect(new Set(allNames).size).toBe(allNames.length);

    // (2) full_context, (3) all_messages, surfaced as a plain-language preset.
    expect((scoped.getByLabelText("Member privacy") as HTMLSelectElement).value).toBe(
      "full",
    );
    expect(screen.queryByLabelText("Coding role")).not.toBeInTheDocument();
  });

  it("Delete removes a member", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("delete-0"));
    fireEvent.click(screen.getByTestId("delete-0"));
    await waitFor(() => {
      const ids = (
        screen.getAllByLabelText("Member id") as HTMLInputElement[]
      ).map((i) => i.value);
      expect(ids).toEqual(["m-2"]);
    });
  });

  it("Up arrow reorders members", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("member-row-0"));
    const moveUp = screen.getAllByLabelText("Move up")[1];
    fireEvent.click(moveUp);
    await waitFor(() => {
      const ids = (
        screen.getAllByLabelText("Member id") as HTMLInputElement[]
      ).map((i) => i.value);
      expect(ids).toEqual(["m-2", "m-1"]);
    });
  });

  it("Changing provider resets the route to the new provider's first route", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("provider-0"));
    fireEvent.change(screen.getByTestId("provider-0"), {
      target: { value: "openai" },
    });
    await waitFor(() => {
      const routeSelect = screen.getByTestId("route-0") as HTMLSelectElement;
      expect(routeSelect.value).toBe("openai.gpt-4o");
    });
  });

  it("Save PUTs the updated members + speaker_order with expected revision", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    const onSaved = vi.fn();
    render(
      <CouncilRoomEditor
        roomId="r-1"
        onClose={vi.fn()}
        onSaved={onSaved}
      />,
    );
    await waitFor(() => screen.getByTestId("add-member"));

    fireEvent.click(screen.getByTestId("delete-1")); // drop m-2

    await waitFor(() => {
      expect(
        (screen.getByTestId("save-room") as HTMLButtonElement).disabled,
      ).toBe(false);
    });
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() =>
      expect(_crApi.putRoom).toHaveBeenCalledWith(
        "r-1",
        3,
        expect.objectContaining({
          members: expect.arrayContaining([
            expect.objectContaining({ id: "m-1" }),
          ]),
          topology: expect.objectContaining({
            speaker_order: ["m-1"],
          }),
        }),
      ),
    );
    expect(onSaved).toHaveBeenCalled();
  });

  it("Save persists a member's coding role into metadata (F087)", async () => {
    const room = sampleRoom();
    room.preset_id = "coding";
    room.members = [
      {
        ...(room.members as Array<Record<string, unknown>>)[0],
        metadata: { weight: 1, coding_role: "dev" },
      },
      (room.members as Array<Record<string, unknown>>)[1],
    ];
    _crApi.getRoomFull.mockResolvedValue({
      room,
      validation: { status: "ready", errors: [] },
    });
    _crApi.putRoom.mockResolvedValue({
      room: { ...room, revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => screen.getByTestId("add-member"));
    const roleSelects = screen.getAllByLabelText("Coding role") as HTMLSelectElement[];
    fireEvent.change(roleSelects[0], { target: { value: "pm" } });
    await waitFor(() =>
      expect((screen.getByTestId("save-room") as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(screen.getByTestId("save-room"));
    await waitFor(() =>
      expect(_crApi.putRoom).toHaveBeenCalledWith(
        "r-1",
        3,
        expect.objectContaining({
          members: expect.arrayContaining([
            expect.objectContaining({
              id: "m-1",
              metadata: expect.objectContaining({ coding_role: "pm" }),
            }),
          ]),
        }),
      ),
    );
  });

  it("Save PUTs an edited room name", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), name: "Renamed room", revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("room-name-input"));

    fireEvent.change(screen.getByTestId("room-name-input"), {
      target: { value: "Renamed room" },
    });
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    expect(sentRoom.name).toBe("Renamed room");
  });

  it("lists catalog corpora and persists selected room corpus_ids", async () => {
    const room = { ...sampleRoom(), corpus_ids: ["welcome"] };
    _crApi.getRoomFull.mockResolvedValue({
      room,
      validation: { status: "ready", errors: [] },
    });
    _crApi.putRoom.mockResolvedValue({
      room: { ...room, corpus_ids: ["welcome", "legal-mini"], revision: 4 },
      validation: { status: "ready", errors: [] },
    });

    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    // WS-E: multi-select is a checkbox list now. welcome is pre-checked.
    const welcome = (await screen.findByRole("checkbox", {
      name: /welcome/,
    })) as HTMLInputElement;
    expect(welcome.checked).toBe(true);
    const legal = screen.getByRole("checkbox", { name: /legal-mini/ }) as HTMLInputElement;
    expect(legal.checked).toBe(false);
    fireEvent.click(legal);
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    expect(sentRoom.corpus_ids).toEqual(["welcome", "legal-mini"]);
  });

  it("F084: enabling Steelman + topic writes metadata.steelman and preserves other metadata", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("steelman-0"));
    // Topic box is disabled until the checkbox is on.
    expect((screen.getByTestId("steelman-topic-0") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("steelman-0"));
    fireEvent.change(screen.getByTestId("steelman-topic-0"), {
      target: { value: "Existence of Santa" },
    });
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const m1 = (sentRoom.members as Array<Record<string, unknown>>)[0];
    expect(m1.metadata).toEqual({
      weight: 1, // preserved
      steelman: true,
      steelman_topic: "Existence of Santa",
    });
  });

  it("F084: a non-steelman member writes no steelman metadata keys", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("room-name-input"));
    fireEvent.change(screen.getByTestId("room-name-input"), { target: { value: "X" } });
    fireEvent.click(screen.getByTestId("save-room"));
    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const m1 = (sentRoom.members as Array<Record<string, unknown>>)[0];
    expect(m1.metadata).toEqual({ weight: 1 });
    expect((m1.metadata as Record<string, unknown>).steelman).toBeUndefined();
  });

  it("Save preserves untouched fields on each member", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("member-row-0"));
    // Touch enabled to flip dirty state.
    fireEvent.click(screen.getByTestId("enable-1"));
    fireEvent.click(screen.getByTestId("save-room"));
    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const sentMembers = sentRoom.members as Array<Record<string, unknown>>;
    const m1 = sentMembers.find((m) => m.id === "m-1") as Record<string, unknown>;
    // metadata round-trips through _extra.
    expect(m1.metadata).toEqual({ weight: 1 });
  });

  it("saves member-mode council steward settings", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("steward-enabled"));

    fireEvent.click(screen.getByTestId("steward-enabled"));
    fireEvent.change(screen.getByTestId("steward-assignment"), {
      target: { value: "member" },
    });
    fireEvent.change(screen.getByTestId("steward-member"), {
      target: { value: "m-1" },
    });
    fireEvent.change(screen.getByTestId("steward-recent"), {
      target: { value: "1" },
    });
    fireEvent.change(screen.getByTestId("steward-max-packet"), {
      target: { value: "1600" },
    });
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const stewardPolicy = sentRoom.steward_policy as Record<string, unknown>;
    const assignment = stewardPolicy.assignment as Record<string, unknown>;
    expect(stewardPolicy.enabled).toBe(true);
    expect(stewardPolicy.recent_full_messages).toBe(1);
    expect(stewardPolicy.max_packet_tokens).toBe(1600);
    expect(assignment.mode).toBe("member");
    expect(assignment.member_id).toBe("m-1");
  });

  it("Token-saver preset applies the efficiency + topology bundle and saves it", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("preset-saver"));

    fireEvent.click(screen.getByTestId("preset-saver"));
    await waitFor(() => screen.getByText(/Applied/));
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const topology = sentRoom.topology as Record<string, unknown>;
    const eff = sentRoom.context_efficiency as Record<string, unknown>;
    const compaction = eff.transcript_compaction as Record<string, unknown>;
    const steward = sentRoom.steward_policy as Record<string, unknown>;
    expect(topology.kind).toBe("consensus_deliberation");
    expect(topology.max_rounds).toBe(3);
    expect(eff.deliberation_style).toBe("telegraphic");
    expect(eff.deliberation_dialect).toBe("digest_v1");
    expect(eff.citation_references).toBe(true);
    expect(eff.prompt_cache_hints).toBe(true);
    expect(compaction.enabled).toBe(true);
    expect(steward.enabled).toBe(true);
  });

  it("Credibility preset sets the topology, finalization, tools, and policy", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 5 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("preset-credibility"));

    fireEvent.click(screen.getByTestId("preset-credibility"));
    await waitFor(() => screen.getByText(/Applied/));
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const topology = sentRoom.topology as Record<string, unknown>;
    const finalization = sentRoom.finalization_policy as Record<string, unknown>;
    const tools = sentRoom.tool_policy as Record<string, unknown>;
    const credibility = sentRoom.credibility_policy as Record<string, unknown>;
    expect(topology.kind).toBe("credibility");
    expect(finalization.mode).toBe("credibility_report");
    expect((tools.web_search as Record<string, unknown>).enabled).toBe(true);
    expect((tools.web_fetch as Record<string, unknown>).enabled).toBe(true);
    expect(credibility.enabled).toBe(true);
  });

  it("groups a large provider route catalog into <optgroup>s in the dropdown", async () => {
    _crApi.getRoomFull.mockResolvedValue({
      room: {
        ...sampleRoom(),
        members: [
          {
            id: "m-1",
            name: "Dev",
            enabled: true,
            provider_kind: "cursor_cli",
            gateway_route_id: "cursor_cli.gpt-5.3-codex",
            context_access: "full_context",
            transcript_access: "all_messages",
          },
        ],
        speaker_order: ["m-1"],
      },
      validation: { status: "ready", errors: [] },
    });
    const families: [string, string][] = [
      ["gpt-5.3-codex", "gpt"], ["gpt-5.3-codex-high", "gpt"], ["gpt-5.2", "gpt"],
      ["gpt-5.1", "gpt"], ["claude-4.5-sonnet", "claude"], ["claude-4.5-opus-high", "claude"],
      ["gemini-3.1-pro", "gemini"], ["gemini-3-flash", "gemini"], ["grok-4.3", "grok"],
      ["kimi-k2.5", "cursor"], ["glm-5.2", "cursor"],
    ];
    _pkApi.listGatewayRoutes.mockImplementation(async (p?: string) => {
      if (p === "cursor_cli") return { routes: [
        { route_id: "cursor_cli.default", label: "Cursor Agent (account default)", family: "cursor" },
        ...families.map(([id, fam]) => ({ route_id: `cursor_cli.${id}`, label: `Cursor ${id}`, family: fam })),
      ] };
      return { routes: [] };
    });

    render(<CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />);
    const select = await waitFor(() => screen.getByTestId("route-0"));

    // 12 routes (> threshold) → grouped by family in a stable order.
    const optgroups = Array.from(select.querySelectorAll("optgroup")).map((g) =>
      g.getAttribute("label"),
    );
    expect(optgroups).toEqual(["GPT / Codex", "Claude", "Gemini", "Grok", "Other"]);
    // Account default stays a top-level option (never buried inside a group).
    expect(select.querySelector('option[value="cursor_cli.default"]')).not.toBeNull();
    expect(select.querySelector('optgroup option[value="cursor_cli.default"]')).toBeNull();
  });

  it("Coding preset builds a mixed CLI coding-team roster with safe code defaults", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 6 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("preset-coding"));

    fireEvent.click(screen.getByTestId("preset-coding"));
    await waitFor(() => screen.getByText(/Applied/));
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const members = sentRoom.members as Array<Record<string, unknown>>;
    const roles = members.map(
      (m) => ((m.metadata as Record<string, unknown>).coding_role as string),
    );
    const providers = new Set(members.map((m) => m.provider_kind));
    const names = members.map((m) => String(m.name));
    const pm = members.find(
      (m) => (m.metadata as Record<string, unknown>).coding_role === "pm",
    )!;
    const topology = sentRoom.topology as Record<string, unknown>;
    const finalization = sentRoom.finalization_policy as Record<string, unknown>;
    const tools = sentRoom.tool_policy as Record<string, Record<string, unknown>>;
    const steward = sentRoom.steward_policy as Record<string, unknown>;
    const budget = sentRoom.budget_policy as Record<string, unknown>;

    expect(members).toHaveLength(8);
    expect(roles.filter((role) => role === "pm")).toHaveLength(1);
    expect(roles.filter((role) => role === "dev")).toHaveLength(3);
    expect(roles.filter((role) => role === "reviewer")).toHaveLength(2);
    expect(roles.filter((role) => role === "tester")).toHaveLength(2);
    expect(new Set(names).size).toBe(8);
    expect(names).not.toContain("Alice");
    expect(names).not.toContain("Bob");
    expect(providers).toEqual(new Set(["claude_cli", "cursor_cli", "codex_cli"]));
    expect(pm.provider_kind).toBe("claude_cli");
    expect(pm.gateway_route_id).toBe("claude_cli.opus");
    // Cursor coding members run on Composer.
    const cursorMembers = members.filter((m) => m.provider_kind === "cursor_cli");
    expect(cursorMembers).toHaveLength(3);
    for (const m of cursorMembers) {
      expect(m.gateway_route_id).toBe("cursor_cli.composer-2.5");
    }
    // Every seeded route must resolve to a model the provider actually offers
    // (the routes the mock returned) — never a stale id that the provider would
    // reject at run time.
    const offered = new Set([
      "claude_cli.opus",
      "codex_cli.default",
      "cursor_cli.default",
      "cursor_cli.composer-2.5",
      "cursor_cli.composer-2.5-fast",
      "cursor_cli.gpt-5.3-codex",
      "cursor_cli.gpt-5.2",
    ]);
    for (const m of members) {
      expect(offered.has(String(m.gateway_route_id))).toBe(true);
    }
    expect(topology.kind).toBe("round_robin");
    expect(topology.max_rounds).toBe(8);
    expect(finalization.mode).toBe("single_finalizer");
    expect(finalization.finalizer_member_id).toBe("m-pm");
    expect((tools.code_read as Record<string, unknown>).enabled).toBe(true);
    expect((tools.code_write as Record<string, unknown>).enabled).toBe(true);
    expect((tools.code_write as Record<string, unknown>).mode).toBe("propose_only");
    expect((tools.code_exec as Record<string, unknown>).enabled).toBe(true);
    expect((tools.execution as Record<string, unknown>).sandbox).toBe("seatbelt");
    expect(tools.require_first_use_consent).toBe(true);
    expect(steward.enabled).toBe(true);
    expect((steward.assignment as Record<string, unknown>).member_id).toBe("m-pm");
    expect(Number(budget.max_total_model_calls)).toBeGreaterThanOrEqual(64);
    expect(Number(budget.max_remote_calls_per_run)).toBeGreaterThanOrEqual(64);
  });

  it("renders compact preset state without depending on card copy", async () => {
    const { container } = render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("preset-saver"));

    expect(container.querySelector(".cre-preset-blurb")).toBeNull();
    expect(screen.getByTestId("preset-saver")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByText(/Choose a preset to seed flow/)).toBeInTheDocument();

    fireEvent.mouseEnter(screen.getByTestId("preset-marathon"));
    expect(screen.getByText(/Open-ended deliberation/)).toBeInTheDocument();
    fireEvent.mouseLeave(screen.getByTestId("preset-marathon"));

    fireEvent.click(screen.getByTestId("preset-saver"));
    expect(screen.getByTestId("preset-saver")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText(/Review changes before saving/)).toBeInTheDocument();
  });

  it("Adding subscription-CLI members lifts the remote-call budget so save is valid", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("provider-0"));

    // Member 0 -> Claude CLI, member 1 -> Codex CLI (and enable it).
    fireEvent.change(screen.getByTestId("provider-0"), {
      target: { value: "claude_cli" },
    });
    fireEvent.change(screen.getByTestId("provider-1"), {
      target: { value: "codex_cli" },
    });
    fireEvent.click(screen.getByTestId("enable-1")); // m-2 starts disabled
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const budget = sentRoom.budget_policy as Record<string, unknown>;
    // Both CLI members are remote -> the per-run remote cap must cover them
    // (this is what was 0 before the fix, causing remote_member_zero_budget).
    expect(Number(budget.max_remote_calls_per_run)).toBeGreaterThanOrEqual(2);
  });

  it("round-trips existing callout config and includes callout budget headroom", async () => {
    const roomWithCallouts = {
      ...sampleRoom(),
      escalation_policy: {
        enabled: true,
        max_callouts_per_run: 2,
      },
      escalation_roster: [
        {
          target_id: "expert-1",
          provider_kind: "anthropic",
          gateway_route_id: "anthropic.claude-opus-4-8",
        },
      ],
      budget_policy: {
        max_total_model_calls: 1,
        max_remote_calls_per_run: 0,
        max_output_tokens_per_turn: 512,
        max_input_tokens_per_turn: 4096,
      },
    };
    _crApi.getRoomFull.mockResolvedValue({
      room: roomWithCallouts,
      validation: { status: "ready", errors: [] },
    });
    _crApi.putRoom.mockResolvedValue({
      room: { ...roomWithCallouts, revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("member-row-0"));

    fireEvent.change(screen.getAllByLabelText("Member name")[0], {
      target: { value: "Alice updated" },
    });
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const budget = sentRoom.budget_policy as Record<string, unknown>;
    expect(sentRoom.escalation_policy).toEqual(roomWithCallouts.escalation_policy);
    expect(sentRoom.escalation_roster).toEqual(roomWithCallouts.escalation_roster);
    expect(Number(budget.max_total_model_calls)).toBeGreaterThanOrEqual(3);
    expect(Number(budget.max_remote_calls_per_run)).toBeGreaterThanOrEqual(2);
  });

  it("Marathon preset enables open-ended consensus with a steward leader", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("preset-marathon"));

    fireEvent.click(screen.getByTestId("preset-marathon"));
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const topology = sentRoom.topology as Record<string, unknown>;
    const eff = sentRoom.context_efficiency as Record<string, unknown>;
    const compaction = eff.transcript_compaction as Record<string, unknown>;
    const steward = sentRoom.steward_policy as Record<string, unknown>;
    const stewardAssignment = steward.assignment as Record<string, unknown>;
    const budget = sentRoom.budget_policy as Record<string, unknown>;
    expect(topology.kind).toBe("consensus_deliberation");
    expect(topology.max_rounds).toBe(100);
    expect(compaction.enabled).toBe(true);
    expect(steward.enabled).toBe(true);
    expect(stewardAssignment.mode).toBe("member");
    // Leader defaults to an existing member.
    expect(stewardAssignment.member_id).toBe("m-1");
    // Save auto-lifts the budget floor to cover the long run.
    expect(Number(budget.max_total_model_calls)).toBeGreaterThanOrEqual(100);
  });

  it("Quick answers preset resets to a single round of plain prose", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("preset-quick"));

    fireEvent.click(screen.getByTestId("preset-quick"));
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const topology = sentRoom.topology as Record<string, unknown>;
    const eff = sentRoom.context_efficiency as Record<string, unknown>;
    expect(topology.kind).toBe("round_robin");
    expect(topology.max_rounds).toBe(1);
    expect(eff.deliberation_style).toBe("natural");
    expect(eff.deliberation_dialect).toBe("prose");
  });

  it("grants tools through the Tools section and round-trips tool_policy", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("tool-web-fetch"));

    fireEvent.click(screen.getByTestId("tool-web-fetch"));
    fireEvent.change(screen.getByTestId("tool-web-fetch-domains"), {
      target: { value: "example.com, docs.python.org" },
    });
    fireEvent.click(screen.getByTestId("tool-code-read"));
    fireEvent.change(screen.getByTestId("tool-workspace-path"), {
      target: { value: "/Users/you/project" },
    });
    fireEvent.click(screen.getByTestId("tool-code-write"));
    fireEvent.change(screen.getByTestId("tool-code-write-mode"), {
      target: { value: "auto_apply" },
    });
    fireEvent.click(screen.getByTestId("tool-code-exec"));
    fireEvent.change(screen.getByTestId("tool-code-exec-sandbox"), {
      target: { value: "seatbelt" },
    });
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sent = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const tp = sent.tool_policy as Record<string, Record<string, unknown>>;
    expect(tp.web_fetch.enabled).toBe(true);
    expect(tp.web_fetch.allowed_domains).toEqual(["example.com", "docs.python.org"]);
    expect(tp.code_read.enabled).toBe(true);
    expect(tp.code_read.workspace_path).toBe("/Users/you/project");
    expect(tp.code_write.enabled).toBe(true);
    expect(tp.code_write.mode).toBe("auto_apply");
    // code_exec sandbox tier round-trips through execution.sandbox.
    expect(tp.code_exec.enabled).toBe(true);
    expect(tp.execution.sandbox).toBe("seatbelt");
  });

  it("lifts budget caps for a remote external steward", async () => {
    const room = {
      ...sampleRoom(),
      steward_policy: {
        enabled: true,
        assignment: {
          mode: "external",
          provider_kind: "anthropic",
          gateway_route_id: "anthropic.claude-opus-4-8",
          name: "Council Steward",
        },
        remote_steward_allowed: false,
      },
    };
    _crApi.getRoomFull.mockResolvedValue({
      room,
      validation: { status: "ready", errors: [] },
    });
    _crApi.putRoom.mockResolvedValue({
      room: { ...room, revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("steward-enabled"));

    expect(screen.queryByTestId("steward-provider")).not.toBeInTheDocument();
    expect(screen.queryByTestId("steward-route")).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("steward-max-calls"), {
      target: { value: "3" },
    });
    fireEvent.click(screen.getByTestId("steward-remote-allowed"));
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sentRoom = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const budgetPolicy = sentRoom.budget_policy as Record<string, unknown>;
    const stewardPolicy = sentRoom.steward_policy as Record<string, unknown>;
    const assignment = stewardPolicy.assignment as Record<string, unknown>;
    expect(assignment.mode).toBe("external");
    expect(assignment.gateway_route_id).toBe("anthropic.claude-opus-4-8");
    expect(stewardPolicy.remote_steward_allowed).toBe(true);
    expect(budgetPolicy.max_total_model_calls).toBeGreaterThanOrEqual(4);
    expect(budgetPolicy.max_remote_calls_per_run).toBeGreaterThanOrEqual(4);
    expect(budgetPolicy.max_steward_calls_per_run).toBe(3);
    expect(budgetPolicy.max_remote_steward_calls_per_run).toBe(3);
  });

  it("Close without changes calls onClose directly", async () => {
    const onClose = vi.fn();
    render(
      <CouncilRoomEditor roomId="r-1" onClose={onClose} onSaved={vi.fn()} />,
    );
    // With no unsaved changes the button reads "Close", not "Cancel".
    await waitFor(() => screen.getAllByText("Close"));
    fireEvent.click(screen.getAllByText("Close")[0]);
    expect(onClose).toHaveBeenCalled();
  });

  it("Surfaces a load error inline", async () => {
    _crApi.getRoomFull.mockRejectedValue(new Error("boom"));
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByText(/Failed to load room/));
  });

  it("Renders validation errors", async () => {
    _crApi.getRoomFull.mockResolvedValue({
      room: sampleRoom(),
      validation: {
        status: "blocked_by_policy",
        errors: [
          { path: "members[0].context_access", code: "full_context_not_allowed" },
        ],
      },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByText(/full_context_not_allowed/));
  });

  it("shows each member's system prompt; Insert example fills a persona", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    // Member 0's configured prompt loads into its textarea.
    const ta0 = (await screen.findByTestId(
      "system-prompt-0",
    )) as HTMLTextAreaElement;
    expect(ta0.value).toBe("Be careful.");

    // Member 1 starts blank; the example button fills a skeptic persona.
    const ta1 = screen.getByTestId("system-prompt-1") as HTMLTextAreaElement;
    expect(ta1.value).toBe("");
    fireEvent.click(screen.getByTestId("insert-example-1"));
    expect(ta1.value.length).toBeGreaterThan(20);
    expect(ta1.value.toLowerCase()).toMatch(/skeptic|disagree|convince/);
  });

  it("edits a member's system prompt via the textarea", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    const ta = (await screen.findByTestId(
      "system-prompt-0",
    )) as HTMLTextAreaElement;
    fireEvent.change(ta, {
      target: { value: "You are furious and never concede." },
    });
    expect(ta.value).toBe("You are furious and never concede.");
  });

  it("groups advanced sections into collapsible <details>, basics stay open", async () => {
    const { container } = render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await screen.findByTestId("member-row-0");
    // The three advanced sections are collapsible <details>.
    expect(container.querySelector("details.cre-budget")).toBeTruthy();
    expect(container.querySelector("details.cre-context-efficiency")).toBeTruthy();
    expect(container.querySelector("details.cre-steward")).toBeTruthy();
    // They are collapsed by default (no `open` attribute).
    expect(container.querySelector("details.cre-budget")?.hasAttribute("open")).toBe(false);
    // The basics remain plain open sections.
    expect(container.querySelector("section.cre-topology")).toBeTruthy();
    expect(container.querySelector("section.cre-finalization")).toBeTruthy();
    // The advanced divider is present.
    expect(container.querySelector(".cre-advanced-divider")).toBeTruthy();
  });

  it("uses the canonical Council Steward label", async () => {
    const { container } = render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await screen.findByTestId("member-row-0");
    const stewardTitle = container.querySelector(
      ".cre-steward .cre-group-title",
    );

    expect(stewardTitle).toHaveTextContent(/^Council Steward$/);
    expect(screen.queryByText("Council Steward (context leader)")).toBeNull();
  });

  // F111: the editor must not offer finalization modes / room-wide token caps the
  // engine silently ignores.
  it("F111: inert finalization modes are shown disabled; implemented ones enabled", async () => {
    const { container } = render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("provider-0"));
    // The finalization mode <select> is the one carrying a consensus_report option.
    const selects = Array.from(
      container.querySelectorAll("section.cre-finalization select"),
    ) as HTMLSelectElement[];
    const modeSelect = selects.find((s) =>
      Array.from(s.options).some((o) => o.value === "consensus_report"),
    )!;
    const opt = (v: string) =>
      Array.from(modeSelect.options).find((o) => o.value === v)!;
    // Inert -> disabled.
    expect(opt("vote_summary").disabled).toBe(true);
    expect(opt("judged_final_answer").disabled).toBe(true);
    expect(opt("vote_summary").textContent).toContain("not implemented yet");
    // Implemented -> enabled (F031-28 added `summary`).
    expect(opt("transcript_only").disabled).toBe(false);
    expect(opt("single_finalizer").disabled).toBe(false);
    expect(opt("consensus_report").disabled).toBe(false);
    expect(opt("summary").disabled).toBe(false);
    expect(opt("credibility_report").disabled).toBe(false);
  });

  it("F111/F124: room-wide token-cap inputs stay hidden until enforced", async () => {
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("provider-0"));
    expect(screen.queryByLabelText(/Max output tokens per turn/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Max input tokens per turn/i)).not.toBeInTheDocument();
    expect(screen.getByText(/preserved for existing rooms but are hidden/i)).toBeInTheDocument();
  });

  it("F124: SearXNG URL is not edited in the room but still round-trips", async () => {
    const room = {
      ...sampleRoom(),
      tool_policy: {
        web_search: {
          enabled: true,
          searxng_url: "https://searxng.example.com",
        },
      },
    };
    _crApi.getRoomFull.mockResolvedValue({
      room,
      validation: { status: "ready", errors: [] },
    });
    _crApi.putRoom.mockResolvedValue({
      room: { ...room, revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("tool-web-search"));
    expect(screen.queryByTestId("tool-web-search-url")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("tool-web-search"));
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sent = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const toolPolicy = sent.tool_policy as Record<string, Record<string, unknown>>;
    expect(toolPolicy.web_search.searxng_url).toBe("https://searxng.example.com");
  });

  it("F124: Save tokens toggle applies the recommended context-efficiency bundle", async () => {
    _crApi.putRoom.mockResolvedValue({
      room: { ...sampleRoom(), revision: 4 },
      validation: { status: "ready", errors: [] },
    });
    render(
      <CouncilRoomEditor roomId="r-1" onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    await waitFor(() => screen.getByTestId("context-save-tokens"));
    fireEvent.click(screen.getByTestId("context-save-tokens"));
    fireEvent.click(screen.getByTestId("save-room"));

    await waitFor(() => expect(_crApi.putRoom).toHaveBeenCalled());
    const sent = _crApi.putRoom.mock.calls[0][2] as Record<string, unknown>;
    const efficiency = sent.context_efficiency as Record<string, unknown>;
    expect(efficiency.deliberation_style).toBe("telegraphic");
    expect(efficiency.deliberation_dialect).toBe("digest_v1");
    expect(efficiency.citation_references).toBe(true);
    expect((efficiency.transcript_compaction as Record<string, unknown>).enabled).toBe(true);
    expect(efficiency.prompt_cache_hints).toBe(true);
  });
});
