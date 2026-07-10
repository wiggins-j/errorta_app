// Per-feature API namespaces. Feature agents fill in the individual modules.
// Consumers do: `import { hardwareApi } from "@/lib/api";` (or relative path).

export * as hardwareApi from "./hardware";
export * as ollamaApi from "./ollama";
export * as corpusApi from "./corpus";
export * as watchApi from "./watch";
export * as aiarConnectionApi from "./aiarConnection";
export * as authApi from "./auth";
export * as shellApi from "./shell";
export * as welcomeApi from "./welcome";
export * as briefsApi from "./briefs";
export * as residencyApi from "./residency";
export * as settingsApi from "./settings";
export * as mobileConnectorApi from "./mobileConnector";
export * as servicesApi from "./services";

// Re-export shared helpers and the canonical health probe so callers can
// import everything from this barrel.
export {
  SIDECAR_BASE,
  DEFAULT_SIDECAR_BASE,
  fetchJSON,
  getJSON,
  postJSON,
  putJSON,
  deleteJSON,
  sidecarHealth,
  type SidecarHealth,
} from "../api";
