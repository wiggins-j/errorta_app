// INTEGRATION — onboarding step 2: hardware scan.
//
// Runs the hardware probe and SHOWS the result (detected GPU/RAM/CPU + the
// recommended local model) instead of vanishing to the next step. The scan no
// longer auto-advances — the user reviews the results, optionally changes the
// model, and clicks Next. The chosen tier id is persisted to
// `errorta.selectedModel` (same key + semantics as the standalone Hardware
// feature, features/hardware/index.tsx) so the "Connect AI" Ollama section can
// offer exactly that model for download.
import { useState } from "react";
import * as hardwareApi from "../../lib/api/hardware";
import type { HardwareReport, ModelTier } from "../../features/hardware/types";

interface Props {
  onAdvance: () => void;
  onSkip: () => void;
  done: boolean;
}

const SELECTED_MODEL_KEY = "errorta.selectedModel";

function persistSelectedModel(id: string): void {
  try {
    localStorage.setItem(SELECTED_MODEL_KEY, id);
  } catch {
    // localStorage may be unavailable; the choice is optional.
  }
}

function TierRow({
  tier,
  selected,
  onSelect,
}: {
  tier: ModelTier;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <label
      className={`hw-tier${selected ? " is-selected" : ""}${tier.compatible ? "" : " is-incompatible"}`}
      data-testid={`hw-tier-${tier.id}`}
    >
      <input
        type="radio"
        name="hw-model-tier"
        checked={selected}
        disabled={!tier.compatible}
        onChange={onSelect}
      />
      <span className="hw-tier-body">
        <span className="hw-tier-title">
          <strong>{tier.label}</strong> <code>{tier.id}</code>
        </span>
        <span className="hw-tier-meta">
          {tier.install_label} · {tier.vram_label} · {tier.tok_label}
        </span>
        {!tier.compatible && tier.incompatible_reason ? (
          <span className="hw-tier-warn">{tier.incompatible_reason}</span>
        ) : null}
      </span>
    </label>
  );
}

function HardwareResults({
  report,
  selectedId,
  onSelectModel,
}: {
  report: HardwareReport;
  selectedId: string | null;
  onSelectModel: (id: string) => void;
}) {
  const rec = report.recommendation;
  // Candidate tiers to choose from: primary + the faster/capable alternatives,
  // de-duped, compatible ones first.
  const tiers: ModelTier[] = [rec.primary, rec.faster, rec.capable].filter(
    (t): t is ModelTier => t != null,
  );
  const seen = new Set<string>();
  const uniqueTiers = tiers.filter((t) => (seen.has(t.id) ? false : seen.add(t.id)));
  const noneFit = !rec.primary.compatible;

  return (
    <div className="hw-results" data-testid="hw-results">
      <dl className="hw-detected">
        <div>
          <dt>GPU</dt>
          <dd>
            {report.gpu.vendor} {report.gpu.model} · {report.gpu.vram_gb} GB VRAM
            {report.gpu.unified_memory ? " (unified memory)" : ""}
          </dd>
        </div>
        <div>
          <dt>Memory</dt>
          <dd>{report.ram_gb} GB RAM · {report.disk_free_gb} GB free disk</dd>
        </div>
        <div>
          <dt>CPU</dt>
          <dd>
            {report.cpu.model} · {report.cpu.cores} cores
          </dd>
        </div>
      </dl>

      <div className="hw-recommendation">
        <h3>Recommended local model</h3>
        <p className="onboarding-detail">{rec.rationale}</p>
        {noneFit ? (
          <p className="onboarding-error" data-testid="hw-none-fit" role="status">
            No local model comfortably fits this machine ({rec.available_vram_gb} GB
            available). You can still connect a provider API key or a Claude /
            ChatGPT / Cursor subscription on the next step and run Errorta that way.
          </p>
        ) : (
          <div className="hw-tiers" role="radiogroup" aria-label="Local model">
            {uniqueTiers.map((tier) => (
              <TierRow
                key={tier.id}
                tier={tier}
                selected={selectedId === tier.id}
                onSelect={() => onSelectModel(tier.id)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function StepHardware({ onAdvance, onSkip, done }: Props) {
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<HardwareReport | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const runScan = async () => {
    setScanning(true);
    setError(null);
    try {
      const r = await hardwareApi.scan();
      setReport(r);
      // Pre-select the recommended primary tier when it fits (OQ2), and persist
      // the handoff for the Ollama download CTA (OQ9: skip when nothing fits).
      if (r.recommendation.primary.compatible) {
        setSelectedId(r.recommendation.primary.id);
        persistSelectedModel(r.recommendation.primary.id);
      } else {
        setSelectedId(null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  };

  const onSelectModel = (id: string) => {
    setSelectedId(id);
    persistSelectedModel(id);
  };

  return (
    <div className="onboarding-step">
      <h2>Detect your hardware</h2>
      <p>
        Errorta scans this machine's GPU, memory, and CPU to recommend a local
        model that will actually run well. Nothing leaves your machine.
      </p>
      {error ? <p className="onboarding-error">Scan failed: {error}</p> : null}
      {scanning ? (
        <p className="onboarding-detail" role="status" data-testid="hw-scanning">
          Scanning this machine…
        </p>
      ) : null}

      {report ? (
        <HardwareResults
          report={report}
          selectedId={selectedId}
          onSelectModel={onSelectModel}
        />
      ) : null}

      <div className="onboarding-actions">
        <button
          type="button"
          className="onboarding-cta-primary"
          onClick={report ? onAdvance : runScan}
          disabled={scanning}
          data-testid={report ? "onboarding-hardware-next" : "onboarding-hardware-scan"}
        >
          {scanning
            ? "Scanning…"
            : report
              ? "Next"
              : done
                ? "Re-scan"
                : "Scan hardware"}
        </button>
        {report ? (
          <button
            type="button"
            className="onboarding-cta-secondary"
            onClick={runScan}
            disabled={scanning}
            data-testid="onboarding-hardware-rescan"
          >
            Re-scan
          </button>
        ) : (
          <button
            type="button"
            className="onboarding-cta-secondary"
            onClick={onAdvance}
            data-testid="onboarding-hardware-skip-step"
          >
            Skip
          </button>
        )}
        <button
          type="button"
          className="onboarding-cta-link"
          onClick={onSkip}
        >
          Skip setup
        </button>
      </div>
    </div>
  );
}
