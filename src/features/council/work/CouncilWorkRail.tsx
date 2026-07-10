// F046 — Council work rail.
//
// A conditional side rail for tool-heavy runs: approvals (F041), tool results,
// artifacts, child runs (F042), and runner/tool health (F045/F048). It never
// appears for plain Council rooms. Tool output is always labeled untrusted and
// every displayed item links to an event id, source ref, or hash.

import { useId, useRef, useState } from "react";
import type { CouncilTranscriptEvent } from "../types";
import ApprovalsTab from "./ApprovalsTab";
import ArtifactsTab from "./ArtifactsTab";
import ChildRunsTab from "./ChildRunsTab";
import RunnerHealthTab from "./RunnerHealthTab";
import ToolResultsTab from "./ToolResultsTab";

type TabKey = "approvals" | "tools" | "artifacts" | "children" | "health";

const TABS: { key: TabKey; label: string }[] = [
  { key: "approvals", label: "Approvals" },
  { key: "tools", label: "Tool results" },
  { key: "artifacts", label: "Artifacts" },
  { key: "children", label: "Child runs" },
  { key: "health", label: "Runner health" },
];

interface Props {
  runId: string | null;
  events: CouncilTranscriptEvent[];
}

export default function CouncilWorkRail({ runId, events }: Props) {
  const [active, setActive] = useState<TabKey>("approvals");
  const baseId = useId();
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  // Arrow-key navigation across the tablist (WAI-ARIA tabs pattern).
  const onKeyDown = (e: React.KeyboardEvent) => {
    const idx = TABS.findIndex((t) => t.key === active);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const next =
        e.key === "ArrowRight"
          ? (idx + 1) % TABS.length
          : (idx - 1 + TABS.length) % TABS.length;
      const key = TABS[next].key;
      setActive(key);
      tabRefs.current[key]?.focus();
    }
  };

  return (
    <aside className="council-work-rail" aria-label="Council work rail">
      <div role="tablist" aria-label="Work rail sections" onKeyDown={onKeyDown}>
        {TABS.map((t) => {
          const selected = t.key === active;
          return (
            <button
              key={t.key}
              ref={(el) => {
                tabRefs.current[t.key] = el;
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-${t.key}`}
              aria-selected={selected}
              aria-controls={`${baseId}-panel-${t.key}`}
              tabIndex={selected ? 0 : -1}
              className={`work-rail-tab${selected ? " is-active" : ""}`}
              onClick={() => setActive(t.key)}
              data-testid={`work-rail-tab-${t.key}`}
            >
              {t.label}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`${baseId}-panel-${active}`}
        aria-labelledby={`${baseId}-tab-${active}`}
        className="work-rail-panel"
        tabIndex={0}
      >
        {active === "approvals" && <ApprovalsTab runId={runId} />}
        {active === "tools" && <ToolResultsTab events={events} />}
        {active === "artifacts" && <ArtifactsTab runId={runId} />}
        {active === "children" && <ChildRunsTab runId={runId} />}
        {active === "health" && <RunnerHealthTab />}
      </div>
    </aside>
  );
}
