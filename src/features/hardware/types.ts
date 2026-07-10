// F002 — shared TypeScript types for the hardware feature.
// Mirrors the JSON shape returned by the Python sidecar's
// /hardware/scan and /hardware/report endpoints.

export interface GpuInfo {
  vendor: string;
  model: string;
  vram_gb: number;
  driver: string | null;
  unified_memory: boolean;
  warning?: string;
}

export interface CpuInfo {
  model: string;
  cores: number;
  avx: boolean;
  avx2: boolean;
}

export interface OsInfo {
  name: string;
  version: string;
  arch: string;
}

export interface ModelTier {
  id: string;
  label: string;
  params_b: number;
  quant: string;
  vram_gb: number;
  install_gb: number;
  tok_s_low: number;
  tok_s_high: number;
  install_label: string;
  vram_label: string;
  tok_label: string;
  compatible: boolean;
  incompatible_reason: string | null;
}

export interface Recommendation {
  available_vram_gb: number;
  primary: ModelTier;
  faster: ModelTier | null;
  capable: ModelTier | null;
  incompatible: ModelTier[];
  all: ModelTier[];
  rationale: string;
  table_version: string | null;
}

export interface HardwareReport {
  scanned_at: string;
  gpu: GpuInfo;
  ram_gb: number;
  disk_free_gb: number;
  cpu: CpuInfo;
  os: OsInfo;
  recommendation: Recommendation;
}
