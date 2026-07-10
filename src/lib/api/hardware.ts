// F002 — hardware scan + model recommendation client.
import { getJSON, postJSON } from "../api";
import type { HardwareReport } from "../../features/hardware/types";

export function scan(): Promise<HardwareReport> {
  return postJSON<HardwareReport>("/hardware/scan");
}

export function report(): Promise<HardwareReport> {
  return getJSON<HardwareReport>("/hardware/report");
}
