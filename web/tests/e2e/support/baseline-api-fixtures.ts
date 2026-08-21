export const FIXED_NOW = Date.parse("2026-01-15T14:30:00.000Z");

export type StoredTheme = "snow" | "light" | "dark" | "glass";

const EMPTY_SERVICES = {
  embedding: {
    active_profile_id: null,
    active_model_id: null,
    profiles: [],
  },
  search: { active_profile_id: null, profiles: [] },
  tts: { active_profile_id: null, active_model_id: null, profiles: [] },
  stt: { active_profile_id: null, active_model_id: null, profiles: [] },
  imagegen: {
    active_profile_id: null,
    active_model_id: null,
    profiles: [],
  },
  videogen: {
    active_profile_id: null,
    active_model_id: null,
    profiles: [],
  },
} as const;

function settingsPayload(theme: StoredTheme) {
  return {
    ui: {
      theme,
      language: "zh",
      code_block_theme: "oneLight",
      code_block_show_line_numbers: false,
      code_block_wrap_long_lines: false,
    },
    catalog: {
      version: 1,
      services: {
        llm: {
          active_profile_id: "baseline-profile",
          active_model_id: "baseline-model",
          profiles: [
            {
              id: "baseline-profile",
              name: "Baseline OpenAI",
              binding: "openai",
              base_url: "http://127.0.0.1:8001/v1",
              api_key: "",
              api_version: "",
              extra_headers: "",
              proxy: "",
              models: [
                {
                  id: "baseline-model",
                  name: "Baseline GPT",
                  model: "gpt-4o-mini",
                  context_window: "128000",
                  context_window_source: "manual",
                },
              ],
            },
          ],
        },
        ...EMPTY_SERVICES,
      },
    },
    providers: {
      llm: [
        {
          value: "openai",
          label: "OpenAI",
          base_url: "https://api.openai.com/v1",
        },
      ],
      embedding: [],
      search: [],
      tts: [],
      stt: [],
      imagegen: [],
      videogen: [],
    },
  };
}

function masteryMapPayload() {
  return {
    book_id: "baseline-path",
    next: {
      action: "practice",
      knowledge_point_name: "Vector spaces",
      knowledge_point_type: "concept",
      status: "learning",
      mastery: 0.55,
      threshold: 0.8,
      reason: "Continue guided practice",
    },
    map: {
      counts: { mastered: 1, learning: 1, new: 1, total: 3 },
      due_reviews: 1,
      complete: false,
      modules: [
        {
          id: "linear-algebra",
          name: "Linear algebra foundations",
          order: 1,
          mastered: 1,
          total: 3,
          knowledge_points: [
            {
              id: "vectors",
              name: "Vectors",
              type: "concept",
              status: "mastered",
              mastery: 0.92,
            },
            {
              id: "vector-spaces",
              name: "Vector spaces",
              type: "concept",
              status: "learning",
              mastery: 0.55,
            },
            {
              id: "linear-maps",
              name: "Linear maps",
              type: "procedure",
              status: "new",
              mastery: 0,
            },
          ],
        },
      ],
    },
  };
}

export function apiPayload(
  pathname: string,
  theme: StoredTheme,
): unknown {
  switch (pathname) {
    case "/api/v1/auth/status":
      return {
        enabled: false,
        authenticated: false,
        role: "admin",
        is_admin: true,
        active_tenant_id: null,
        tenants: [],
      };
    case "/api/v1/auth/is_first_user":
      return { is_first_user: false };
    case "/api/v1/sessions":
      return { sessions: [] };
    case "/api/v1/dashboard/suggestions":
      return { suggestions: [], stale: false };
    case "/api/v1/settings/chat-attachments":
      return {
        effective: {
          max_file_bytes: 25 * 1024 * 1024,
          max_total_bytes: 50 * 1024 * 1024,
        },
      };
    case "/api/v1/knowledge/list":
      return { knowledge_bases: [] };
    case "/api/v1/knowledge/rag-providers":
      return {
        providers: [
          {
            id: "default",
            name: "Default RAG",
            description: "Built-in deterministic baseline provider",
            configured: true,
            modes: ["hybrid"],
            default_mode: "hybrid",
          },
        ],
      };
    case "/api/v1/knowledge/supported-file-types":
      return {
        extensions: [".md", ".pdf", ".txt"],
        accept: ".md,.pdf,.txt",
        max_file_size_bytes: 200 * 1024 * 1024,
      };
    case "/api/v1/tools":
      return { enabled_optional_tools: [] };
    case "/api/v1/settings/llm-options":
      return {
        active: {
          profile_id: "baseline-profile",
          model_id: "baseline-model",
        },
        options: [
          {
            profile_id: "baseline-profile",
            model_id: "baseline-model",
            profile_name: "Baseline OpenAI",
            model_name: "Baseline GPT",
            model: "gpt-4o-mini",
            provider: "openai",
            provider_label: "OpenAI",
            context_window: 128000,
            is_active_default: true,
          },
        ],
      };
    case "/api/v1/settings/providers/openai-codex/oauth/status":
      return {
        connection: "disconnected",
        operation_id: null,
        operation_state: null,
        authorize_url: null,
        expires_in: null,
        callback_port: null,
        callback_forward_port: null,
        redirect_uri: null,
        model_count: 0,
        catalog_source: null,
        catalog_fetched_at: null,
        active_model: null,
        models: [],
        activated: false,
        error_code: null,
      };
    case "/api/v1/subagents/settings":
      return { consult_budget: 3, backends: {} };
    case "/api/v1/settings":
      return settingsPayload(theme);
    case "/api/v1/system/status":
      return {
        backend: {
          status: "healthy",
          timestamp: "2026-01-15T14:30:00.000Z",
        },
        llm: { status: "ready", model: "gpt-4o-mini" },
        embeddings: { status: "not_configured" },
        search: { status: "not_configured" },
      };
    case "/api/v1/learning/progress":
      return {
        summaries: [
          {
            book_id: "baseline-path",
            name: "Linear Algebra",
            modules_count: 1,
            kp_count: 3,
            current_stage: "practice",
            avg_mastery_pct: 49,
            updated_at: FIXED_NOW / 1000,
          },
        ],
        errors: [],
      };
    case "/api/v1/learning/progress/baseline-path/map":
      return masteryMapPayload();
    case "/api/v1/learning/progress/baseline-path/events":
      return { book_id: "baseline-path", events: [] };
    default:
      return undefined;
  }
}
