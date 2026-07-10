// F002 — hardware scan + model recommendation feature pane.
import { useCallback, useEffect, useState } from "react";
import * as hardwareApi from "../../lib/api/hardware";
import { HardwareScanModal } from "./HardwareScanModal";
import { HardwareResultsPanel } from "./HardwareResultsPanel";
import { ModelRecommendationCard } from "./ModelRecommendationCard";
import { AlternativeModelsList } from "./AlternativeModelsList";
import { IncompatibleModelAlert } from "./IncompatibleModelAlert";
import { RecommendationActionButtons } from "./RecommendationActionButtons";
import type { HardwareReport, ModelTier } from "./types";

export default function HardwareFeature() {
  const [report, setReport] = useState<HardwareReport | null>(null);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string>("");

  const runScan = useCallback(async () => {
    setScanning(true);
    setError(null);
    try {
      const r = await hardwareApi.scan();
      setReport(r);
      setSelectedId(r.recommendation.primary.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  }, []);

  // On first mount, try to load a persisted report; if none, trigger a scan.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await hardwareApi.report();
        if (!cancelled) {
          setReport(r);
          setSelectedId(r.recommendation.primary.id);
        }
      } catch {
        if (!cancelled) {
          await runScan();
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runScan]);

  const handleUse = useCallback((model: ModelTier) => {
    // F110 — record the choice. The actual `ollama pull` happens in the
    // "Connect your AI" onboarding step, which reads errorta.selectedModel and
    // offers an explicit, sized download. Persisting here is the handoff.
    try {
      localStorage.setItem("errorta.selectedModel", model.id);
    } catch {
      // ignore
    }
  }, []);

  return (
    <section className="feature-pane">
      <header className="feature-pane-header">
        <h1>Hardware</h1>
        <p className="feature-pane-spec">F002 — hardware scan + model recommendation</p>
      </header>

      {scanning ? <HardwareScanModal visible /> : null}

      {error ? (
        <p className="feature-pane-note" style={{ color: "#b00020" }}>
          Scan failed: {error}
          <br />
          <button type="button" onClick={runScan} style={{ marginTop: 8 }}>
            Retry
          </button>
        </p>
      ) : null}

      {report ? (
        <>
          <HardwareResultsPanel report={report} />
          <ModelRecommendationCard
            model={report.recommendation.primary}
            rationale={report.recommendation.rationale}
          />
          <AlternativeModelsList
            faster={report.recommendation.faster}
            capable={report.recommendation.capable}
            onPick={(m) => setSelectedId(m.id)}
          />
          <IncompatibleModelAlert incompatible={report.recommendation.incompatible} />
          <RecommendationActionButtons
            primary={report.recommendation.primary}
            allModels={report.recommendation.all}
            selectedId={selectedId || report.recommendation.primary.id}
            onSelect={setSelectedId}
            onUseSelected={handleUse}
          />
          <p className="feature-pane-note">
            <button type="button" onClick={runScan} disabled={scanning}>
              Re-scan hardware
            </button>
            <span style={{ marginLeft: 8, fontSize: 12, color: "var(--muted, #666)" }}>
              Last scanned: {report.scanned_at}
            </span>
          </p>
        </>
      ) : null}
    </section>
  );
}
