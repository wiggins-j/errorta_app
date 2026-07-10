// F031-DEMO-CORPUS Task 4 — AIAR readiness banner.
//
// Honest, informational banner: when `/healthz` reports no connected AIAR
// runtime, surface a message telling the user that retrieval needs AIAR. The
// local `aiar_pin` is only an install detail and must not shadow a connected
// remote AIAR service.
import { useEffect, useState } from "react";
import { sidecarHealth } from "../../lib/api";

const BANNER_COPY =
  "Council retrieval needs AIAR. Complete onboarding to enable, or proceed for a Seed demonstration without retrieval.";

const ONBOARDING_HREF = "#/onboarding";

export interface AiarReadinessBannerProps {
  /** When provided, skips the network probe and uses the supplied value. */
  available?: boolean;
}

export default function AiarReadinessBanner({
  available: availableProp,
}: AiarReadinessBannerProps = {}) {
  const [available, setAvailable] = useState<boolean | undefined>(
    availableProp,
  );

  useEffect(() => {
    if (availableProp !== undefined) {
      setAvailable(availableProp);
      return;
    }
    let cancelled = false;
    sidecarHealth()
      .then((h) => {
        if (!cancelled) {
          setAvailable(h.aiar_runtime?.connected ?? h.aiar_pin?.available ?? false);
        }
      })
      .catch(() => {
        if (!cancelled) setAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, [availableProp]);

  // Hide while still resolving — avoid a flash of incorrect copy.
  if (available === undefined) return null;
  if (available === true) return null;

  return (
    <div
      className="council-status-banner warn aiar-readiness-banner"
      role="status"
      data-testid="aiar-readiness-banner"
    >
      <p>{BANNER_COPY}</p>
      <a href={ONBOARDING_HREF} className="aiar-onboarding-link">
        Open onboarding
      </a>
    </div>
  );
}
