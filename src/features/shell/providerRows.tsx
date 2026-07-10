// F040-02 — shared provider-key/CLI rows.
//
// These row components + the connection-test hook were originally internal to
// `ProviderKeysSettings.tsx`. They are extracted here verbatim so BOTH the
// Settings panel and the first-run "Connect your AI" onboarding step
// (`StepConnectAI`) render the SAME controls with no logic fork. The extraction
// is behavior-preserving — DOM, test-ids, and copy are unchanged.
import type { JSX } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiStyle,
  CliLoginCommand,
  CliStatus,
  CustomEntryPayload,
  CustomEntrySummary,
  FixedKeySummary,
  FixedProvider,
  ProviderKeysMasked,
  ProviderListItem,
  TestConnectionResult,
  cliLoginLaunchAvailable,
  clearCliBinary,
  deleteCustomProviderEntry,
  deleteFixedProviderKey,
  getCliLoginCommand,
  getCliStatus,
  launchCliLogin,
  putCustomProviderEntry,
  putFixedProviderKey,
  setCliBinary,
  testCustomAlias,
  testFixedProvider,
  testProvider,
} from "../../lib/api/providerKeys";
import { pickPaths } from "./FilePickerDialog";

export type TestStatus =
  | { state: "idle" }
  | { state: "testing" }
  | { state: "ok"; detail: string; latencyMs: number }
  // F120: a logged-out CLI is a distinct, actionable state (not a generic fail);
  // carries the one-step remediation so the panel never shows a bare exit string.
  | { state: "logged_out"; detail: string; remediation: string; latencyMs: number }
  // F132: a throttled CLI is connected-but-busy (amber), not a red failure.
  | { state: "rate_limited"; detail: string; remediation: string; latencyMs: number }
  | { state: "fail"; detail: string; remediation?: string; latencyMs: number };

export function StatusBadge({ status }: { status: TestStatus }) {
  if (status.state === "idle") return null;
  if (status.state === "testing")
    return <span className="provider-test-badge testing">Checking…</span>;
  if (status.state === "ok")
    return (
      <span className="provider-test-badge ok" title={status.detail}>
        ✓ Connected ({status.latencyMs}ms)
      </span>
    );
  if (status.state === "logged_out")
    return (
      <span className="provider-test-badge logged-out" title={status.detail}>
        Not logged in. {status.remediation || "Run the login command, then try again."}
      </span>
    );
  if (status.state === "rate_limited")
    return (
      <span className="provider-test-badge rate-limited" title={status.detail}>
        Connected — rate-limited. Try again later.
      </span>
    );
  return (
    <span className="provider-test-badge fail" title={status.detail}>
      Failed: {status.detail}
      {status.remediation ? ` — ${status.remediation}` : ""}
    </span>
  );
}

export function useTestConnection(
  runTest: () => Promise<TestConnectionResult>,
): [TestStatus, () => void] {
  const [status, setStatus] = useState<TestStatus>({ state: "idle" });
  const trigger = useCallback(() => {
    setStatus({ state: "testing" });
    runTest()
      .then((r) => {
        if (r.ok) {
          setStatus({ state: "ok", detail: r.detail, latencyMs: r.latency_ms });
        } else if (r.state === "logged_out") {
          setStatus({
            state: "logged_out",
            detail: r.detail,
            remediation: r.remediation || "Run the login command, then try again.",
            latencyMs: r.latency_ms,
          });
        } else if (r.state === "rate_limited") {
          setStatus({
            state: "rate_limited",
            detail: r.detail,
            remediation: r.remediation || "Wait and retry, or use a different model.",
            latencyMs: r.latency_ms,
          });
        } else {
          setStatus({
            state: "fail",
            detail: r.detail,
            remediation: r.remediation,
            latencyMs: r.latency_ms,
          });
        }
      })
      .catch((err) =>
        setStatus({
          state: "fail",
          detail: err instanceof Error ? err.message : String(err),
          latencyMs: 0,
        }),
      );
  }, [runTest]);
  return [status, trigger];
}

interface FixedRowProps {
  provider: FixedProvider;
  label: string;
  summary: FixedKeySummary;
  onChange: (next: ProviderKeysMasked) => void;
}

export function FixedProviderRow({
  provider,
  label,
  summary,
  onChange,
}: FixedRowProps) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = useCallback(async () => {
    if (!value) {
      setError("Key is required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const next = await putFixedProviderKey(provider, value);
      onChange(next);
      setEditing(false);
      setValue("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [provider, value, onChange]);

  const handleClear = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const next = await deleteFixedProviderKey(provider);
      onChange(next);
      setEditing(false);
      setValue("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [provider, onChange]);

  return (
    <div
      className="provider-key-row"
      data-provider={provider}
      data-testid={`provider-row-${provider}`}
    >
      <div className="provider-key-head">
        <strong>{label}</strong>
        {summary.configured ? (
          <span className="provider-key-status configured">
            configured: <code>{summary.key_preview ?? "…"}</code>
          </span>
        ) : (
          <span className="provider-key-status missing">no key</span>
        )}
      </div>
      {editing ? (
        <div className="provider-key-editor">
          <label htmlFor={`pk-${provider}`} className="sr-only">
            {label} API key
          </label>
          <input
            id={`pk-${provider}`}
            type="password"
            placeholder={`${label} API key`}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoComplete="off"
            disabled={busy}
          />
          <button onClick={handleSave} disabled={busy} type="button">
            Save
          </button>
          <button
            onClick={() => {
              setEditing(false);
              setValue("");
              setError(null);
            }}
            disabled={busy}
            type="button"
          >
            Cancel
          </button>
        </div>
      ) : (
        <div className="provider-key-actions">
          <button
            onClick={() => setEditing(true)}
            type="button"
            data-testid={`edit-${provider}`}
          >
            {summary.configured ? "Replace" : "Add key"}
          </button>
          {summary.configured && (
            <button onClick={handleClear} disabled={busy} type="button">
              Clear
            </button>
          )}
          <FixedTestButton provider={provider} disabled={!summary.configured || busy} />
        </div>
      )}
      {error && (
        <div className="provider-key-error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

interface CustomRowProps {
  entry: CustomEntrySummary;
  onChange: (next: ProviderKeysMasked) => void;
}

export function CustomEntryRow({ entry, onChange }: CustomRowProps) {
  const [busy, setBusy] = useState(false);
  const handleDelete = useCallback(async () => {
    setBusy(true);
    try {
      const next = await deleteCustomProviderEntry(entry.alias);
      onChange(next);
    } finally {
      setBusy(false);
    }
  }, [entry.alias, onChange]);

  return (
    <div className="provider-key-row" data-testid={`custom-row-${entry.alias}`}>
      <div className="provider-key-head">
        <strong>custom.{entry.alias}</strong>
        <span className="provider-key-status configured">
          {entry.api_style}, {entry.key_preview ?? "…"}
        </span>
      </div>
      <div className="provider-key-meta">
        <code>{entry.base_url}</code>
        {entry.model && <> · model <code>{entry.model}</code></>}
      </div>
      <div className="provider-key-actions">
        <button onClick={handleDelete} disabled={busy} type="button">
          Delete
        </button>
        <CustomTestButton alias={entry.alias} disabled={busy} />
      </div>
    </div>
  );
}

export function FixedTestButton({
  provider,
  disabled,
}: {
  provider: FixedProvider | "local";
  disabled: boolean;
}) {
  const [status, trigger] = useTestConnection(
    useCallback(() => testFixedProvider(provider), [provider]),
  );
  return (
    <>
      <button
        type="button"
        onClick={trigger}
        disabled={disabled || status.state === "testing"}
        data-testid={`test-${provider}`}
      >
        Test
      </button>
      <StatusBadge status={status} />
    </>
  );
}

export function CustomTestButton({
  alias,
  disabled,
}: {
  alias: string;
  disabled: boolean;
}) {
  const [status, trigger] = useTestConnection(
    useCallback(() => testCustomAlias(alias), [alias]),
  );
  return (
    <>
      <button
        type="button"
        onClick={trigger}
        disabled={disabled || status.state === "testing"}
        data-testid={`test-custom-${alias}`}
      >
        Test
      </button>
      <StatusBadge status={status} />
    </>
  );
}

const SUBSCRIPTION_CLI_HELP: Record<string, { label: string; hint: string }> = {
  claude_cli: {
    label: "Claude CLI",
    hint: "Use a Claude Pro/Max subscription. Errorta never stores your credential — the Claude CLI owns the login.",
  },
  codex_cli: {
    label: "Codex CLI",
    hint: "Use a ChatGPT subscription. Errorta never stores your credential — the Codex CLI owns the login.",
  },
  cursor_cli: {
    label: "Cursor CLI",
    hint: "Use a Cursor subscription. Errorta never stores your credential — the agent CLI owns the login.",
  },
};

// Open an external URL through the Tauri shell (falls back to window.open in
// plain browser dev). Mirrors RunPreviewPanel.openExternalDemo.
async function openExternalUrl(url: string): Promise<void> {
  try {
    const shell = await import("@tauri-apps/plugin-shell");
    await shell.open(url);
    return;
  } catch {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

// "Detected at <path> (v1.2.3, via Homebrew)" — each clause is conditional.
function detectedLine(cli: CliStatus): string {
  let line = "Detected";
  if (cli.path) line += ` at ${cli.path}`;
  const meta: string[] = [];
  if (cli.version) meta.push(`v${cli.version}`);
  if (cli.source) meta.push(`via ${cli.source}`);
  if (meta.length > 0) line += ` (${meta.join(", ")})`;
  return line;
}

function formatVerifiedAt(verifiedAt: string | null): string {
  if (!verifiedAt) return "";
  const epoch = Number(verifiedAt);
  if (Number.isFinite(epoch) && epoch > 0) {
    const ms = epoch < 1e12 ? epoch * 1000 : epoch;
    const d = new Date(ms);
    if (!Number.isNaN(d.getTime())) return d.toLocaleString();
  }
  return verifiedAt;
}

export function SubscriptionCliRow({ provider }: { provider: ProviderListItem }) {
  const providerClass = provider.provider_class;
  const help = SUBSCRIPTION_CLI_HELP[providerClass] ?? {
    label: provider.display_name,
    hint: "Install and log in to this CLI before assigning it to a room. Errorta never stores its credential.",
  };

  const [cli, setCli] = useState<CliStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [loginMeta, setLoginMeta] = useState<CliLoginCommand | null>(null);
  // F040-01 S5a: whether the native one-click login launcher is usable.
  // Cached once on mount; falls back to copy-command when false/unknown.
  const [launchAvailable, setLaunchAvailable] = useState(false);
  // Shown after a successful native launch ("finish in Terminal").
  const [launchNotice, setLaunchNotice] = useState<string | null>(null);

  const [testStatus, runTest] = useTestConnection(
    useCallback(() => testProvider(providerClass), [providerClass]),
  );

  // Cheap auto re-detect: on mount + on window focus. NEVER the billable probe.
  const redetect = useCallback(async () => {
    try {
      const next = await getCliStatus(providerClass);
      setCli(next);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [providerClass]);

  useEffect(() => {
    let cancelled = false;
    void getCliStatus(providerClass)
      .then((next) => {
        if (!cancelled) {
          setCli(next);
          setLoadError(null);
        }
      })
      .catch((err) => {
        if (!cancelled)
          setLoadError(err instanceof Error ? err.message : String(err));
      });
    const onFocus = () => void redetect();
    window.addEventListener("focus", onFocus);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", onFocus);
    };
  }, [providerClass, redetect]);

  // After an explicit Test completes, re-detect so the cached connected/login
  // flows into the cheap status payload.
  const prevTestState = useRef<TestStatus["state"]>("idle");
  useEffect(() => {
    if (
      (testStatus.state === "ok" ||
        testStatus.state === "fail" ||
        testStatus.state === "logged_out") &&
      prevTestState.current === "testing"
    ) {
      void redetect();
    }
    prevTestState.current = testStatus.state;
  }, [testStatus.state, redetect]);

  // F040-01 S5a: read launcher availability once on mount (cached).
  useEffect(() => {
    let cancelled = false;
    void cliLoginLaunchAvailable()
      .then((available) => {
        if (!cancelled) setLaunchAvailable(available);
      })
      .catch(() => {
        if (!cancelled) setLaunchAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Clear the "finish in Terminal" notice once the row reconnects.
  useEffect(() => {
    if (cli?.connected === true && launchNotice) setLaunchNotice(null);
  }, [cli?.connected, launchNotice]);

  const ensureLoginMeta = useCallback(async (): Promise<CliLoginCommand | null> => {
    if (loginMeta) return loginMeta;
    try {
      const meta = await getCliLoginCommand(providerClass);
      setLoginMeta(meta);
      return meta;
    } catch {
      return null;
    }
  }, [loginMeta, providerClass]);

  const copyText = useCallback(async (key: string, text: string) => {
    if (!text) return;
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(key);
    } catch {
      // Clipboard unavailable — leave the text visible to copy by hand.
    }
  }, []);

  const handleInstall = useCallback(async () => {
    const meta = await ensureLoginMeta();
    if (meta?.installUrl) void openExternalUrl(meta.installUrl);
  }, [ensureLoginMeta]);

  const handleCopyInstall = useCallback(async () => {
    const meta = await ensureLoginMeta();
    if (meta?.installCommand) void copyText("install", meta.installCommand);
  }, [ensureLoginMeta, copyText]);

  const handleCopyLogin = useCallback(async () => {
    const meta = await ensureLoginMeta();
    if (meta && meta.loginArgv.length > 0)
      void copyText("login", meta.loginArgv.join(" "));
  }, [ensureLoginMeta, copyText]);

  // F040-01 S5a: one-click native login. When the launcher is available and
  // we have a detected binary path, launch the vendor's own login in a
  // terminal. On `launched` show a notice + let the existing focus re-detect
  // reconnect the row. On `unavailable`/throw, fall back to copy-command.
  const handleLogin = useCallback(async () => {
    const path = cli?.path ?? "";
    if (launchAvailable && path) {
      try {
        const result = await launchCliLogin(providerClass, path);
        if (result.launched) {
          setLaunchNotice(
            "Login opened in Terminal — finish there; it'll reconnect.",
          );
          return;
        }
        // transport === "unavailable" — fall through to copy-command.
      } catch {
        // Native launch failed — fall through to copy-command.
      }
    }
    await handleCopyLogin();
  }, [cli?.path, launchAvailable, providerClass, handleCopyLogin]);

  const handleLocate = useCallback(async () => {
    setBusy(true);
    try {
      const paths = await pickPaths({ requireAbsolutePath: true });
      if (paths.length > 0) {
        const next = await setCliBinary(providerClass, paths[0]);
        setCli(next);
        setLoadError(null);
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [providerClass]);

  const handleClearBinary = useCallback(async () => {
    setBusy(true);
    try {
      const next = await clearCliBinary(providerClass);
      setCli(next);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [providerClass]);

  const installed = cli?.state === "installed";
  const connected = cli?.connected === true;

  // ---- State-driven status line + actions -----------------------------
  let statusLine: JSX.Element;
  if (cli == null) {
    statusLine = (
      <span className="provider-key-status missing" data-testid={`cli-state-${providerClass}`}>
        Detecting…
      </span>
    );
  } else if (!installed) {
    statusLine = (
      <span
        className="provider-key-status missing"
        data-testid={`cli-state-${providerClass}`}
      >
        Not found
      </span>
    );
  } else if (connected) {
    statusLine = (
      <span
        className="provider-key-status configured cli-connected"
        data-testid={`cli-state-${providerClass}`}
      >
        ✓ Connected
        {cli.login ? ` — logged in as ${cli.login}` : ""}
      </span>
    );
  } else {
    statusLine = (
      <span
        className="provider-key-status configured"
        data-testid={`cli-state-${providerClass}`}
      >
        {detectedLine(cli)}
        {cli.connected === false ? " — not logged in" : ""}
      </span>
    );
  }

  return (
    <div
      className="provider-key-row"
      data-provider={providerClass}
      data-testid={`provider-row-${providerClass}`}
    >
      <div className="provider-key-head">
        <strong>{help.label}</strong>
        {statusLine}
      </div>
      <p className="provider-keys-help">{help.hint}</p>

      {connected && cli?.verifiedAt ? (
        <p className="provider-key-meta" data-testid={`cli-verified-${providerClass}`}>
          Verified {formatVerifiedAt(cli.verifiedAt)}
        </p>
      ) : null}

      {/* not_installed: Install + copy command + Locate binary */}
      {cli != null && !installed ? (
        <>
          <div className="provider-key-actions">
            <button
              type="button"
              onClick={() => void handleInstall()}
              disabled={busy}
              data-testid={`cli-install-${providerClass}`}
            >
              Install
            </button>
            <button
              type="button"
              onClick={() => void handleCopyInstall()}
              disabled={busy}
              data-testid={`cli-copy-install-${providerClass}`}
            >
              {copied === "install" ? "Copied install command" : "Copy install command"}
            </button>
            <button
              type="button"
              onClick={() => void handleLocate()}
              disabled={busy}
              data-testid={`cli-locate-${providerClass}`}
            >
              Locate binary…
            </button>
          </div>
          {loginMeta?.installCommand ? (
            <pre className="provider-cli-command" data-testid={`cli-install-cmd-${providerClass}`}>
              {loginMeta.installCommand}
            </pre>
          ) : null}
        </>
      ) : null}

      {/* installed but not connected: Log in (copy) + Test + Locate/Clear */}
      {installed && !connected ? (
        <>
          <div className="provider-key-actions">
            <button
              type="button"
              onClick={() => void handleLogin()}
              disabled={busy}
              data-testid={`cli-login-${providerClass}`}
            >
              {copied === "login"
                ? "Login command copied"
                : launchAvailable
                  ? "Log in"
                  : "Log in (copy command)"}
            </button>
            <button
              type="button"
              onClick={runTest}
              disabled={busy || testStatus.state === "testing"}
              data-testid={`test-${providerClass}`}
            >
              Verify login
            </button>
            <button
              type="button"
              onClick={() => void handleLocate()}
              disabled={busy}
              data-testid={`cli-locate-${providerClass}`}
            >
              Locate binary…
            </button>
            {cli?.source === "override_settings" ? (
              <button
                type="button"
                onClick={() => void handleClearBinary()}
                disabled={busy}
                data-testid={`cli-clear-${providerClass}`}
              >
                Clear binary override
              </button>
            ) : null}
            <StatusBadge status={testStatus} />
          </div>
          {launchNotice ? (
            <p
              className="provider-key-meta"
              role="status"
              data-testid={`cli-login-notice-${providerClass}`}
            >
              {launchNotice}
            </p>
          ) : null}
          {loginMeta && loginMeta.loginArgv.length > 0 ? (
            <pre className="provider-cli-command" data-testid={`cli-login-cmd-${providerClass}`}>
              {loginMeta.loginArgv.join(" ")}
            </pre>
          ) : null}
        </>
      ) : null}

      {/* connected: Re-check + Locate/Clear */}
      {connected ? (
        <div className="provider-key-actions">
          <button
            type="button"
            onClick={runTest}
            disabled={busy || testStatus.state === "testing"}
            data-testid={`test-${providerClass}`}
          >
            Re-check
          </button>
          <button
            type="button"
            onClick={() => void handleLocate()}
            disabled={busy}
            data-testid={`cli-locate-${providerClass}`}
          >
            Locate binary…
          </button>
          {cli?.source === "override_settings" ? (
            <button
              type="button"
              onClick={() => void handleClearBinary()}
              disabled={busy}
              data-testid={`cli-clear-${providerClass}`}
            >
              Clear binary override
            </button>
          ) : null}
          <StatusBadge status={testStatus} />
        </div>
      ) : null}

      {/* error from a Test probe (redacted detail) shows via StatusBadge above;
          surface explicit Retry text when the last probe failed. */}
      {testStatus.state === "fail" && installed ? (
        <p className="provider-key-error" role="alert" data-testid={`cli-probe-error-${providerClass}`}>
          {testStatus.detail} — Test again to retry.
        </p>
      ) : null}

      {/* F120: a logged-out CLI is actionable, not a generic error — show the
          remediation + retry, never a bare exit string. */}
      {testStatus.state === "logged_out" && installed ? (
        <p
          className="provider-key-warn"
          role="alert"
          data-testid={`cli-logged-out-${providerClass}`}
        >
          Not logged in. {testStatus.remediation} Then Test again.
        </p>
      ) : null}

      {loadError ? (
        <div className="provider-key-error" role="alert">
          {loadError}
        </div>
      ) : null}
    </div>
  );
}

interface AddCustomFormProps {
  onChange: (next: ProviderKeysMasked) => void;
}

export function AddCustomForm({ onChange }: AddCustomFormProps) {
  const [alias, setAlias] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiStyle, setApiStyle] = useState<ApiStyle>(
    "openai_chat_completions",
  );
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const reset = () => {
    setAlias("");
    setBaseUrl("");
    setApiKey("");
    setApiStyle("openai_chat_completions");
    setModel("");
  };

  const handleSubmit = useCallback(async () => {
    if (!alias || !baseUrl || !apiKey) {
      setError("alias, base_url, and api_key are required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const payload: CustomEntryPayload = {
        alias,
        base_url: baseUrl,
        api_key: apiKey,
        api_style: apiStyle,
      };
      if (model) payload.model = model;
      const next = await putCustomProviderEntry(payload);
      onChange(next);
      reset();
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [alias, baseUrl, apiKey, apiStyle, model, onChange]);

  if (!open) {
    return (
      <button
        className="provider-key-add"
        onClick={() => setOpen(true)}
        type="button"
        data-testid="custom-add-button"
      >
        + Add custom provider
      </button>
    );
  }

  return (
    <div className="provider-key-form" data-testid="custom-add-form">
      <label>
        Alias
        <input
          value={alias}
          onChange={(e) => setAlias(e.target.value)}
          placeholder="lmstudio"
          disabled={busy}
        />
      </label>
      <label>
        Base URL
        <input
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="http://127.0.0.1:1234/v1"
          disabled={busy}
        />
      </label>
      <label>
        API key
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="(secret)"
          autoComplete="off"
          disabled={busy}
        />
      </label>
      <label>
        API style
        <select
          value={apiStyle}
          onChange={(e) => setApiStyle(e.target.value as ApiStyle)}
          disabled={busy}
        >
          <option value="openai_chat_completions">
            OpenAI chat completions
          </option>
          <option value="anthropic_messages">Anthropic messages</option>
          <option value="raw">Raw</option>
        </select>
      </label>
      <label>
        Model (optional)
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="qwen2.5-coder-7b"
          disabled={busy}
        />
      </label>
      <div className="provider-key-actions">
        <button onClick={handleSubmit} disabled={busy} type="button">
          Add
        </button>
        <button
          onClick={() => {
            reset();
            setError(null);
            setOpen(false);
          }}
          disabled={busy}
          type="button"
        >
          Cancel
        </button>
      </div>
      {error && (
        <div className="provider-key-error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
