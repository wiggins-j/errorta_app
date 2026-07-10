import { sidecarFetch } from "../api";

const UI_HEADERS = { "x-errorta-origin": "tauri-ui" };

export type MobileBindMode =
  | "disabled"
  | "loopback_dev"
  | "lan"
  | "tailscale"
  | "explicit_host";

export type MobilePairingState =
  | "awaiting_device"
  | "awaiting_approval"
  | "approved"
  | "consumed"
  | "denied"
  | "expired"
  | "cancelled";

export interface MobileCapabilities {
  read_runs: boolean;
  start_runs: boolean;
  send_messages: boolean;
  cancel_runs: boolean;
  read_coding_projects: boolean;
  read_coding_activity: boolean;
  read_coding_diffs: boolean;
  send_coding_messages: boolean;
  start_coding_runs: boolean;
  resume_coding_runs: boolean;
  cancel_coding_runs: boolean;
  edit_coding_plan: boolean;
  accept_coding_merge_back: boolean;
  approve_low_risk: boolean;
  approve_remote_egress: boolean;
  approve_mcp_elicitation: boolean;
  approve_code_exec: boolean;
  approve_code_write: boolean;
  approve_merge_back: boolean;
}

export interface MobileDevice {
  deviceId: string;
  displayName: string;
  platform: string;
  publicKeyFingerprint: string;
  pairedAt: string | null;
  lastSeenAt: string | null;
  lastIpLabel: string | null;
  capabilities: MobileCapabilities;
  revokedAt: string | null;
}

export interface LanListenerStatus {
  running?: boolean;
  host?: string;
  port?: number;
  cert_sha256?: string | null;
  error?: string | null;
}

export interface MobileConnectorSettings {
  enabled: boolean;
  bindMode: MobileBindMode;
  explicitHost: string | null;
  lanBindAddress: string | null;
  port: number;
  requireTls: boolean;
  pairingEnabled: boolean;
  pairingPinRequired: boolean;
  allowedNetworks: string[];
  maxEventStreams: number;
  deviceCount: number;
  devices: MobileDevice[];
  lanListener?: LanListenerStatus | null;
  alsoTailscale: boolean;
  tailscaleBindAddress: string | null;
}

export interface MobileConnectorUpdate {
  enabled?: boolean;
  bindMode?: MobileBindMode;
  explicitHost?: string | null;
  lanBindAddress?: string | null;
  port?: number;
  requireTls?: boolean;
  pairingEnabled?: boolean;
  allowedNetworks?: string[];
  maxEventStreams?: number;
  alsoTailscale?: boolean;
  tailscaleBindAddress?: string | null;
}

export interface LanAddressCandidate {
  address: string;
  interface: string;
  kind: string;
  isDefault: boolean;
}

export interface PairingPayload {
  schema: "errorta.mobile_pairing.v1";
  connector_id: string;
  desktop_name: string;
  hosts: Array<{ kind: string; host: string }>;
  port: number;
  tls_cert_sha256: string | null;
  pairing_token: string;
  expires_at: string;
}

export interface PairingStart {
  sessionId: string;
  expiresAt: string;
  pin?: string;
  pairingPayload: PairingPayload;
}

export interface PairingDeviceDraft {
  display_name: string;
  platform: string;
  public_key_fingerprint: string;
  submitted_at: string;
}

export interface PairingStatus {
  sessionId: string;
  state: MobilePairingState;
  deviceDraft: PairingDeviceDraft | null;
  requiresPin: boolean;
  pinAttemptsRemaining: number;
  expiresAt: string;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const r = await sidecarFetch(path, init);
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    throw new Error(`HTTP ${r.status} on ${path}: ${body.slice(0, 200)}`);
  }
  if (r.status === 204) return undefined as T;
  const text = await r.text();
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: UI_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

function mapDevice(wire: Record<string, unknown>): MobileDevice {
  return {
    deviceId: String(wire.device_id ?? ""),
    displayName: String(wire.display_name ?? "iPhone"),
    platform: String(wire.platform ?? "ios"),
    publicKeyFingerprint: String(wire.public_key_fingerprint ?? ""),
    pairedAt: (wire.paired_at as string | null | undefined) ?? null,
    lastSeenAt: (wire.last_seen_at as string | null | undefined) ?? null,
    lastIpLabel: (wire.last_ip_label as string | null | undefined) ?? null,
    capabilities: wire.capabilities as MobileCapabilities,
    revokedAt: (wire.revoked_at as string | null | undefined) ?? null,
  };
}

function mapSettings(wire: Record<string, unknown>): MobileConnectorSettings {
  const devices = Array.isArray(wire.devices)
    ? wire.devices.map((item) => mapDevice(item as Record<string, unknown>))
    : [];
  return {
    enabled: Boolean(wire.enabled),
    bindMode: (wire.bind_mode as MobileBindMode) ?? "disabled",
    explicitHost: (wire.explicit_host as string | null | undefined) ?? null,
    lanBindAddress: (wire.lan_bind_address as string | null | undefined) ?? null,
    port: Number(wire.port ?? 8788),
    requireTls: Boolean(wire.require_tls),
    pairingEnabled: Boolean(wire.pairing_enabled),
    pairingPinRequired: Boolean(wire.pairing_pin_required),
    allowedNetworks: Array.isArray(wire.allowed_networks)
      ? (wire.allowed_networks as string[])
      : [],
    maxEventStreams: Number(wire.max_event_streams ?? 4),
    deviceCount: Number(wire.device_count ?? devices.length),
    devices,
    lanListener: (wire.lan_listener as LanListenerStatus | null | undefined) ?? null,
    alsoTailscale: Boolean(wire.also_tailscale),
    tailscaleBindAddress:
      (wire.tailscale_bind_address as string | null | undefined) ?? null,
  };
}

function updateToWire(update: MobileConnectorUpdate): Record<string, unknown> {
  return {
    ...(update.enabled !== undefined ? { enabled: update.enabled } : {}),
    ...(update.bindMode !== undefined ? { bind_mode: update.bindMode } : {}),
    ...(update.explicitHost !== undefined ? { explicit_host: update.explicitHost } : {}),
    ...(update.lanBindAddress !== undefined
      ? { lan_bind_address: update.lanBindAddress }
      : {}),
    ...(update.port !== undefined ? { port: update.port } : {}),
    ...(update.requireTls !== undefined ? { require_tls: update.requireTls } : {}),
    ...(update.pairingEnabled !== undefined
      ? { pairing_enabled: update.pairingEnabled }
      : {}),
    ...(update.allowedNetworks !== undefined
      ? { allowed_networks: update.allowedNetworks }
      : {}),
    ...(update.maxEventStreams !== undefined
      ? { max_event_streams: update.maxEventStreams }
      : {}),
    ...(update.alsoTailscale !== undefined
      ? { also_tailscale: update.alsoTailscale }
      : {}),
    ...(update.tailscaleBindAddress !== undefined
      ? { tailscale_bind_address: update.tailscaleBindAddress }
      : {}),
  };
}

export async function getMobileConnectorSettings(): Promise<MobileConnectorSettings> {
  const wire = await request<Record<string, unknown>>(
    "/settings/mobile-connector",
    jsonInit("GET"),
  );
  return mapSettings(wire);
}

export async function putMobileConnectorSettings(
  update: MobileConnectorUpdate,
): Promise<MobileConnectorSettings> {
  const wire = await request<Record<string, unknown>>(
    "/settings/mobile-connector",
    jsonInit("PUT", updateToWire(update)),
  );
  return mapSettings(wire);
}

export async function getLanAddresses(): Promise<LanAddressCandidate[]> {
  const wire = await request<{ addresses?: Array<Record<string, unknown>> }>(
    "/settings/mobile-connector/lan-addresses",
    jsonInit("GET"),
  );
  return (wire.addresses ?? []).map((item) => ({
    address: String(item.address ?? ""),
    interface: String(item.interface ?? ""),
    kind: String(item.kind ?? "lan"),
    isDefault: Boolean(item.is_default),
  }));
}

export async function startPairing(): Promise<PairingStart> {
  const wire = await request<Record<string, unknown>>(
    "/settings/mobile-connector/pairing/start",
    jsonInit("POST", { desktop_name: "Errorta Desktop", ttl_seconds: 300 }),
  );
  return {
    sessionId: String(wire.session_id ?? ""),
    expiresAt: String(wire.expires_at ?? ""),
    pin: (wire.pin as string | undefined) ?? undefined,
    pairingPayload: wire.pairing_payload as PairingPayload,
  };
}

export async function getPairingStatus(sessionId: string): Promise<PairingStatus> {
  const wire = await request<{ pairing: Record<string, unknown> }>(
    `/settings/mobile-connector/pairing/${encodeURIComponent(sessionId)}`,
    jsonInit("GET"),
  );
  const pairing = wire.pairing;
  return {
    sessionId: String(pairing.session_id ?? ""),
    state: (pairing.state as MobilePairingState) ?? "awaiting_device",
    deviceDraft:
      (pairing.device_draft as PairingDeviceDraft | null | undefined) ?? null,
    requiresPin: Boolean(pairing.requires_pin),
    pinAttemptsRemaining: Number(pairing.pin_attempts_remaining ?? 0),
    expiresAt: String(pairing.expires_at ?? ""),
  };
}

export async function updateDeviceCapabilities(
  deviceId: string,
  capabilities: Partial<MobileCapabilities>,
): Promise<MobileDevice> {
  const wire = await request<{ device: Record<string, unknown> }>(
    `/settings/mobile-connector/devices/${encodeURIComponent(deviceId)}`,
    jsonInit("PATCH", { capabilities }),
  );
  return mapDevice(wire.device);
}

export async function revokeDevice(deviceId: string): Promise<MobileDevice> {
  const wire = await request<{ device: Record<string, unknown> }>(
    `/settings/mobile-connector/devices/${encodeURIComponent(deviceId)}/revoke`,
    jsonInit("POST"),
  );
  return mapDevice(wire.device);
}

/** Forget a device entirely (removes the record, not just a revoked tombstone). */
export async function deleteDevice(deviceId: string): Promise<void> {
  await request<{ device_id: string; deleted: string }>(
    `/settings/mobile-connector/devices/${encodeURIComponent(deviceId)}`,
    jsonInit("DELETE"),
  );
}
