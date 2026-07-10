// F034 — typed client for the /provider-keys + /gateway routes.
//
// Masking is server-side: the client never sees a raw API key after
// a PUT. The Settings UI uses the typed shapes here.
import { getJSON, sidecarFetch } from "../api";
import { loadTauriInvoke } from "../sidecarPort";

const UI_ORIGIN = { "x-errorta-origin": "tauri-ui" } as const;

export type ApiStyle =
  | "openai_chat_completions"
  | "anthropic_messages"
  | "raw";

export interface FixedKeySummary {
  configured: boolean;
  key_preview: string | null;
}

export interface CustomEntrySummary {
  alias: string;
  base_url: string;
  api_style: ApiStyle | "";
  auth_header: string;
  auth_prefix: string;
  model: string;
  configured: boolean;
  key_preview: string | null;
}

export interface ProviderKeysMasked {
  anthropic: FixedKeySummary;
  openai: FixedKeySummary;
  google: FixedKeySummary;
  custom: CustomEntrySummary[];
}

export interface ProviderListItem {
  provider_class: string;
  display_name: string;
  configured: boolean;
  // F040-01 — for subscription CLI providers, the cached live-probe result
  // (null until the user explicitly Tests). `configured` stays = installed.
  connected?: boolean | null;
}

export interface GatewayProviderList {
  providers: ProviderListItem[];
}

export interface RouteListItem {
  route_id: string;
  label: string;
  family: string | null;
  provider_class?: string;
}

export interface GatewayRouteList {
  routes: RouteListItem[];
  provider_class?: string;
}

export interface RouteAvailabilityItem {
  route_id: string;
  provider_family: string;
  available: boolean;
  reason: string;
}

export interface RouteAvailabilityList {
  routes: RouteAvailabilityItem[];
}

export type FixedProvider = "anthropic" | "openai" | "google";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function normalizeFixedSummary(raw: unknown): FixedKeySummary {
  const obj = isRecord(raw) ? raw : {};
  return {
    configured: obj.configured === true,
    key_preview: stringOrNull(obj.key_preview),
  };
}

function normalizeApiStyle(value: unknown): ApiStyle | "" {
  return value === "openai_chat_completions" ||
    value === "anthropic_messages" ||
    value === "raw"
    ? value
    : "";
}

function normalizeCustomEntry(raw: unknown): CustomEntrySummary | null {
  if (!isRecord(raw) || typeof raw.alias !== "string" || !raw.alias) {
    return null;
  }
  return {
    alias: raw.alias,
    base_url: stringOr(raw.base_url, ""),
    api_style: normalizeApiStyle(raw.api_style),
    auth_header: stringOr(raw.auth_header, "Authorization"),
    auth_prefix: stringOr(raw.auth_prefix, "Bearer "),
    model: stringOr(raw.model, ""),
    configured: raw.configured === true,
    key_preview: stringOrNull(raw.key_preview),
  };
}

export function normalizeProviderKeys(raw: unknown): ProviderKeysMasked {
  const obj = isRecord(raw) ? raw : {};
  const custom = Array.isArray(obj.custom)
    ? obj.custom
        .map((entry) => normalizeCustomEntry(entry))
        .filter((entry): entry is CustomEntrySummary => entry !== null)
    : [];
  return {
    anthropic: normalizeFixedSummary(obj.anthropic),
    openai: normalizeFixedSummary(obj.openai),
    google: normalizeFixedSummary(obj.google),
    custom,
  };
}

// ----------------------------------------------------------------------
// Discovery
// ----------------------------------------------------------------------

export async function listGatewayProviders(): Promise<GatewayProviderList> {
  return getJSON<GatewayProviderList>("/gateway/providers");
}

export async function listGatewayRoutes(
  provider?: string,
): Promise<GatewayRouteList> {
  const q = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  return getJSON<GatewayRouteList>(`/gateway/routes${q}`);
}

export async function listModelAvailability(): Promise<RouteAvailabilityList> {
  return getJSON<RouteAvailabilityList>("/gateway/model-availability");
}

// ----------------------------------------------------------------------
// Provider keys
// ----------------------------------------------------------------------

export async function getProviderKeys(): Promise<ProviderKeysMasked> {
  return getJSON<unknown>("/provider-keys").then(normalizeProviderKeys);
}

export async function putFixedProviderKey(
  provider: FixedProvider,
  apiKey: string,
): Promise<ProviderKeysMasked> {
  const res = await sidecarFetch(`/provider-keys/${provider}`, {
    method: "PUT",
    headers: UI_ORIGIN,
    body: JSON.stringify({ api_key: apiKey }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`PUT /provider-keys/${provider} failed (${res.status}): ${text}`);
  }
  return normalizeProviderKeys(await res.json());
}

export async function deleteFixedProviderKey(
  provider: FixedProvider,
): Promise<ProviderKeysMasked> {
  const res = await sidecarFetch(`/provider-keys/${provider}`, {
    method: "DELETE",
    headers: UI_ORIGIN,
  });
  if (!res.ok) {
    throw new Error(
      `DELETE /provider-keys/${provider} failed (${res.status})`,
    );
  }
  return normalizeProviderKeys(await res.json());
}

export interface CustomEntryPayload {
  alias: string;
  base_url: string;
  api_key: string;
  api_style: ApiStyle;
  auth_header?: string;
  auth_prefix?: string;
  model?: string;
}

export async function putCustomProviderEntry(
  entry: CustomEntryPayload,
): Promise<ProviderKeysMasked> {
  const res = await sidecarFetch("/provider-keys/custom", {
    method: "PUT",
    headers: UI_ORIGIN,
    body: JSON.stringify(entry),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `PUT /provider-keys/custom failed (${res.status}): ${text}`,
    );
  }
  return normalizeProviderKeys(await res.json());
}

export interface TestConnectionResult {
  ok: boolean;
  detail: string;
  latency_ms: number;
  // F120: CLI providers also return a classified auth state + one-step
  // remediation, so a logged-out Test reads "Not logged in — run the login
  // command" instead of a bare `claude_cli_failed: exit 1:`.
  // F132: `rate_limited` is a distinct connected-but-throttled state (amber),
  // not a red failure.
  state?: "connected" | "logged_out" | "rate_limited" | "error";
  remediation?: string;
}

export async function testProvider(
  provider: string,
): Promise<TestConnectionResult> {
  const res = await sidecarFetch(`/provider-keys/${encodeURIComponent(provider)}/test`, {
    method: "POST",
    headers: UI_ORIGIN,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`POST /provider-keys/${provider}/test failed (${res.status}): ${text}`);
  }
  return res.json();
}

export async function testFixedProvider(
  provider: FixedProvider | "local",
): Promise<TestConnectionResult> {
  return testProvider(provider);
}

export async function testCustomAlias(
  alias: string,
): Promise<TestConnectionResult> {
  const res = await sidecarFetch(
    `/provider-keys/custom/test?alias=${encodeURIComponent(alias)}`,
    { method: "POST", headers: UI_ORIGIN },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`POST /provider-keys/custom/test failed (${res.status}): ${text}`);
  }
  return res.json();
}

export async function deleteCustomProviderEntry(
  alias: string,
): Promise<ProviderKeysMasked> {
  const res = await sidecarFetch(
    `/provider-keys/custom?alias=${encodeURIComponent(alias)}`,
    { method: "DELETE", headers: UI_ORIGIN },
  );
  if (!res.ok) {
    throw new Error(
      `DELETE /provider-keys/custom?alias=${alias} failed (${res.status})`,
    );
  }
  return normalizeProviderKeys(await res.json());
}

// ----------------------------------------------------------------------
// F040-01 — subscription CLI provider setup (status / binary override / login)
// ----------------------------------------------------------------------

/** 3-state CLI connection model. `not_installed` | `installed` (auth unknown). */
export type CliState = "not_installed" | "installed";

export interface CliStatus {
  provider: string;
  state: CliState;
  found: boolean;
  path: string;
  nameUsed: string;
  source: string;
  version: string;
  // Cached live-probe result (null until the user explicitly Tests).
  connected: boolean | null;
  login: string;
  verifiedAt: string | null;
}

export interface CliProbeResult {
  state: "connected" | "logged_out" | "error";
  login: string;
  verifiedAt: string | null;
}

export interface CliLoginCommand {
  loginArgv: string[];
  installUrl: string;
  installCommand: string;
}

function asBoolOrNull(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

// The backend may serialize `verified_at` as an ISO string, a number (epoch),
// or null. Surface it as a string for display, or null when absent.
function asStringOrNull(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return null;
}

function normalizeCliStatus(raw: unknown): CliStatus {
  const obj = isRecord(raw) ? raw : {};
  const state: CliState = obj.state === "installed" ? "installed" : "not_installed";
  return {
    provider: stringOr(obj.provider, ""),
    state,
    found: obj.found === true,
    path: stringOr(obj.path, ""),
    nameUsed: stringOr(obj.name_used, ""),
    source: stringOr(obj.source, ""),
    version: stringOr(obj.version, ""),
    connected: asBoolOrNull(obj.connected),
    login: stringOr(obj.login, ""),
    verifiedAt: asStringOrNull(obj.verified_at),
  };
}

export async function getCliStatus(provider: string): Promise<CliStatus> {
  const res = await sidecarFetch(
    `/gateway/providers/${encodeURIComponent(provider)}/cli-status`,
    { headers: UI_ORIGIN },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `GET /gateway/providers/${provider}/cli-status failed (${res.status}): ${text}`,
    );
  }
  return normalizeCliStatus(await res.json());
}

export async function setCliBinary(
  provider: string,
  path: string,
): Promise<CliStatus> {
  const res = await sidecarFetch(
    `/provider-keys/${encodeURIComponent(provider)}/cli-binary`,
    { method: "PUT", headers: UI_ORIGIN, body: JSON.stringify({ path }) },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `PUT /provider-keys/${provider}/cli-binary failed (${res.status}): ${text}`,
    );
  }
  return normalizeCliStatus(await res.json());
}

export async function clearCliBinary(provider: string): Promise<CliStatus> {
  const res = await sidecarFetch(
    `/provider-keys/${encodeURIComponent(provider)}/cli-binary`,
    { method: "DELETE", headers: UI_ORIGIN },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `DELETE /provider-keys/${provider}/cli-binary failed (${res.status}): ${text}`,
    );
  }
  return normalizeCliStatus(await res.json());
}

export async function getCliLoginCommand(
  provider: string,
): Promise<CliLoginCommand> {
  const res = await sidecarFetch(
    `/provider-keys/${encodeURIComponent(provider)}/login-command`,
    { headers: UI_ORIGIN },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `GET /provider-keys/${provider}/login-command failed (${res.status}): ${text}`,
    );
  }
  const raw = await res.json();
  const obj = isRecord(raw) ? raw : {};
  const argv = Array.isArray(obj.login_argv)
    ? obj.login_argv.filter((v): v is string => typeof v === "string")
    : [];
  return {
    loginArgv: argv,
    installUrl: stringOr(obj.install_url, ""),
    installCommand: stringOr(obj.install_command, ""),
  };
}

// ----------------------------------------------------------------------
// F040-01 S5a — native CLI login launcher (Tauri commands)
//
// These wrap the Rust `launch_cli_login` / `cli_login_launch_available`
// commands. They degrade gracefully in plain `vite dev` / vitest where the
// Tauri command bridge is not attached — `cliLoginLaunchAvailable()` returns
// false and `launchCliLogin()` throws, so the UI falls back to the #129
// copy-command path.
// ----------------------------------------------------------------------

export interface CliLoginLaunch {
  launched: boolean;
  /** "terminal" on success; "unavailable" when no terminal resolved. */
  transport: "terminal" | "unavailable" | string;
  detail: string;
}

// Tauri invoke resolves through the shared, bundler-visible `loadTauriInvoke`
// (src/lib/sidecarPort.ts) so the CLI one-click login launcher actually ships
// in the packaged app. `null` === browser-dev (no Tauri shell).

/**
 * Map the gateway `provider_class` (`claude_cli` / `codex_cli` / `cursor_cli`)
 * to the closed Rust provider enum (`claude` / `codex` / `cursor`). The Rust
 * side rejects anything outside that enum, so we normalize here.
 */
function cliProviderForLaunch(providerClass: string): string {
  return providerClass.endsWith("_cli")
    ? providerClass.slice(0, -"_cli".length)
    : providerClass;
}

/** Whether the native one-click login launcher is usable on this platform. */
export async function cliLoginLaunchAvailable(): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  try {
    return await invoke<boolean>("cli_login_launch_available");
  } catch {
    return false;
  }
}

/**
 * Launch the vendor's own login flow in a terminal for an installed CLI.
 * Throws when the Tauri bridge is unavailable or the command errors so the
 * caller can fall back to copy-command.
 */
export async function launchCliLogin(
  providerClass: string,
  binaryPath: string,
): Promise<CliLoginLaunch> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    throw new Error("native launcher unavailable");
  }
  return invoke<CliLoginLaunch>("launch_cli_login", {
    provider: cliProviderForLaunch(providerClass),
    binaryPath,
  });
}
