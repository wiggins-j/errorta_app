// F002 - summary of detected hardware.
import type { HardwareReport } from "./types";

export interface HardwareResultsPanelProps {
  report: HardwareReport;
}

export function HardwareResultsPanel({ report }: HardwareResultsPanelProps) {
  const { gpu, ram_gb, disk_free_gb, cpu, os } = report;
  return (
    <div className="feature-pane-note" aria-label="Detected hardware">
      <h3>Your hardware</h3>
      <ul style={{ listStyle: "none", padding: 0, margin: 0, lineHeight: 1.7 }}>
        <li>
          {gpu.model}
          {gpu.vram_gb > 0 ? ` · ${gpu.vram_gb} GB ${gpu.unified_memory ? "unified memory" : "VRAM"}` : ""}
        </li>
        <li>
          {ram_gb} GB RAM · {disk_free_gb} GB free disk
        </li>
        <li>
          {cpu.model} · {cpu.cores} cores
          {cpu.avx2 ? " · AVX2" : cpu.avx ? " · AVX" : ""}
        </li>
        <li>
          {os.name} {os.version} · {os.arch}
        </li>
        {gpu.warning ? <li style={{ color: "#c97a00" }}>Warning: {gpu.warning}</li> : null}
      </ul>
    </div>
  );
}
