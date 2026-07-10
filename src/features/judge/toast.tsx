// F001-polish — local-only toast with createPortal + Copy clipboard affordance.
//
// Design constraints:
//   - Zero external deps.
//   - The Copy button writes the toast message to navigator.clipboard ONLY.
//     It MUST NOT make any network request. See inline comment on the Copy
//     handler. The acceptance test asserts global.fetch is never called.
//   - The portal mounts to document.body; a single live region (aria-live="status")
//     hosts the active toast for screen-reader visibility and findByRole lookup.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

export interface ToastPayload {
  message: string;
  details?: string;
}

interface ToastCtxValue {
  show: (payload: ToastPayload) => void;
}

const ToastCtx = createContext<ToastCtxValue | null>(null);

export function useToast(): ToastCtxValue {
  const ctx = useContext(ToastCtx);
  if (!ctx) {
    // Permissive fallback for components rendered outside the provider in tests.
    return {
      show: () => {
        /* noop */
      },
    };
  }
  return ctx;
}

interface ProviderProps {
  children: ReactNode;
}

export function ToastProvider({ children }: ProviderProps) {
  const [current, setCurrent] = useState<ToastPayload | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const dismiss = useCallback(() => {
    setCurrent(null);
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const show = useCallback(
    (payload: ToastPayload) => {
      setCurrent(payload);
      if (timerRef.current) clearTimeout(timerRef.current);
      // Auto-dismiss after 8s; user can also dismiss manually.
      timerRef.current = setTimeout(() => setCurrent(null), 8000);
    },
    [],
  );

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const value = useMemo<ToastCtxValue>(() => ({ show }), [show]);

  const onCopy = useCallback(async () => {
    if (!current) return;
    // NO NETWORK — clipboard only. This handler MUST NOT invoke fetch or any
    // remote logger. Errorta is local-first; error details stay on the device
    // unless the user explicitly pastes them somewhere themselves.
    const text = current.details
      ? `${current.message}\n\n${current.details}`
      : current.message;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard may be unavailable in restricted contexts; swallow silently.
    }
  }, [current]);

  const portalTarget =
    typeof document !== "undefined" ? document.body : null;

  return (
    <ToastCtx.Provider value={value}>
      {children}
      {portalTarget && current
        ? createPortal(
            <div
              className="errorta-toast"
              role="status"
              aria-live="polite"
            >
              <div className="errorta-toast-message">{current.message}</div>
              <div className="errorta-toast-actions">
                <button
                  type="button"
                  className="errorta-toast-copy"
                  onClick={onCopy}
                >
                  Copy error details (stays local)
                </button>
                <button
                  type="button"
                  className="errorta-toast-dismiss"
                  onClick={dismiss}
                  aria-label="Dismiss"
                >
                  ×
                </button>
              </div>
            </div>,
            portalTarget,
          )
        : null}
    </ToastCtx.Provider>
  );
}
