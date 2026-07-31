# Spec 33 — The sidecar log handler must never crash on emit

**Source:** Run 10 (`gravity-golf`, 2026-07-31). The SPEC-22 sidecar log handler
threw while logging, from the background grounding sync:

```
--- Logging error ---
Traceback (most recent call last):
  File "errorta_app/sidecar_log.py", line 97, in emit
AttributeError: 'NoneType' object has no attribute 'write'
  File "errorta_council/coding/runner.py", line 1489/1512, in _sync_grounding
  File "errorta_app/sidecar_log.py", line 100, in emit
    self.handleError(record)
```

`self.stream` is `None` when `_sync_grounding` logs, so `self.stream.write(msg)`
(line 97) raises `AttributeError`; the `except` calls `handleError`, whose default
implementation ALSO writes to a stream that isn't there, producing the nested
"Logging error". Caught (non-fatal), but it spews tracebacks into the very log
SPEC-22 added for diagnosability and drops the log lines it was trying to write —
the diagnosability code defeating itself.
**Target version:** v0.1 (CLI/sidecar — `errorta_app/sidecar_log.py`)
**Depends on:** [SPEC-22](SPEC-22-diagnosable-failures.md) (the handler this hardens)
**Status:** proposed · **Owner:** wiggins-j

---

## Problem

Two defects compound:

1. **Root cause — `self.stream` is `None` mid-run.** The handler's target stream is
   absent when `_sync_grounding` (a background task) emits. Why it is `None` needs
   confirming — a closed/rotated fd, a handler constructed without a stream, or a
   worker thread emitting after teardown — but the emit path assumes a live stream
   and dereferences it unconditionally.
2. **Amplifier — `handleError` re-raises into the same absent stream.** The fallback
   for a failed emit tries to write to a stream that is exactly what just failed,
   turning one dropped line into a nested traceback per emit.

## Principle

> A logging handler must NEVER raise, and must never turn a dropped line into more
> noise than the line itself. SPEC-22's log is a diagnostic aid; an aid that
> crashes on use is worse than none.

## What this spec does

1. **Guard the stream.** In `emit`, if `self.stream` is falsy, drop the record
   silently (or route to a last-resort fallback), never dereference `None`.
2. **Make `handleError` inert here.** Override so a failed emit does not attempt
   another write to the same missing stream — swallow it (the handler is
   best-effort by contract), so no nested "Logging error".
3. **Find and fix why the stream is `None`.** Confirm the lifecycle: if the
   background `_sync_grounding` can emit after the stream is closed/rotated, either
   keep the stream open for the handler's lifetime or re-open lazily on emit. The
   guard (1/2) is the safety net; this is the actual fix.

## Regression locks

1. `emit` never raises, for any stream state (None, closed, full/capped).
2. The rotation/cap behaviour (`_max_bytes`, `_CAPPED_NOTICE`) is unchanged for a
   live stream.
3. A normal run's log output is byte-identical when the stream is healthy.

## Definition of done

- A unit test drives `emit` with `self.stream = None` (and a closed stream) and
  asserts no exception and no nested "Logging error".
- A gravity-golf-shaped run's sidecar log contains no `--- Logging error ---`
  blocks from the grounding sync.
