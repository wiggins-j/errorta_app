// INTEGRATION — onboarding step 2: data residency (F-INFRA-12 Phase B Slice 10).
//
// Lets a first-run user pick Local / SSH-remote / Cloud and persist the
// choice before continuing. Mirrors the three radio options of
// DataResidencyCard but with onboarding-grade simplifications:
//   - Local: one click and the step is done.
//   - SSH-remote: collect host/port/key/remote_sidecar_port, run
//     test_ssh_connection through the Tauri shim; Apply may install the
//     remote sidecar if the probe reaches SSH but finds no sidecar yet.
//   - Cloud: not exposed in first-run yet; Settings retains that surface.
//
// On Apply we call BOTH invoke('set_data_residency', {...}) AND
// PUT /residency so the Rust shell and the Python sidecar share the same
// view of the active mode within one tick (same ordering rule as
// DataResidencyCard.tsx).
//
// The Skip path is documented to deliberately persist mode=local on disk
// regardless of what the user partially filled in. The theory:
//   "an inert Settings card beats a half-configured residency."
// A user who skips can finish setup later in Settings → Data residency,
// and an inert local-mode install is always safe to fall back to.
//
// Bookkeeping:
//   - Writes the localStorage sentinel `errorta.onboarding.residency.seen`
//     on either Apply or Skip (same precedent as StepBriefs).
//   - The OnboardingFlow's done-pill reads that sentinel directly; no new
//     server flag is threaded through useOnboardingState.

import { useState } from "react";
import {
  putResidency,
  type ResidencyConfig,
  type ResidencyMode,
} from "../../lib/api/residency";
import { loadTauriInvoke } from "../../lib/sidecarPort";

interface Props {
  onAdvance: () => void;
  onSkip: () => void;
}

const RESIDENCY_SEEN_KEY = "errorta.onboarding.residency.seen";
const DEFAULT_SSH_PORT = 22;
const DEFAULT_REMOTE_SIDECAR_PORT = 8770;

// Tauri invoke is resolved through the shared, bundler-visible
// `loadTauriInvoke` (src/lib/sidecarPort.ts) so it actually ships in the
// packaged app. `null` return === browser-dev (no Tauri shell).

interface SshProbeReport {
  uname: string;
  sidecar_present: boolean;
  sidecar_version: string | null;
  raw_stdout?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function StepResidency({ onAdvance, onSkip }: Props) {
  const [mode, setMode] = useState<ResidencyMode>("local");

  // SSH form state.
  const [sshHost, setSshHost] = useState("");
  const [sshPort, setSshPort] = useState(String(DEFAULT_SSH_PORT));
  const [sshKeyPath, setSshKeyPath] = useState("");
  const [remoteSidecarPort, setRemoteSidecarPort] = useState(
    String(DEFAULT_REMOTE_SIDECAR_PORT),
  );

  const [probing, setProbing] = useState(false);
  const [probeOk, setProbeOk] = useState<boolean>(false);
  const [probeMessage, setProbeMessage] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [showSkipWarning, setShowSkipWarning] = useState(false);

  const markSeen = () => {
    try {
      localStorage.setItem(RESIDENCY_SEEN_KEY, "1");
    } catch {
      // localStorage may be unavailable (e.g. private mode); ignore.
    }
  };

  const resetProbe = () => {
    setProbeOk(false);
    setProbeMessage(null);
  };

  // -------------------------------------------------------------------------
  // SSH probe
  // -------------------------------------------------------------------------

  const onTestSsh = async () => {
    setProbing(true);
    resetProbe();
    try {
      const invoke = await loadTauriInvoke();
      if (!invoke) {
        setProbeMessage(
          "SSH test requires the Tauri shell (this is browser-dev).",
        );
        return;
      }
      const port = Number.parseInt(sshPort, 10) || DEFAULT_SSH_PORT;
      const report = await invoke<SshProbeReport>("test_ssh_connection", {
        host: sshHost,
        port,
        keyPath: sshKeyPath || null,
        username: null,
      });
      setProbeOk(true);
      if (!report.sidecar_present) {
        setProbeMessage(
          `SSH OK — ${report.uname}. Apply will install and start the Errorta sidecar.`,
        );
      } else {
        setProbeMessage(
          `SSH OK — sidecar ${report.sidecar_version ?? "(version unknown)"} on ${report.uname}.`,
        );
      }
    } catch (e) {
      setProbeMessage(e instanceof Error ? e.message : String(e));
      setProbeOk(false);
    } finally {
      setProbing(false);
    }
  };

  // -------------------------------------------------------------------------
  // Apply
  // -------------------------------------------------------------------------

  const buildPayload = (): ResidencyConfig => {
    if (mode === "ssh-remote") {
      const port = Number.parseInt(sshPort, 10);
      const remotePort = Number.parseInt(remoteSidecarPort, 10);
      return {
        mode: "ssh-remote",
        ssh_host: sshHost || null,
        ssh_port: Number.isFinite(port) ? port : DEFAULT_SSH_PORT,
        ssh_key_path: sshKeyPath || null,
        ssh_username: null,
        remote_sidecar_port: Number.isFinite(remotePort)
          ? remotePort
          : DEFAULT_REMOTE_SIDECAR_PORT,
      };
    }
    return { mode: "local" };
  };

  const onApply = async () => {
    setApplying(true);
    setApplyError(null);
    try {
      const payload = buildPayload();
      const invoke = await loadTauriInvoke();

      // Sync ordering mirrors DataResidencyCard.onApply: SSH-remote → Tauri
      // first (tunnel bring-up), then PUT to refresh the sidecar's view;
      // local + cloud → PUT first (server-side validation is authoritative
      // for cloud), then Tauri.
      if (payload.mode === "ssh-remote") {
        if (invoke) {
          const report = await invoke<{
            local_tunnel_port: number | null;
          }>("set_data_residency", { newState: payload });
          await putResidency({
            ...payload,
            local_tunnel_port: report.local_tunnel_port,
          });
        } else {
          await putResidency(payload);
        }
      } else {
        await putResidency(payload);
        if (invoke) {
          await invoke("set_data_residency", { newState: payload });
        }
      }

      markSeen();
      onAdvance();
    } catch (e) {
      setApplyError(e instanceof Error ? e.message : String(e));
    } finally {
      setApplying(false);
    }
  };

  // -------------------------------------------------------------------------
  // Skip
  // -------------------------------------------------------------------------

  // Skip-the-step path: we deliberately persist mode=local regardless of
  // what the user partially filled in. The theory: an inert Settings card
  // beats a half-configured residency. The user can finish setup later in
  // Settings → Data residency.
  const onSkipStep = async () => {
    setShowSkipWarning(true);
    try {
      await putResidency({ mode: "local" });
      const invoke = await loadTauriInvoke();
      if (invoke) {
        try {
          await invoke("set_data_residency", { newState: { mode: "local" } });
        } catch {
          // The Rust shell may already be in local mode; treat as no-op.
        }
      }
    } catch {
      // Sidecar may be unreachable in browser-dev; we still proceed since
      // local mode is the safe default that the on-disk file will reflect
      // the next time the sidecar boots.
    }
    markSeen();
    onAdvance();
  };

  const onSkipOnboarding = () => {
    markSeen();
    onSkip();
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const canApply = (() => {
    if (mode === "local") return true;
    if (mode === "ssh-remote") return probeOk && sshHost.trim().length > 0;
    return false;
  })();

  return (
    <div className="onboarding-step" data-testid="step-residency">
      <h2>Choose where Errorta stores data</h2>
      <p>
        You can keep everything on this Mac, or run the Errorta sidecar on a
        server you own. You can change this any time from Settings.
      </p>

      <fieldset className="onboarding-residency-modes">
        <legend className="visually-hidden">Data residency mode</legend>
        <label className="onboarding-residency-option">
          <input
            type="radio"
            name="onboarding-residency-mode"
            value="local"
            checked={mode === "local"}
            onChange={() => {
              setMode("local");
              resetProbe();
            }}
            data-testid="onboarding-residency-local"
          />
          <span>
            <strong>Local</strong> — Keep everything on this Mac.
          </span>
        </label>
        <label className="onboarding-residency-option">
          <input
            type="radio"
            name="onboarding-residency-mode"
            value="ssh-remote"
            checked={mode === "ssh-remote"}
            onChange={() => {
              setMode("ssh-remote");
              resetProbe();
            }}
            data-testid="onboarding-residency-ssh"
          />
          <span>
            <strong>SSH-remote</strong> — Use a server I own (Linux, Mac, etc.).
          </span>
        </label>
        <label className="onboarding-residency-option">
          <input
            type="radio"
            name="onboarding-residency-mode"
            value="cloud"
            checked={mode === "cloud"}
            disabled
            onChange={() => {}}
            data-testid="onboarding-residency-cloud"
          />
          <span>
            <strong>Cloud</strong> — Hosted sidecar support is not enabled in
            first-run setup yet.
          </span>
        </label>
      </fieldset>

      {mode === "ssh-remote" && (
        <div className="onboarding-residency-fields" data-testid="onboarding-ssh-fields">
          <label>
            SSH host
            <input
              type="text"
              spellCheck={false}
              value={sshHost}
              onChange={(e) => {
                setSshHost(e.target.value);
                resetProbe();
              }}
              placeholder="hostname"
              data-testid="onboarding-ssh-host"
            />
          </label>
          <label>
            Port <span className="onboarding-detail">(optional, default 22)</span>
            <input
              type="number"
              min={1}
              max={65535}
              value={sshPort}
              onChange={(e) => {
                setSshPort(e.target.value);
                resetProbe();
              }}
              data-testid="onboarding-ssh-port"
            />
          </label>
          <label>
            SSH key path <span className="onboarding-detail">(optional)</span>
            <input
              type="text"
              spellCheck={false}
              value={sshKeyPath}
              onChange={(e) => {
                setSshKeyPath(e.target.value);
                resetProbe();
              }}
              placeholder="~/.ssh/id_ed25519"
              data-testid="onboarding-ssh-key-path"
            />
          </label>
          <label>
            Remote sidecar port
            <input
              type="number"
              min={1}
              max={65535}
              value={remoteSidecarPort}
              onChange={(e) => {
                setRemoteSidecarPort(e.target.value);
                resetProbe();
              }}
              data-testid="onboarding-ssh-remote-port"
            />
          </label>
          <button
            type="button"
            className="onboarding-cta-secondary"
            onClick={onTestSsh}
            disabled={probing || !sshHost.trim()}
            data-testid="onboarding-ssh-test"
          >
            {probing ? "Testing…" : "Test connection"}
          </button>
        </div>
      )}

      {probeMessage ? (
        <p
          className={probeOk ? "onboarding-detail" : "onboarding-error"}
          role={probeOk ? "status" : "alert"}
          data-testid="onboarding-residency-probe-msg"
        >
          {probeMessage}
        </p>
      ) : null}

      {applyError ? (
        <p
          className="onboarding-error"
          role="alert"
          data-testid="onboarding-residency-apply-error"
        >
          {applyError}
        </p>
      ) : null}

      {showSkipWarning ? (
        <p
          className="onboarding-detail"
          role="status"
          data-testid="onboarding-residency-skip-warning"
        >
          Skipped — Errorta is running in Local mode. You can finish remote
          setup later in Settings → Data residency.
        </p>
      ) : null}

      <div className="onboarding-actions">
        <button
          type="button"
          className="onboarding-cta-primary"
          onClick={onApply}
          disabled={applying || !canApply}
          data-testid="onboarding-residency-apply"
        >
          {applying ? "Applying…" : "Apply and continue"}
        </button>
        {mode !== "local" ? (
          <button
            type="button"
            className="onboarding-cta-secondary"
            onClick={onSkipStep}
            data-testid="onboarding-residency-skip-step"
          >
            Skip
          </button>
        ) : null}
        <button
          type="button"
          className="onboarding-cta-link"
          onClick={onSkipOnboarding}
          data-testid="onboarding-residency-skip-onboarding"
        >
          Skip setup
        </button>
      </div>
    </div>
  );
}
