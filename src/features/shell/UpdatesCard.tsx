// F-INFRA-09 Slice 4 — Settings → Updates card.
//
// Surfaces:
//   * Current version (from the existing `about()` Tauri command).
//   * Last-checked timestamp + manual "Check now" button.
//   * Channel selector  (stable / beta / disabled, localStorage-backed).
//   * Behavior selector (notify-only / auto-install / off, localStorage-backed).
//   * Rollback list (empty in v0.5 default builds; populated by slice 5).
//   * Crash-recovery banner (slice 6) when the previous boot auto-rolled back.
//
// Renders gracefully in three states:
//   - Tauri command bridge absent (vite dev / vitest): everything degrades to
//     the not_configured stub; UI shows the v0.6 disabled hint.
//   - Default build (Tauri command bridge present, no updater-enabled feature):
//     `check_for_updates` returns `not_configured`; same v0.6 disabled hint.
//   - Feature-enabled v0.6 build: real check + install + rollback paths run.
//
// No fetch() ever leaves this component — every dependency goes through
// Tauri commands. This matches the no-telemetry guarantee in the F-INFRA-09
// spec.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  checkForUpdates,
  dismissCrashRecovery,
  getAboutVersion,
  getCrashRecovery,
  installUpdate,
  listRollbacks,
  rollbackTo,
  type CrashRecoveryEntry,
  type RollbackEntry,
  type UpdateCheckResult,
} from "../../lib/api/updater";
import "./shell.css";

type Channel = "stable" | "beta" | "disabled";
type Behavior = "notify-only" | "auto-install" | "off";

const STORAGE_CHANNEL = "errorta.updates.channel";
const STORAGE_BEHAVIOR = "errorta.updates.behavior";

function readChannel(): Channel {
  try {
    const raw = window.localStorage.getItem(STORAGE_CHANNEL);
    if (raw === "stable" || raw === "beta" || raw === "disabled") return raw;
  } catch {
    // ignore
  }
  return "stable";
}

function readBehavior(): Behavior {
  try {
    const raw = window.localStorage.getItem(STORAGE_BEHAVIOR);
    if (raw === "notify-only" || raw === "auto-install" || raw === "off") return raw;
  } catch {
    // ignore
  }
  return "notify-only";
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function relativeTime(when: Date | null): string {
  if (!when) return "never";
  const seconds = Math.max(0, Math.floor((Date.now() - when.getTime()) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function UpdatesCard() {
  const [version, setVersion] = useState<string | null>(null);
  const [check, setCheck] = useState<UpdateCheckResult | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [channel, setChannel] = useState<Channel>(() => readChannel());
  const [behavior, setBehavior] = useState<Behavior>(() => readBehavior());
  const [rollbacks, setRollbacks] = useState<RollbackEntry[]>([]);
  const [crashRecovery, setCrashRecovery] = useState<CrashRecoveryEntry | null>(null);
  const [installing, setInstalling] = useState(false);
  const [installResultMsg, setInstallResultMsg] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const [c, v, rl, cr] = await Promise.all([
      checkForUpdates(),
      getAboutVersion(),
      listRollbacks(),
      getCrashRecovery(),
    ]);
    setCheck(c);
    setLastChecked(new Date());
    setVersion(v);
    setRollbacks(rl);
    setCrashRecovery(cr);
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const onCheckNow = useCallback(() => {
    void reload();
  }, [reload]);

  const onInstall = useCallback(async () => {
    setInstalling(true);
    setInstallResultMsg(null);
    const r = await installUpdate();
    setInstalling(false);
    if (r.status === "installed") {
      setInstallResultMsg(`Installed v${r.version ?? "?"}. Restart Errorta to apply.`);
    } else if (r.status === "error") {
      setInstallResultMsg(`Install failed: ${r.error}`);
    } else if (r.status === "not_configured") {
      setInstallResultMsg("Auto-install is not configured in this build.");
    } else {
      setInstallResultMsg(null);
    }
    await reload();
  }, [reload]);

  const onChannelChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const next = e.target.value as Channel;
      setChannel(next);
      try {
        window.localStorage.setItem(STORAGE_CHANNEL, next);
      } catch {
        // ignore
      }
    },
    [],
  );

  const onBehaviorChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const next = e.target.value as Behavior;
      setBehavior(next);
      try {
        window.localStorage.setItem(STORAGE_BEHAVIOR, next);
      } catch {
        // ignore
      }
    },
    [],
  );

  const onRollback = useCallback(
    async (v: string) => {
      const ok =
        typeof window !== "undefined" && typeof window.confirm === "function"
          ? window.confirm(
              `Roll back to v${v}? Errorta will restart on the previous version.`,
            )
          : true;
      if (!ok) return;
      const r = await rollbackTo(v);
      if (r.status === "error") {
        setInstallResultMsg(`Rollback failed: ${r.error}`);
      } else {
        setInstallResultMsg(`Rollback to v${v} queued; Errorta will restart.`);
      }
      await reload();
    },
    [reload],
  );

  const onDismissCrash = useCallback(async () => {
    await dismissCrashRecovery();
    setCrashRecovery(null);
  }, []);

  // Auto-install / notify timer (slice 6).
  useEffect(() => {
    if (behavior === "off" || channel === "disabled") return;
    let cancelled = false;
    const tick = async () => {
      const result = await checkForUpdates();
      if (cancelled) return;
      setCheck(result);
      setLastChecked(new Date());
      if (result.status === "available" && behavior === "auto-install") {
        const r = await installUpdate();
        if (cancelled) return;
        if (r.status === "installed") {
          setInstallResultMsg(
            `Installed v${r.version ?? "?"}. Restart Errorta to apply.`,
          );
        }
      }
    };
    const id = window.setInterval(() => {
      void tick();
    }, 6 * 60 * 60 * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [behavior, channel]);

  const notConfigured = check?.status === "not_configured";

  const status = useMemo(() => {
    if (!check) return "Checking…";
    switch (check.status) {
      case "up_to_date":
        return version ? `Up to date — Errorta v${version}` : "Up to date";
      case "available":
        return `Update available: v${check.version}`;
      case "not_configured":
        return "Auto-update activates in v0.6";
      case "error":
        return `Check failed: ${check.error}`;
    }
  }, [check, version]);

  return (
    <div className="updates-card">
      {crashRecovery && (
        <div
          className="updates-card__crash-banner"
          role="alert"
          aria-live="polite"
        >
          <div>
            <strong>
              v{crashRecovery.failed_version} crashed on launch.
            </strong>{" "}
            Rolled back to v{crashRecovery.rolled_back_to}.
            <div className="shell-muted">{crashRecovery.error}</div>
          </div>
          <button type="button" onClick={onDismissCrash}>
            Dismiss
          </button>
        </div>
      )}

      {notConfigured && (
        <p className="shell-muted">
          Auto-update activates in v0.6. Today, install updates manually from{" "}
          <a
            href="https://wiggins-j.github.io/errorta-downloads/"
            target="_blank"
            rel="noreferrer"
          >
            the downloads page
          </a>
          .
        </p>
      )}

      <div className="updates-card__row">
        <strong>Status:</strong> {status}
      </div>
      {check?.status === "available" && check.notes && (
        <p className="updates-card__notes">{check.notes}</p>
      )}
      <div className="updates-card__row shell-muted">
        Last checked: {relativeTime(lastChecked)}
      </div>

      <div className="updates-card__actions">
        <button type="button" onClick={onCheckNow}>
          Check now
        </button>
        {check?.status === "available" && (
          <button type="button" onClick={onInstall} disabled={installing}>
            {installing ? "Installing…" : "Install update"}
          </button>
        )}
      </div>
      {installResultMsg && <p className="shell-muted">{installResultMsg}</p>}

      <div className="updates-card__row updates-card__channel">
        <label>
          Channel
          <select value={channel} onChange={onChannelChange}>
            <option value="stable">Stable</option>
            <option value="beta">Beta</option>
            <option value="disabled">Disabled</option>
          </select>
        </label>
        {channel === "beta" && (
          <span className="shell-muted">
            Beta channel: switch takes effect on next release — manual download
            required.
          </span>
        )}
      </div>

      <div className="updates-card__row updates-card__behavior">
        <label>
          Behavior
          <select value={behavior} onChange={onBehaviorChange}>
            <option value="notify-only">Notify only</option>
            <option value="auto-install">Auto-install</option>
            <option value="off">Off</option>
          </select>
        </label>
      </div>

      <div className="updates-card__rollback-list">
        <h3>Previous versions</h3>
        {rollbacks.length === 0 ? (
          <p className="shell-muted">
            No previous versions installed yet — rollback becomes available
            after your first update.
          </p>
        ) : (
          <ul>
            {rollbacks.map((r) => (
              <li key={r.version} className="updates-card__rollback-row">
                <div>
                  <strong>v{r.version}</strong>{" "}
                  <span className="shell-muted">
                    installed {r.installed_at} · {formatBytes(r.size_bytes)}
                    {r.crashed_on_launch ? " · crashed on launch" : ""}
                  </span>
                </div>
                <button type="button" onClick={() => void onRollback(r.version)}>
                  Roll back
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export default UpdatesCard;
