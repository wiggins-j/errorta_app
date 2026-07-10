// F069 — a tiny readiness context so panes can grey out backend-dependent
// controls while the local sidecar is still booting (the AI stack cold-starts
// in tens of seconds). App.tsx owns the single /healthz poll and provides the
// derived boolean here; consumers read it with useBackendReady().
import { createContext, useContext, type ReactNode } from "react";

const BackendReadyContext = createContext<boolean>(false);

export function BackendReadyProvider({
  ready,
  children,
}: {
  ready: boolean;
  children: ReactNode;
}) {
  return (
    <BackendReadyContext.Provider value={ready}>
      {children}
    </BackendReadyContext.Provider>
  );
}

/** True once the sidecar has reported healthy at least once this session. */
export function useBackendReady(): boolean {
  return useContext(BackendReadyContext);
}
