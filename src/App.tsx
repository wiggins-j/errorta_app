import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { FeatureErrorBoundary } from "./components/FeatureErrorBoundary";
import { Sidebar, type NavNode, type SidebarEntry } from "./components/Sidebar";
import { SidecarStatusBadge } from "./components/SidecarStatusBadge";
import { BackendBanner } from "./components/BackendBanner";
import { StaleBuildBanner } from "./components/StaleBuildBanner";
import { BackendReadyProvider } from "./lib/backendReady";
import { StartupSplash } from "./components/StartupSplash";
import { useStartupGate } from "./lib/useStartupGate";
import { sidecarHealth, type SidecarHealth } from "./lib/api";
import { APP_NAVIGATION_EVENT } from "./lib/featureNavigation";
import { setPendingPrompt } from "./features/judge/pendingPrompt";
import AlphaGate from "./features/alpha/AlphaGate";
import AlphaUpdateBanner from "./features/alpha/AlphaUpdateBanner";
import { useAlphaStatus } from "./features/alpha/useAlphaStatus";
import aiarLogo from "./assets/aiar-logo.png";

export type FeatureKey =
  | "ollama"
  | "corpus"
  | "watch"
  | "settings"
  | "shell"
  | "judge"
  | "briefs"
  | "council"
  | "coding"
  | "rooms";

const ENTRIES: readonly SidebarEntry[] = [
  { key: "ollama", label: "Ollama", spec: "F003" },
  { key: "corpus", label: "Corpus", spec: "F004" },
  { key: "watch", label: "Folder Watcher", spec: "F005" },
  { key: "settings", label: "Settings", spec: "F032+F034" },
  { key: "shell", label: "Shell", spec: "F006" },
  { key: "judge", label: "Judge", spec: "F001" },
  { key: "briefs", label: "Briefs", spec: "F008" },
  { key: "council", label: "Council", spec: "F031" },
  { key: "coding", label: "Coding Team", spec: "F087" },
  { key: "rooms", label: "Rooms", spec: "F033" },
] as const;

const ENTRY_BY_KEY: Record<FeatureKey, SidebarEntry> = Object.fromEntries(
  ENTRIES.map((e) => [e.key, e]),
) as Record<FeatureKey, SidebarEntry>;

// Sidebar layout: grouped under parent headers, with Settings last as a
// standalone leaf.
const NAV_GROUPS: ReadonlyArray<{ label: string; keys: FeatureKey[] }> = [
  { label: "Workspace", keys: ["council", "coding", "rooms", "judge"] },
  { label: "Knowledge", keys: ["corpus", "briefs", "watch"] },
  // F134 — Ollama + Shell folded into Settings; the SYSTEM group is gone.
];

type CouncilStatus = "ready" | "loading" | "unavailable";

// Council is always shown. While the sidecar health is still loading, or if a
// sidecar build doesn't provide the F031 routes, the tab renders greyed-out
// with a hover explanation instead of vanishing (which looked like a bug).
function buildNav(councilStatus: CouncilStatus): NavNode[] {
  const decorate = (k: FeatureKey): SidebarEntry => {
    const base = ENTRY_BY_KEY[k];
    if (k !== "council" || councilStatus === "ready") return base;
    return {
      ...base,
      disabled: true,
      disabledReason:
        councilStatus === "loading"
          ? "Connecting to the local sidecar… Council appears once the backend reports ready."
          : "This sidecar build doesn't provide Council (the F031 routes). Rebuild or update the sidecar to enable it.",
    };
  };
  const nodes: NavNode[] = NAV_GROUPS.map((g) => ({
    kind: "group" as const,
    label: g.label,
    children: g.keys.map(decorate),
  }));
  // Settings is always last and standalone.
  nodes.push({ kind: "leaf", entry: ENTRY_BY_KEY.settings });
  return nodes;
}

const FEATURE_MODULES: Record<FeatureKey, React.LazyExoticComponent<React.ComponentType>> = {
  ollama: lazy(() => import("./features/ollama/index")),
  corpus: lazy(() => import("./features/corpus/index")),
  watch: lazy(() => import("./features/watch/index")),
  settings: lazy(() => import("./features/settings/index")),
  shell: lazy(() => import("./features/shell/index")),
  judge: lazy(() => import("./features/judge/index")),
  briefs: lazy(() => import("./features/briefs/index")),
  council: lazy(() => import("./features/council/index")),
  coding: lazy(() => import("./features/coding/index")),
  rooms: lazy(() => import("./features/rooms/index")),
};

const Onboarding = lazy(() => import("./features/onboarding/index"));

const STORAGE_KEY = "errorta.activeFeature";
const ONBOARDING_KEY = "errorta.onboarding.complete";
const SIDEBAR_COLLAPSED_KEY = "errorta.sidebar.collapsed";

function loadInitial(): FeatureKey {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    // F134 — Ollama + Shell moved into Settings; redirect a legacy stored tab.
    if (raw === "ollama" || raw === "shell") {
      return "settings";
    }
    if (raw && ENTRIES.some((e) => e.key === raw)) {
      return raw as FeatureKey;
    }
  } catch {
    // localStorage unavailable — fall through
  }
  // F113: Hardware moved into Settings; default a returning user to the lead
  // feature (Judge) instead of a now-removed sidebar entry.
  return "judge";
}

function loadOnboardingComplete(): boolean {
  try {
    return localStorage.getItem(ONBOARDING_KEY) === "1";
  } catch {
    return true;
  }
}

function loadSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function AiarPinBadge({ health }: { health: SidecarHealth | null }) {
  if (!health) return null;
  const runtime = health.aiar_runtime;
  if (runtime?.connected) {
    const model = runtime.active_model ? ` - ${runtime.active_model}` : "";
    return (
      <div
        className="aiar-pin-status"
        title={`Active AIAR runtime: ${runtime.display_name ?? runtime.runtime_kind}${model}`}
      >
        <img src={aiarLogo} alt="" className="aiar-logo-mark" aria-hidden="true" />
        <span className="aiar-version">AIAR {runtime.display_name ?? "connected"}</span>
      </div>
    );
  }
  if (!health.aiar_pin) return null;
  const { source, version } = health.aiar_pin;
  const ver = version ?? health.aiar_version ?? null;
  // The install "source" (editable / pinned / absent) is a developer detail with
  // no user action, and the pill overflowed the sidebar — keep it in the tooltip
  // only, and show just the version chip.
  return (
    <div
      className="aiar-pin-status"
      title={`Local AIAR package: ${source}${ver ? ` (${ver})` : ""}`}
    >
      <img src={aiarLogo} alt="" className="aiar-logo-mark" aria-hidden="true" />
      <span className="aiar-version">local aiar {ver ?? source}</span>
    </div>
  );
}

export default function App() {
  // F103 — gate the whole app on the local sidecar coming up. Must be the first
  // hook so it runs unconditionally before any early return below.
  const startup = useStartupGate();
  const [active, setActive] = useState<FeatureKey>(loadInitial);
  const [onboardingDone, setOnboardingDone] = useState<boolean>(loadOnboardingComplete);
  const [health, setHealth] = useState<SidecarHealth | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(loadSidebarCollapsed);
  // F-DIST-01 — poll the alpha license state once the sidecar is up. Must be a
  // top-level hook (called unconditionally, before the early returns below).
  const alpha = useAlphaStatus(startup.mode === "ready" || startup.mode === "limited");

  useEffect(() => {
    if (startup.mode !== "ready") {
      setHealth(null);
      return;
    }
    let cancelled = false;
    async function ping() {
      try {
        const h = await sidecarHealth();
        if (!cancelled) setHealth(h);
      } catch {
        // SidecarStatusBadge already surfaces unreachable state.
      }
    }
    ping();
    const id = setInterval(ping, 5000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [startup.mode]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, active);
    } catch {
      // ignore
    }
  }, [active]);

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, sidebarCollapsed ? "1" : "0");
    } catch {
      // ignore
    }
  }, [sidebarCollapsed]);

  // F040-01 — point-of-use "Set up →" affordances (e.g. an installed-but-not-
  // connected subscription CLI in the Council room editor) dispatch this event
  // to deep-link the user to the provider-keys panel in Settings.
  useEffect(() => {
    const onNavigate = (e: Event) => {
      const detail = (e as CustomEvent<{ view?: string; prompt?: string }>)
        .detail;
      if (detail?.view === "settings") setActive("settings");
      // Council's room list deep-links here ("Manage rooms →") since room
      // management moved out of the Council shell into its own tab.
      else if (detail?.view === "rooms") setActive("rooms");
      // F109 — the welcome "Suggested prompt" Run deep-links to the Judge with
      // the prompt prefilled. Stash it one-shot BEFORE switching views so the
      // judge feature consumes it on mount.
      else if (detail?.view === "judge") {
        if (typeof detail.prompt === "string" && detail.prompt.length > 0) {
          setPendingPrompt(detail.prompt);
        }
        setActive("judge");
      } else if (
        detail?.view === "briefs" ||
        detail?.view === "corpus" ||
        detail?.view === "watch"
      ) {
        setActive(detail.view);
      }
    };
    window.addEventListener(APP_NAVIGATION_EVENT, onNavigate);
    return () => window.removeEventListener(APP_NAVIGATION_EVENT, onNavigate);
  }, []);

  const completeOnboarding = () => {
    try {
      localStorage.setItem(ONBOARDING_KEY, "1");
    } catch {
      // ignore
    }
    setOnboardingDone(true);
  };

  // Gate Council on the sidecar advertising it (P2 — only mount when the
  // backend has the F031 routes; older sidecars without `council: true`
  // would otherwise produce a broken nav target).
  const councilStatus: CouncilStatus =
    health === null ? "loading" : health.council === true ? "ready" : "unavailable";
  // Memoize so the array identity is stable across the 5s health-poll
  // re-renders; only rebuilds when Council status changes.
  // NOTE: this hook MUST be called before any conditional early return below —
  // otherwise the onboarding→shell transition changes the hook count and React
  // throws "rendered more hooks than during the previous render" (blank app).
  const navNodes = useMemo(() => buildNav(councilStatus), [councilStatus]);

  // F103 — cold-launch gate. While the local sidecar is still coming up (or has
  // failed), show the full-window splash instead of the shell so the user never
  // clicks into a backend-heavy pane that can't complete work yet. `limited`
  // and `ready` fall through to the normal shell below.
  if (startup.mode === "loading" || startup.mode === "failed") {
    return (
      <StartupSplash
        failed={startup.mode === "failed"}
        state={startup.state}
        actions={startup.actions}
      />
    );
  }
  const limited = startup.mode === "limited";

  // F-DIST-01 — alpha access gate. When this build ships the gate on and the
  // tester isn't activated (or is expired/revoked/EOL), show the activation or
  // lock screen instead of the shell. Comes before onboarding: activation
  // precedes first-run setup. Server-side 403 alpha_locked is the real gate, so
  // answering remains protected even if the webview is compromised. The first
  // status read also blocks shell rendering; a gate-off production build then
  // reports unlocked and falls through normally.
  if (alpha.loading && alpha.status === null) {
    return <AlphaGate status={null} onActivated={alpha.refresh} />;
  }
  if (alpha.status?.locked) {
    return <AlphaGate status={alpha.status} onActivated={alpha.refresh} />;
  }

  if (!onboardingDone && !limited) {
    return (
      <div className="shell-root shell-root-onboarding">
        <main className="main-pane main-pane-onboarding">
          <Suspense fallback={<div className="feature-pane-loading">Loading…</div>}>
            <Onboarding onComplete={completeOnboarding} />
          </Suspense>
        </main>
        <SidecarStatusBadge />
      </div>
    );
  }

  // Don't mount Council until it's actually ready; fall back otherwise.
  const safeActive: FeatureKey =
    active === "council" && councilStatus !== "ready" ? "judge" : active;
  const ActiveFeature = FEATURE_MODULES[safeActive];
  // F069 — health is set only on a successful /healthz, so this is "the sidecar
  // has reported healthy at least once". The shell stays interactive while this
  // is false; panes grey out backend-dependent controls via useBackendReady().
  // F103 — limited mode (entered from the splash after a failure) forces
  // not-ready so degraded-state controls stay gated even if a late /healthz
  // races in.
  const backendReady = !limited && (startup.mode === "ready" || health !== null);

  return (
    <BackendReadyProvider ready={backendReady}>
      <div
        className={
          "shell-root" + (sidebarCollapsed ? " shell-root-sidebar-collapsed" : "")
        }
      >
        <Sidebar
          nodes={navNodes}
          active={safeActive}
          onSelect={setActive}
          collapsed={sidebarCollapsed}
          onCollapsedChange={setSidebarCollapsed}
        />
        <main className="main-pane">
          <BackendBanner ready={backendReady} />
          <StaleBuildBanner />
          <AlphaUpdateBanner status={alpha.status} />
          <FeatureErrorBoundary
            featureLabel={ENTRY_BY_KEY[safeActive].label}
            resetKey={safeActive}
          >
            <Suspense fallback={<div className="feature-pane-loading">Loading…</div>}>
              <ActiveFeature />
            </Suspense>
          </FeatureErrorBoundary>
        </main>
        <SidecarStatusBadge />
        <AiarPinBadge health={health} />
      </div>
    </BackendReadyProvider>
  );
}
