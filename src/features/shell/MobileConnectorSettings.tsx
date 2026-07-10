import { useEffect, useMemo, useState } from "react";
import {
  deleteDevice,
  getLanAddresses,
  getMobileConnectorSettings,
  putMobileConnectorSettings,
  revokeDevice,
  updateDeviceCapabilities,
  type LanAddressCandidate,
  type MobileCapabilities,
  type MobileConnectorSettings as MobileConnectorSettingsState,
  type MobileDevice,
} from "../../lib/api/mobileConnector";
import PairPhoneModal from "./PairPhoneModal";

const CAPABILITY_LABELS: Record<keyof MobileCapabilities, string> = {
  read_runs: "Read runs",
  start_runs: "Start runs",
  send_messages: "Send messages",
  cancel_runs: "Cancel runs",
  read_coding_projects: "Read Coding Team projects",
  read_coding_activity: "Read Coding Team activity",
  read_coding_diffs: "Read Coding Team diffs",
  send_coding_messages: "Send Coding Team messages",
  start_coding_runs: "Start Coding Team runs",
  resume_coding_runs: "Resume Coding Team runs",
  cancel_coding_runs: "Cancel Coding Team runs",
  edit_coding_plan: "Edit Coding Team plan",
  accept_coding_merge_back: "Accept Coding Team merge back",
  approve_low_risk: "Approve low risk",
  approve_remote_egress: "Approve remote egress",
  approve_mcp_elicitation: "Approve MCP elicitation",
  approve_code_exec: "Approve code exec",
  approve_code_write: "Approve code write",
  approve_merge_back: "Approve merge back",
};

function preferredAddress(addresses: LanAddressCandidate[]): string {
  return (
    addresses.find((item) => item.isDefault)?.address ??
    addresses[0]?.address ??
    ""
  );
}

export function MobileConnectorSettings() {
  const [settings, setSettings] = useState<MobileConnectorSettingsState | null>(null);
  const [addresses, setAddresses] = useState<LanAddressCandidate[]>([]);
  const [address, setAddress] = useState("");
  const [port, setPort] = useState(8788);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pairingOpen, setPairingOpen] = useState(false);

  async function load() {
    setError(null);
    const [nextSettings, nextAddresses] = await Promise.all([
      getMobileConnectorSettings(),
      getLanAddresses(),
    ]);
    setSettings(nextSettings);
    setAddresses(nextAddresses);
    setAddress(nextSettings.lanBindAddress ?? preferredAddress(nextAddresses));
    setPort(nextSettings.port);
  }

  useEffect(() => {
    let cancelled = false;
    Promise.all([getMobileConnectorSettings(), getLanAddresses()])
      .then(([nextSettings, nextAddresses]) => {
        if (cancelled) return;
        setSettings(nextSettings);
        setAddresses(nextAddresses);
        setAddress(nextSettings.lanBindAddress ?? preferredAddress(nextAddresses));
        setPort(nextSettings.port);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const listenerText = useMemo(() => {
    if (!settings?.enabled) return "Off";
    const host = settings.lanListener?.host ?? settings.lanBindAddress ?? address;
    const livePort = settings.lanListener?.port ?? settings.port;
    return `Listening on https://${host}:${livePort}`;
  }, [address, settings]);

  async function saveEnabled(enabled: boolean) {
    if (enabled && !address.trim()) {
      setError("Choose or enter a LAN IPv4 address before enabling.");
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const saved = await putMobileConnectorSettings(
        enabled
          ? {
              enabled: true,
              bindMode: "lan",
              lanBindAddress: address.trim(),
              port,
              requireTls: true,
              pairingEnabled: true,
              allowedNetworks: ["lan"],
            }
          : {
              enabled: false,
              bindMode: "disabled",
              pairingEnabled: false,
            },
      );
      setSettings(saved);
      setMessage(enabled ? "Mobile connector enabled." : "Mobile connector disabled.");
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  // F071 — also bind/advertise the Tailscale IP for off-LAN reach.
  const detectedTailscale =
    addresses.find((item) => item.kind === "tailscale")?.address ??
    settings?.tailscaleBindAddress ??
    "";

  async function toggleTailscale(on: boolean) {
    if (!settings?.enabled) return;
    if (on && !detectedTailscale) {
      setError("No Tailscale address detected on this machine.");
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const saved = await putMobileConnectorSettings({
        enabled: true,
        alsoTailscale: on,
        tailscaleBindAddress: on ? detectedTailscale : null,
        // _host_candidates only advertises tailscale when it's in allowed_networks.
        allowedNetworks: on ? ["lan", "tailscale"] : ["lan"],
      });
      setSettings(saved);
      setMessage(
        on
          ? "Also reachable over Tailscale. Re-pair phones — the certificate changed."
          : "Tailscale access off.",
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function saveNetwork() {
    if (!settings?.enabled) return;
    if (!address.trim()) {
      setError("Choose or enter a LAN IPv4 address before saving.");
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const saved = await putMobileConnectorSettings({
        enabled: true,
        bindMode: "lan",
        lanBindAddress: address.trim(),
        port,
        requireTls: true,
        pairingEnabled: true,
        allowedNetworks: ["lan"],
      });
      setSettings(saved);
      setMessage("Mobile connector settings saved.");
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function toggleCapability(
    device: MobileDevice,
    capability: keyof MobileCapabilities,
    value: boolean,
  ) {
    setBusy(true);
    setError(null);
    try {
      const updated = await updateDeviceCapabilities(device.deviceId, {
        [capability]: value,
      });
      setSettings((current) =>
        current
          ? {
              ...current,
              devices: current.devices.map((item) =>
                item.deviceId === updated.deviceId ? updated : item,
              ),
            }
          : current,
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(device: MobileDevice) {
    setBusy(true);
    setError(null);
    try {
      const updated = await revokeDevice(device.deviceId);
      setSettings((current) =>
        current
          ? {
              ...current,
              devices: current.devices.map((item) =>
                item.deviceId === updated.deviceId ? updated : item,
              ),
            }
          : current,
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(device: MobileDevice) {
    setBusy(true);
    setError(null);
    try {
      await deleteDevice(device.deviceId);
      setSettings((current) =>
        current
          ? {
              ...current,
              devices: current.devices.filter((item) => item.deviceId !== device.deviceId),
              deviceCount: Math.max(0, (current.deviceCount ?? current.devices.length) - 1),
            }
          : current,
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  if (!settings && !error) {
    return <p className="shell-muted">Loading mobile connector...</p>;
  }

  return (
    <div className="mobile-connector-settings">
      <p className="shell-muted">
        Off by default. When enabled, Errorta listens on the selected LAN address
        and pairs phones with a QR code plus a separate PIN.
      </p>

      <div className="mobile-connector-row">
        <label>
          <span>LAN address</span>
          {addresses.length > 0 ? (
            <select
              value={address}
              onChange={(event) => setAddress(event.currentTarget.value)}
              disabled={busy}
            >
              {addresses.map((candidate) => (
                <option key={candidate.address} value={candidate.address}>
                  {candidate.address}
                  {candidate.isDefault ? " (default)" : ""}
                </option>
              ))}
              {address && !addresses.some((item) => item.address === address) ? (
                <option value={address}>{address} (manual)</option>
              ) : null}
            </select>
          ) : null}
        </label>
        <label>
          <span>Manual address</span>
          <input
            value={address}
            onChange={(event) => setAddress(event.currentTarget.value)}
            placeholder="192.0.2.14"
            disabled={busy}
          />
        </label>
        <label>
          <span>Port</span>
          <input
            type="number"
            min={1}
            max={65535}
            value={port}
            onChange={(event) => setPort(Number(event.currentTarget.value))}
            disabled={busy}
          />
        </label>
      </div>

      <div className="mobile-connector-status">
        <span>{listenerText}</span>
        {settings?.pairingPinRequired ? <span>PIN required</span> : <span>Manual approval</span>}
        {settings?.lanListener?.cert_sha256 ? (
          <span>Cert {settings.lanListener.cert_sha256.slice(0, 12)}</span>
        ) : null}
      </div>

      {settings?.enabled ? (
        <div className="mobile-connector-tailscale">
          <label>
            <input
              type="checkbox"
              checked={Boolean(settings.alsoTailscale)}
              disabled={busy || !detectedTailscale}
              onChange={(event) => toggleTailscale(event.currentTarget.checked)}
            />
            <span>
              Also reach me over Tailscale
              {detectedTailscale
                ? ` (${detectedTailscale})`
                : " — no Tailscale IP detected"}
            </span>
          </label>
          {settings.alsoTailscale ? (
            <p className="shell-muted">
              Reachable off-LAN via Tailscale. Toggling this regenerates the
              certificate — paired phones must re-pair.
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="shell-actions">
        {settings?.enabled ? (
          <button type="button" onClick={() => saveEnabled(false)} disabled={busy}>
            Disable
          </button>
        ) : (
          <button type="button" onClick={() => saveEnabled(true)} disabled={busy}>
            Enable
          </button>
        )}
        <button
          type="button"
          onClick={saveNetwork}
          disabled={busy || !settings?.enabled}
        >
          Save network
        </button>
        <button
          type="button"
          onClick={() => setPairingOpen(true)}
          disabled={busy || !settings?.enabled || !settings.pairingEnabled}
        >
          Pair a phone
        </button>
      </div>

      {message ? <p className="shell-muted">{message}</p> : null}
      {error ? <p className="shell-error">{error}</p> : null}

      <div className="mobile-device-list">
        <h3>Paired devices</h3>
        {settings?.devices.length ? (
          settings.devices.map((device) => (
            <div className="mobile-device-row" key={device.deviceId}>
              <div>
                <strong>{device.displayName}</strong>
                <p className="shell-muted">
                  {device.platform} - {device.publicKeyFingerprint}
                  {device.revokedAt ? " - revoked" : ""}
                </p>
              </div>
              <div className="mobile-capability-grid">
                {(Object.keys(CAPABILITY_LABELS) as Array<keyof MobileCapabilities>).map(
                  (capability) => (
                    <label key={capability}>
                      <input
                        type="checkbox"
                        checked={device.capabilities[capability]}
                        disabled={busy || Boolean(device.revokedAt)}
                        onChange={(event) =>
                          toggleCapability(device, capability, event.currentTarget.checked)
                        }
                      />
                      <span>{CAPABILITY_LABELS[capability]}</span>
                    </label>
                  ),
                )}
              </div>
              <div className="mobile-device-actions">
                <button
                  type="button"
                  onClick={() => revoke(device)}
                  disabled={busy || Boolean(device.revokedAt)}
                >
                  Revoke
                </button>
                {device.revokedAt && (
                  <button
                    type="button"
                    className="mobile-device-delete"
                    onClick={() => remove(device)}
                    disabled={busy}
                  >
                    Delete
                  </button>
                )}
              </div>
            </div>
          ))
        ) : (
          <p className="shell-muted">No paired phones yet.</p>
        )}
      </div>

      <PairPhoneModal
        open={pairingOpen}
        onClose={() => setPairingOpen(false)}
        onPaired={load}
      />
    </div>
  );
}

export default MobileConnectorSettings;
