import { expect, type Page, type Route } from "@playwright/test";

const SIDECAR_BASE = "http://127.0.0.1:8770";
const ISO_NOW = "2026-06-24T12:00:00Z";

export interface SidecarMockState {
  judgeRequests: Array<Record<string, unknown>>;
  rooms: Record<string, Record<string, unknown>>;
  roomOrder: string[];
  requestLog: string[];
  unhandledRequests: string[];
}

interface OpenAppOptions {
  activeFeature?: string;
  activeCorpus?: string;
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function text(route: Route, body: string, status = 200, contentType = "text/plain") {
  return route.fulfill({ status, contentType, body });
}

async function readJson(route: Route): Promise<Record<string, unknown>> {
  const raw = route.request().postData();
  if (!raw) return {};
  try {
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function baseRoom(id: string, name: string): Record<string, unknown> {
  return {
    format_version: 1,
    id,
    name,
    description: "",
    revision: 1,
    status_hint: "ready",
    updated_at: ISO_NOW,
    corpus_ids: ["demo-corpus"],
    members: [
      {
        id: "member-1",
        name: "Reviewer",
        role: "member",
        enabled: true,
        gateway_route_id: "local.fake.reviewer",
        provider_kind: "local",
        provider_display: "Fake",
        model_display: "reviewer",
        context_access: "prompt_only",
        transcript_access: "all_messages",
        turn_limits: {
          max_messages: 1,
          max_input_tokens: 8192,
          max_output_tokens: 1024,
          max_context_tokens: 8192,
        },
        generation: { temperature: 0.2, top_p: null, seed: null },
        system_prompt: "",
        metadata: {},
      },
    ],
    topology: {
      kind: "round_robin",
      max_rounds: 1,
      max_messages_per_member: 1,
      max_total_turns: 1,
      speaker_order: ["member-1"],
      stop_condition: null,
    },
    context_policy: {
      default_context_access: "prompt_only",
      default_transcript_access: "all_messages",
      allow_full_context: true,
      require_confirmation_for_remote_context: true,
      require_confirmation_for_full_context: false,
    },
    budget_policy: {
      max_total_tokens: null,
      max_wall_time_seconds: null,
      max_child_runs: 0,
    },
    finalization_policy: {
      mode: "last_message",
      writer_member_id: null,
      require_consensus: false,
    },
    metadata: {},
  };
}

function createState(): SidecarMockState {
  const room = baseRoom("demo-room", "Demo Room");
  return {
    judgeRequests: [],
    rooms: { "demo-room": room },
    roomOrder: ["demo-room"],
    requestLog: [],
    unhandledRequests: [],
  };
}

function roomSummary(room: Record<string, unknown>) {
  return {
    id: String(room.id),
    name: String(room.name),
    updated_at: String(room.updated_at ?? ISO_NOW),
    revision: Number(room.revision ?? 1),
    status_hint: String(room.status_hint ?? "ready"),
  };
}

function corpusCatalog() {
  return {
    source: "local",
    corpora: [
      {
        name: "demo-corpus",
        file_count: 1,
        ready_count: 1,
        status: "ready",
        source: "local",
        unit: "files",
        capabilities: {
          list_files: true,
          upload_files: true,
          folder_watch: true,
          refresh_preview: true,
          remote_ingest: false,
        },
      },
      {
        name: "remote-papers",
        file_count: 42,
        ready_count: 40,
        status: "indexing",
        source: "remote",
        unit: "chunks",
        capabilities: {
          list_files: false,
          upload_files: false,
          folder_watch: false,
          refresh_preview: false,
          remote_ingest: false,
        },
      },
    ],
  };
}

function health() {
  return {
    service: "errorta-sidecar",
    version: "0.1.0-alpha.0",
    now: ISO_NOW,
    aiar_available: true,
    aiar_version: "0.2.0",
    aiar_pin: { available: true, version: "0.2.0", source: "editable" },
    aiar_runtime: {
      kind: "service",
      runtime_kind: "service-aiar",
      display_name: "example-host",
      connected: true,
      backend_id: "example-host",
      active_model: "llama3.1:8b",
      active_model_ready: true,
      corpus_count: 2,
      capabilities: { answer: true, judge: true, corpora: true },
    },
    council: true,
    briefs: true,
    build: {
      commit: "e2e-mock-commit",
      commit_short: "e2emock",
      dirty: false,
    },
    features: {
      grounding: true,
      council: true,
      coding: true,
      briefs: true,
    },
    corpus_backend: {
      kind: "local",
      detail: { mode: "fixture" },
      retrieval_coordinated: true,
      backend_id: "local",
    },
  };
}

function watchStatus() {
  return {
    corpus: "demo-corpus",
    watching: false,
    alive: false,
    watched_path: null,
    deletion_policy: "mark_missing",
    type_filter: [".md", ".txt"],
    extra_ignores: [],
    last_scan_at: null,
    last_scan_ok: true,
    last_error: null,
    heartbeat_age_seconds: null,
    stale: false,
    paused: false,
    file_count: 1,
  };
}

async function routeSidecar(route: Route, state: SidecarMockState) {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;
  const method = request.method().toUpperCase();
  state.requestLog.push(`${method} ${path}`);

  if (path === "/healthz") return json(route, health());
  if (path === "/version") return json(route, { version: "0.1.0-alpha.0" });
  if (path === "/onboarding/corpora") {
    return json(route, {
      corpora: [
        { name: "demo-corpus", file_count: 1, ready_count: 1 },
        { name: "remote-papers", file_count: 42, ready_count: 40 },
      ],
    });
  }

  if (path === "/corpora") return json(route, corpusCatalog());
  if (path === "/corpus/formats") {
    return json(route, {
      extensions: [".md", ".txt", ".pdf", ".docx"],
      large_file_bytes: 104857600,
    });
  }
  if (path === "/corpus/events") {
    return text(route, ": ok\n\n", 200, "text/event-stream");
  }
  if (path === "/corpus/demo-corpus/files") {
    return json(route, {
      corpus: "demo-corpus",
      files: [
        {
          file_id: "file-1",
          original_path: "welcome.md",
          copied_path: "/tmp/welcome.md",
          sha256: "abc123",
          size_bytes: 2048,
          mime_ext: ".md",
          status: "ready",
          error: null,
          chunk_count: 4,
          chunk_ids: ["chunk-1", "chunk-2", "chunk-3", "chunk-4"],
          token_count: 512,
          ingested_at: ISO_NOW,
          progress: 1,
        },
      ],
      stats: {
        file_count: 1,
        chunk_count: 4,
        token_count: 512,
        disk_bytes: 2048,
      },
    });
  }
  if (path === "/corpus/demo-corpus/refresh-preview") {
    return json(route, {
      corpus: "demo-corpus",
      added: [],
      removed: [],
      updated: [],
      snapshot_at: ISO_NOW,
      partial: false,
    });
  }
  if (path.startsWith("/corpus/demo-corpus/") && method === "POST") {
    return json(route, { ok: true });
  }

  if (path === "/briefs") return json(route, []);
  if (path.startsWith("/briefs/")) {
    return json(route, {
      brief_id: "brief-1",
      markdown: "---\ntitle: Demo\ncorpus_name: demo-corpus\n---\n",
      manifest: {
        brief_id: "brief-1",
        title: "Demo",
        corpus_name: "demo-corpus",
        state: "DRAFT",
        parse_errors: [],
      },
    });
  }

  if (path === "/watch/status") return json(route, watchStatus());
  if (path.startsWith("/watch/")) return json(route, watchStatus());

  if (path === "/judge/model" && method === "GET") {
    return json(route, { judge_model: "llama3.1:8b", source: "default" });
  }
  if (path === "/judge/model" && method === "PUT") {
    const body = await readJson(route);
    return json(route, {
      judge_model: body.judge_model ?? "llama3.1:8b",
      source: body.judge_model ? "override" : "default",
    });
  }
  if (path === "/judge/preflight") {
    return json(route, {
      judge_model: "llama3.1:8b",
      judge_model_source: "default",
      aiar_available: true,
      aiar_connected: true,
      ollama_reachable: true,
      model_available: true,
      runtime_kind: "service-aiar",
      display_name: "example-host",
      backend_id: "example-host",
      answer_available: true,
      judge_available: true,
      active_model: "llama3.1:8b",
      active_model_ready: true,
      available_models: ["llama3.1:8b"],
      model_source: "service",
      capabilities: { model_set_active: true, ollama_pull: false },
    });
  }
  if (path === "/judge/metrics") {
    return json(route, {
      total: 1,
      pass_rate: 1,
      total_7d: 1,
      pass_rate_7d: 1,
      rating_counts: { pass: 1 },
      trend_7d: [
        { date: "2026-06-18", total: 0, pass: 0, pass_rate: null },
        { date: "2026-06-19", total: 0, pass: 0, pass_rate: null },
        { date: "2026-06-20", total: 0, pass: 0, pass_rate: null },
        { date: "2026-06-21", total: 0, pass: 0, pass_rate: null },
        { date: "2026-06-22", total: 0, pass: 0, pass_rate: null },
        { date: "2026-06-23", total: 0, pass: 0, pass_rate: null },
        { date: "2026-06-24", total: 1, pass: 1, pass_rate: 1 },
      ],
      most_corrected_prompts: [],
      latency_histogram: {
        buckets: [{ label: "0-1s", count: 1 }],
        p50_ms: 420,
        p95_ms: 420,
        p99_ms: 420,
      },
      log_path: "/tmp/errorta/judge-metrics.jsonl",
    });
  }
  if (path === "/judge/verdict" && method === "POST") {
    const body = await readJson(route);
    state.judgeRequests.push(body);
    const prompt = String(body.prompt ?? "");
    return json(route, {
      id: "verdict-1",
      prompt,
      answer: "AIAR says the demo corpus describes Errorta as a local AI that checks its own answers.",
      verdict: {
        rating: "pass",
        reason: "The answer is grounded in the selected demo corpus.",
        failure_tags: [],
        confidence: 0.92,
        latency_ms: 420,
      },
      judge_model: body.judge_model ?? "llama3.1:8b",
      prompt_signature: "sig-demo",
      grounding_match: { kind: "similar", similarity: 0.91 },
      prior_correction: null,
      grounded: true,
      reground_applied: false,
      rag_enabled: true,
      latency: 0.42,
    });
  }
  if (path === "/judge/prior-verdicts") {
    return json(route, {
      signature: url.searchParams.get("signature") ?? "sig-demo",
      priors: [
        {
          verdict: {
            rating: "partial",
            reason: "Earlier answer missed the local-first framing.",
            failure_tags: ["missing_context"],
            confidence: 0.71,
            latency_ms: 510,
          },
          judge_model: "llama3.1:8b",
          created_at: "2026-06-23T12:00:00Z",
        },
      ],
    });
  }
  if (path === "/judge/correction-draft") {
    return json(route, { draft: "Preserve the local-first framing." });
  }
  if (path === "/judge/accept") {
    return json(route, {
      id: "verdict-1",
      prompt: "demo",
      answer: "demo",
      correction: null,
      verdict: { rating: "pass" },
      grounding_recorded: true,
      created_at: ISO_NOW,
    });
  }
  if (path === "/judge/replay") return json(route, []);

  if (path === "/council/rooms" && method === "GET") {
    return json(route, { rooms: state.roomOrder.map((id) => roomSummary(state.rooms[id])) });
  }
  if (path === "/council/rooms" && method === "POST") {
    const body = await readJson(route);
    const id = `room-${state.roomOrder.length + 1}`;
    const room = {
      ...baseRoom(id, String(body.name ?? "New room")),
      ...body,
      id,
      revision: 1,
      updated_at: ISO_NOW,
    };
    state.rooms[id] = room;
    state.roomOrder.push(id);
    return json(route, {
      room,
      validation: { status: "draft", errors: [] },
    });
  }
  if (path.startsWith("/council/rooms/")) {
    const roomId = decodeURIComponent(path.split("/").pop() ?? "");
    const room = state.rooms[roomId] ?? baseRoom(roomId, "New room");
    if (method === "GET") {
      return json(route, {
        room,
        validation: { status: "ready", errors: [] },
      });
    }
    if (method === "PUT") {
      const body = await readJson(route);
      const next = {
        ...room,
        ...(body.room as Record<string, unknown> | undefined),
        revision: Number(room.revision ?? 1) + 1,
        updated_at: ISO_NOW,
      };
      state.rooms[roomId] = next;
      return json(route, {
        room: next,
        validation: { status: "ready", errors: [] },
      });
    }
    if (method === "DELETE") return json(route, { deleted: true });
  }
  if (path === "/council/runs") {
    return json(route, {
      run: {
        id: "run-1",
        room_id: "demo-room",
        status: "completed",
        prompt: "demo",
        terminal_reason: "done",
      },
      events: [],
    });
  }

  if (path === "/gateway/providers") {
    return json(route, {
      providers: [
        {
          provider_class: "local",
          display_name: "Fake",
          configured: true,
          connected: true,
        },
      ],
    });
  }
  if (path === "/gateway/routes") {
    return json(route, {
      provider_class: url.searchParams.get("provider") ?? "local",
      routes: [
        {
          route_id: "local.fake.reviewer",
          label: "Fake reviewer",
          family: "fake",
          provider_class: "local",
        },
      ],
    });
  }
  if (path === "/provider-keys") return json(route, { providers: [], custom: [] });
  if (path === "/settings") return json(route, { log_level: "INFO" });
  if (path === "/settings/mobile-connector") {
    return json(route, {
      enabled: false,
      bind_host: "127.0.0.1",
      port: 8781,
      pairing_enabled: false,
      devices: [],
    });
  }
  if (path === "/settings/mobile-connector/lan-addresses") {
    return json(route, { addresses: ["127.0.0.1"] });
  }
  if (path === "/api/auth/pairs") return json(route, { pairs: [] });
  if (path === "/api/auth/tokens") return json(route, { tokens: [] });
  if (path === "/aiar/status" || path === "/aiar/connection") {
    return json(route, {
      connected: true,
      kind: "service",
      runtime_kind: "service-aiar",
      display_name: "example-host",
      base_url: "https://example-host.example",
      active_model: "llama3.1:8b",
      active_model_ready: true,
      capabilities: { answer: true, judge: true, corpora: true },
    });
  }
  if (path === "/settings/remote-aiar") {
    return json(route, {
      enabled: false,
      host: "",
      user: "",
      port: 22,
      tunnel_local_port: null,
      status: "disabled",
    });
  }
  if (path === "/hardware/report" || path === "/hardware/scan") {
    return json(route, {
      cpu: { brand: "E2E CPU", cores_logical: 8, cores_physical: 4 },
      memory: { total_gb: 16 },
      gpu: [],
      recommendation: {
        tier: "local",
        model: "llama3.1:8b",
        rationale: "Mocked for browser e2e.",
      },
    });
  }
  if (path === "/diagnostics/log-tail") return json(route, { lines: [] });
  if (path === "/diagnostics/log-stream") {
    return text(route, ": ok\n\n", 200, "text/event-stream");
  }

  state.unhandledRequests.push(`${method} ${path}`);
  return json(route, { error: "unhandled_e2e_sidecar_route", method, path }, 501);
}

export async function installSidecarMock(page: Page): Promise<SidecarMockState> {
  const state = createState();
  await page.route(`${SIDECAR_BASE}/**`, (route) => routeSidecar(route, state));
  return state;
}

export async function seedBrowserState(
  page: Page,
  options: OpenAppOptions = {},
) {
  const activeFeature = options.activeFeature ?? "judge";
  const activeCorpus = options.activeCorpus ?? "demo-corpus";
  await page.addInitScript(
    ({ feature, corpus }) => {
      localStorage.setItem("errorta.onboarding.complete", "1");
      localStorage.setItem("errorta.activeFeature", feature);
      localStorage.setItem("errorta.knowledge.activeCorpus", corpus);

      class MockEventSource extends EventTarget {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSED = 2;
        readonly url: string;
        readyState = MockEventSource.OPEN;
        onmessage: ((event: MessageEvent) => void) | null = null;
        onerror: ((event: Event) => void) | null = null;
        onopen: ((event: Event) => void) | null = null;

        constructor(url: string) {
          super();
          this.url = url;
          setTimeout(() => this.onopen?.(new Event("open")), 0);
        }

        close() {
          this.readyState = MockEventSource.CLOSED;
        }
      }

      Object.defineProperty(window, "EventSource", {
        configurable: true,
        value: MockEventSource,
      });
    },
    { feature: activeFeature, corpus: activeCorpus },
  );
}

export async function openApp(
  page: Page,
  options: OpenAppOptions = {},
): Promise<SidecarMockState> {
  const state = await installSidecarMock(page);
  await seedBrowserState(page, options);
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Feature navigation" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Judge" })).toBeVisible();
  return state;
}

export function expectNoUnhandledSidecarRequests(state: SidecarMockState) {
  expect(state.unhandledRequests).toEqual([]);
}
