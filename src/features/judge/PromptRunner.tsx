// F001-polish — PromptRunner with skeleton, non-blocking corpus hint, manual retry.
import { useCallback, useMemo, useState } from "react";
import {
  fetchPriorVerdicts,
  runVerdict,
  type PriorVerdictPayload,
  type VerdictResponse,
} from "../../lib/api/judge";
import RetryBanner, { type RetryReason } from "./RetryBanner";
import Skeleton from "./Skeleton";
import { useToast } from "./toast";

interface Props {
  judgeModel?: string | null;
  /**
   * Non-blocking corpus hint. When null/empty, a hint is rendered. Run stays
   * ENABLED regardless of this prop's value in this cycle — upstream corpus
   * wiring is deferred.
   */
  corpus?: string | null;
  /**
   * F109 — optional initial prompt (e.g. a welcome "Suggested prompt" handed off
   * via the navigate event). Seeds the prompt field once on mount; the user
   * still presses Run (no auto-run). Default empty preserves prior behavior.
   */
  initialPrompt?: string | null;
  onResult: (r: VerdictResponse) => void;
  /**
   * Fires when the prior-verdict fetch resolves after a successful run.
   * Errors are swallowed silently — the diff panel is non-critical.
   */
  onPriors?: (priors: PriorVerdictPayload[]) => void;
}

const RETRY_MAX = 3;

function detectRetryReason(
  result: VerdictResponse | null,
  err: unknown,
): RetryReason | null {
  // Verdict-tag-driven retry: judge succeeded the request but flagged itself.
  if (result && result.verdict?.failure_tags) {
    if (result.verdict.failure_tags.includes("judge_timeout")) return "timeout";
    if (result.verdict.failure_tags.includes("judge_unparseable"))
      return "unparseable";
  }
  if (err) {
    // Prefer structured status if present (forward-compat with typed errors).
    const maybeStatus =
      typeof err === "object" && err !== null && "status" in err
        ? (err as { status?: unknown }).status
        : undefined;
    if (typeof maybeStatus === "number" && maybeStatus >= 500) {
      return "server";
    }
    // Fallback: detect `HTTP 5xx` in Error.message (current api.ts shape).
    const msg = err instanceof Error ? err.message : String(err);
    if (/HTTP\s+5\d\d/.test(msg)) return "server";
    if (/timeout/i.test(msg)) return "timeout";
  }
  return null;
}

export default function PromptRunner({
  judgeModel,
  corpus,
  initialPrompt,
  onResult,
  onPriors,
}: Props) {
  // Seed once from initialPrompt (F109 handoff); empty otherwise. The lazy
  // initializer runs only on first mount, so subsequent prop changes don't
  // clobber what the user has typed.
  const [prompt, setPrompt] = useState<string>(() => initialPrompt ?? "");
  const [running, setRunning] = useState<boolean>(false);
  const [priorFound, setPriorFound] = useState<boolean>(false);
  const [retry, setRetry] = useState<{
    reason: RetryReason;
    attempts: number;
  } | null>(null);
  const toast = useToast();

  const corpusHint =
    !corpus || corpus.trim().length === 0
      ? "Pick a corpus on the Corpus tab first."
      : null;

  // Memoize the payload so manual Retry sends an identical body.
  const payload = useMemo(
    () => ({
      prompt,
      judgeModel: judgeModel ?? undefined,
    }),
    [prompt, judgeModel],
  );

  const doRun = useCallback(
    async (isRetry: boolean) => {
      setRunning(true);
      setPriorFound(false);
      if (!isRetry) setRetry(null);
      let result: VerdictResponse | null = null;
      let caught: unknown = null;
      try {
        result = await runVerdict(payload.prompt, {
          judge_model: payload.judgeModel,
        });
        onResult(result);
        if (result.prompt_signature) {
          fetchPriorVerdicts(result.prompt_signature, 5)
            .then((resp) => {
              if (resp.priors.length >= 1) setPriorFound(true);
              onPriors?.(resp.priors);
            })
            .catch(() => {
              // Diff is wedge polish, non-blocking.
            });
        } else {
          onPriors?.([]);
        }
      } catch (e: unknown) {
        caught = e;
      } finally {
        setRunning(false);
      }

      const reason = detectRetryReason(result, caught);
      if (reason) {
        setRetry((prev) => ({
          reason,
          attempts: isRetry && prev ? prev.attempts + 1 : 1,
        }));
      } else {
        setRetry(null);
      }

      if (caught) {
        const message =
          caught instanceof Error ? caught.message : String(caught);
        toast.show({
          message: "Couldn't run the judge.",
          details: message,
        });
      }
    },
    [payload, onResult, onPriors, toast],
  );

  const onRun = () => {
    void doRun(false);
  };

  const onRetry = () => {
    void doRun(true);
  };

  return (
    <div className="prompt-runner" aria-busy={running}>
      <label htmlFor="prompt-input">
        <strong>Prompt</strong>
      </label>
      <textarea
        id="prompt-input"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder="Ask Errorta a question…"
        disabled={running}
      />
      {corpusHint && (
        <div
          className="prompt-runner-hint"
          role="note"
          aria-live="polite"
        >
          {corpusHint}
        </div>
      )}
      <div className="actions">
        <button
          type="button"
          onClick={onRun}
          // NOTE: Run stays ENABLED regardless of corpus prop this cycle.
          disabled={running || prompt.trim().length === 0}
        >
          {running ? "Running…" : "Run"}
        </button>
        {priorFound && (
          <span className="prior-found-chip" role="status">
            Prior verdict found
          </span>
        )}
      </div>
      {running && <Skeleton variant="verdict" rows={3} />}
      {retry && (
        <RetryBanner
          reason={retry.reason}
          attempts={retry.attempts}
          max={RETRY_MAX}
          onRetry={onRetry}
        />
      )}
    </div>
  );
}
