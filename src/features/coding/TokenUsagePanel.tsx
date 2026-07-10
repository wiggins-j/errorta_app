import { useCallback, useEffect, useState } from "react";

import {
  getProjectUsageSummary,
  type ProjectUsageSummary,
  type TokenUsageBucket,
} from "../../lib/api/coding";
import { formatTokens } from "./formatTokens";

export interface TokenUsagePanelProps {
  projectId: string;
}

const ESTIMATED_TITLE = "locally tokenized — not provider-reported";

/**
 * Whether a bucket's headline NUMBER should be tagged as an estimate ("~ / not
 * provider-reported"). Only a WHOLLY-estimated bucket qualifies: its entire
 * headline came from local estimation (coverage 0% measured) AND it is not the
 * legacy pre-accounting case (those bytes WERE provider-reported, just never
 * token-attributed — see isLegacyUnattributed). A bucket that is partly measured
 * renders PLAIN numbers; the coverage line discloses the measured/estimated split.
 * This is deliberate: tagging a mostly-measured total "~ not provider-reported"
 * would contradict its own "N% measured" coverage line and mislead the user.
 * (isLegacyUnattributed is hoisted, so referencing it here is fine.)
 */
function isWhollyEstimated(bucket: TokenUsageBucket): boolean {
  const total = bucket.input + bucket.output;
  return total > 0 && bucket.coverage.measuredPct === 0 && !isLegacyUnattributed(bucket);
}

/**
 * The legacy edge (F143): a pre-feature ledger can carry turns that were counted
 * (measuredTurns > 0) but never token-attributed, so coverage reads 0% measured
 * despite a "measured" turn count. Annotate so "1 measured turn · 0% measured"
 * isn't confusing.
 */
function isLegacyUnattributed(bucket: TokenUsageBucket): boolean {
  return (
    bucket.measuredTurns > 0 &&
    bucket.coverage.measuredPct === 0 &&
    bucket.input + bucket.output > 0
  );
}

/** A count that is estimated renders with a leading `~` + muted styling + tooltip. */
function EstimatedNum({ value }: { value: number }) {
  return (
    <span className="coding-usage-est" title={ESTIMATED_TITLE}>
      ~{formatTokens(value)}
    </span>
  );
}

/** The coverage line: "N% measured · M% estimated", honest about zero-token buckets. */
function CoverageLine({ bucket }: { bucket: TokenUsageBucket }) {
  const total = bucket.input + bucket.output;
  if (total <= 0) {
    return <span className="coding-usage-cov">no tokens yet</span>;
  }
  const { measuredPct, estimatedPct } = bucket.coverage;
  return (
    <span className="coding-usage-cov">
      {measuredPct}% measured
      {estimatedPct > 0 ? (
        <>
          {" · "}
          <span className="coding-usage-est" title={ESTIMATED_TITLE}>
            {estimatedPct}% estimated
          </span>
        </>
      ) : null}
    </span>
  );
}

function BucketRow({ label, bucket }: { label: string; bucket: TokenUsageBucket }) {
  const estimated = isWhollyEstimated(bucket);
  const total = bucket.input + bucket.output;
  const noTokens = total <= 0;
  const legacy = isLegacyUnattributed(bucket);

  const cacheBits: string[] = [];
  if (bucket.cacheRead > 0) cacheBits.push(`${formatTokens(bucket.cacheRead)} cache read`);
  if (bucket.cacheWrite > 0) cacheBits.push(`${formatTokens(bucket.cacheWrite)} cache write`);

  // A wholly-estimated bucket tags its headline numbers; a measured bucket renders
  // them plain. A bucket with no headline tokens shows an em-dash, never a 0.
  const renderNum = (value: number) => {
    if (noTokens) return <span className="coding-usage-none">—</span>;
    return estimated ? <EstimatedNum value={value} /> : formatTokens(value);
  };

  return (
    <tr>
      <th scope="row" className="coding-usage-key">
        {label}
      </th>
      <td className="coding-usage-num">{renderNum(bucket.input)}</td>
      <td className="coding-usage-num">{renderNum(bucket.output)}</td>
      <td
        className="coding-usage-num"
        title={cacheBits.length ? cacheBits.join(" · ") : undefined}
      >
        {renderNum(total)}
        {cacheBits.length ? <span className="coding-usage-cache-dot"> •</span> : null}
      </td>
      <td className="coding-usage-cov-cell">
        <CoverageLine bucket={bucket} />
        {legacy ? (
          <span
            className="coding-usage-legacy"
            title="Older turns were counted but recorded before token accounting — no per-token attribution."
          >
            {" "}
            (pre-accounting turns)
          </span>
        ) : null}
      </td>
    </tr>
  );
}

function Breakdown({
  caption,
  rows,
}: {
  caption: string;
  rows: Array<[string, TokenUsageBucket]>;
}) {
  if (rows.length === 0) return null;
  // Busiest first so the biggest spender reads at the top.
  const sorted = rows
    .slice()
    .sort((a, b) => b[1].input + b[1].output - (a[1].input + a[1].output));
  return (
    <table className="coding-usage-table">
      <caption>{caption}</caption>
      <thead>
        <tr>
          <th scope="col">&nbsp;</th>
          <th scope="col">In</th>
          <th scope="col">Out</th>
          <th scope="col">Total</th>
          <th scope="col">Coverage</th>
        </tr>
      </thead>
      <tbody>
        {sorted.map(([key, bucket]) => (
          <BucketRow key={key} label={key} bucket={bucket} />
        ))}
      </tbody>
    </table>
  );
}

/**
 * F143 / F143-01: per-project token usage — the GENUINE grand total (measured
 * where the provider reports it, locally estimated otherwise) plus per-member,
 * per-model (route) and per-role breakdowns, so you can see which model burned
 * what across the whole project.
 *
 * Honest about provenance (F143-01): estimated tokens are tagged with a `~` and a
 * muted style ("locally tokenized — not provider-reported"); measured tokens are
 * plain. A coverage line shows the measured/estimated split. Cache tokens are a
 * per-row detail only (D4) — never folded into the headline in/out. Legacy turns
 * with no retained bytes stay em-dashed, never shown as 0 spend. Totals cover
 * turns recorded since this feature landed.
 */
export default function TokenUsagePanel({ projectId }: TokenUsagePanelProps) {
  const [usage, setUsage] = useState<ProjectUsageSummary | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setUsage(await getProjectUsageSummary(projectId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!open) return undefined;
    const id = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(id);
  }, [open, load]);

  const total = usage?.total;
  const headline = total ? total.input + total.output : 0;

  return (
    <details
      className="coding-panel coding-usage"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>
        <span>Token usage</span>
        <span className="coding-count">{formatTokens(headline)}</span>
      </summary>
      <section aria-label="Token usage">
        {error ? (
          <p className="coding-error" role="alert">
            {error}
          </p>
        ) : null}
        {!usage || total === undefined || total.turns === 0 ? (
          <p className="coding-empty">No token usage recorded yet.</p>
        ) : (
          <>
            <p className="coding-usage-total">
              <strong>{formatTokens(total.input + total.output)}</strong> tokens total
              {" · "}
              {formatTokens(total.input)} in / {formatTokens(total.output)} out
              {total.cacheRead > 0 || total.cacheWrite > 0 ? (
                <span
                  className="coding-usage-cache"
                  title="Cache is a detail, not part of the in/out headline."
                >
                  {" · "}
                  {total.cacheRead > 0 ? `${formatTokens(total.cacheRead)} cache read` : ""}
                  {total.cacheRead > 0 && total.cacheWrite > 0 ? " / " : ""}
                  {total.cacheWrite > 0 ? `${formatTokens(total.cacheWrite)} cache write` : ""}
                </span>
              ) : null}
            </p>
            {total.input + total.output > 0 ? (
              <p className="coding-usage-coverage">
                {total.coverage.measuredPct}% measured
                {total.coverage.estimatedPct > 0 ? (
                  <>
                    {" · "}
                    <span className="coding-usage-est" title={ESTIMATED_TITLE}>
                      {total.coverage.estimatedPct}% estimated
                    </span>
                  </>
                ) : null}
              </p>
            ) : null}
            <p className="coding-field-hint">
              Genuine total — provider-reported where available, otherwise locally
              estimated (tagged <span className="coding-usage-est">~</span>). Cache is a
              detail, not in the in/out headline. Totals cover turns since this feature
              landed.
              {total.unreportedTurns > 0
                ? ` ${total.unreportedTurns} of ${total.turns} turns retained no bytes (shown as "—", not zero).`
                : ""}
            </p>
            <Breakdown caption="By member" rows={Object.entries(usage.byMember)} />
            <Breakdown caption="By model / route" rows={Object.entries(usage.byRoute)} />
            <Breakdown caption="By role" rows={Object.entries(usage.byRole)} />
          </>
        )}
      </section>
    </details>
  );
}
