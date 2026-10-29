import { apiFetch, apiUrl } from "@/lib/api";
import { invalidateClientCache, withClientCache } from "@/lib/client-cache";
import type { LLMSelection } from "@/lib/unified-ws";

const LLM_OPTIONS_CACHE_KEY = "llm-options:list";
const DEFAULT_LLM_OPTIONS_TIMEOUT_MS = 30_000;

export interface LLMOption extends LLMSelection {
  profile_name: string;
  model_name: string;
  model: string;
  provider: string;
  /** Human-readable provider name from the registry ("OpenRouter"). */
  provider_label?: string;
  context_window?: number;
  is_active_default: boolean;
  /** Static required inputs are present; this does not test provider access. */
  configured?: boolean;
}

export interface LLMOptionsResponse {
  active: LLMSelection | null;
  options: LLMOption[];
}

export function llmSelectionKey(selection: LLMSelection | null | undefined) {
  if (!selection?.profile_id || !selection.model_id) return "";
  return `${selection.profile_id}:${selection.model_id}`;
}

export function sameLLMSelection(
  a: LLMSelection | null | undefined,
  b: LLMSelection | null | undefined,
) {
  return llmSelectionKey(a) === llmSelectionKey(b);
}

export function chatModelIssue(
  options: LLMOption[], selection: LLMSelection | null,
  activeDefault: LLMSelection | null, loading: boolean, error: boolean,
): string | null {
  if (loading) return "Loading model configuration…";
  if (error) return "Could not load models. Refresh to try again.";
  if (!options.length) return "No chat model is available. Configure a model or ask an administrator to grant access.";
  const selected = options.find(option => sameLLMSelection(option, selection ?? activeDefault));
  if (!selected) return "Select an available model before sending.";
  if (selected.configured === false) return "The selected model is missing its API address or credential. Update its settings before sending.";
  return null;
}

/** List the configured model profiles.
 *
 *  Cached so the many consumers that need the model list (composer, model
 *  picker, capability gate, partner forms) share one round-trip instead of
 *  each firing their own on mount. Editing a profile calls
 *  ``invalidateLLMOptionsCache``; pass ``force`` to bypass the cache. */
export async function listLLMOptions(options?: {
  force?: boolean;
  timeoutMs?: number;
}): Promise<LLMOptionsResponse> {
  return withClientCache<LLMOptionsResponse>(
    LLM_OPTIONS_CACHE_KEY,
    async () => {
      const controller = new AbortController();
      const timeout = setTimeout(
        () => controller.abort(),
        options?.timeoutMs ?? DEFAULT_LLM_OPTIONS_TIMEOUT_MS,
      );
      try {
        const response = await apiFetch(
          apiUrl("/api/v1/settings/llm-options"),
          {
            cache: "no-store",
            signal: controller.signal,
          },
        );
        if (!response.ok) {
          throw new Error(`Failed to load LLM options: ${response.status}`);
        }
        const data = (await response.json()) as LLMOptionsResponse;
        return {
          active: data.active ?? null,
          options: Array.isArray(data.options) ? data.options : [],
        };
      } finally {
        clearTimeout(timeout);
      }
    },
    { force: options?.force },
  );
}

export function invalidateLLMOptionsCache(): void {
  invalidateClientCache(LLM_OPTIONS_CACHE_KEY);
}
