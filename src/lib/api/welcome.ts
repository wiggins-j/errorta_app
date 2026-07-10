// F007 — welcome corpus client.
import { getJSON, postJSON } from "../api";
import type {
  WelcomeInstallResult,
  WelcomeOptionsResponse,
  WelcomeStatus,
} from "../../features/welcome/types";

export function listOptions(): Promise<WelcomeOptionsResponse> {
  return getJSON<WelcomeOptionsResponse>("/welcome/options");
}

export function getStatus(): Promise<WelcomeStatus> {
  return getJSON<WelcomeStatus>("/welcome/status");
}

export function install(): Promise<WelcomeInstallResult> {
  return postJSON<WelcomeInstallResult>("/welcome/install");
}
