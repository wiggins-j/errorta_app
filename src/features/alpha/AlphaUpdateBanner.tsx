// F-DIST-01 slice 8 — non-blocking "update available" banner for a SOFT build
// EOL. The *required* case is handled by the full-window LockScreen; this is the
// gentle nudge that leaves the app fully usable. Renders nothing unless the gate
// is on and the server flagged this build as (softly) retired.

import type { AlphaStatus } from "../../lib/api/alpha";
import { safeUpdateUrl } from "./safeUpdateUrl";
import "./alphaUpdate.css";

export default function AlphaUpdateBanner({ status }: { status: AlphaStatus | null }) {
  if (!status || status.locked || !status.buildEol) return null;
  const updateUrl = safeUpdateUrl(status.updateUrl);
  return (
    <div className="alpha-update-banner" role="status">
      <span className="alpha-update-text">A newer Errorta alpha build is available.</span>
      {updateUrl && (
        <a
          className="alpha-update-link"
          href={updateUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          Get the update
        </a>
      )}
    </div>
  );
}
