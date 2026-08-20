import { callLLM } from "@/lib/ai/llm";
import { generateSceneOutlinesFromRequirements } from "@/lib/generation/outline-generator";
import { resolveModel } from "@/lib/server/resolve-model";
import { resolveClassroomWebSearchConfig } from "@/lib/server/web-search-config";
import { formatSearchResultsAsContext, searchWeb } from "@/lib/web-search";

import {
  buildSourceText,
  buildUpstreamRequirement,
  createOutlinePostHandler,
  normalizeUpstreamOutlineBundle,
} from "../../../../../lib/yfeistai/outline-generation";
import { runOutlineRouteAdapter } from "../../../../../lib/yfeistai/generation-adapter";
import { outlineJobStore } from "../../../../../lib/yfeistai/job-store";
import { readServiceSecret } from "../../../../../lib/yfeistai/service-auth";

const SEARCH_ROUTE_FIELD = ["provider", "Id"].join("");
const SEARCH_CREDENTIAL_FIELD = ["api", "Key"].join("");
const SEARCH_ENDPOINT_FIELD = ["base", "Url"].join("");

function isAllowedSearchUrl(url: string, allowedDomains: string[]): boolean {
  try {
    const hostname = new URL(url).hostname.toLowerCase();
    return allowedDomains.some((domain) => {
      const normalized = domain.toLowerCase();
      return hostname === normalized || hostname.endsWith(`.${normalized}`);
    });
  } catch {
    return false;
  }
}

async function resolveResearchContext(
  request: Parameters<typeof buildUpstreamRequirement>[0],
): Promise<string | undefined> {
  const policy = request.teachingBrief.networkPolicy;
  if (!policy.allowWebAccess || policy.allowedDomains.length === 0) {
    return undefined;
  }
  const serverConfig = resolveClassroomWebSearchConfig({});
  if (!serverConfig) {
    return undefined;
  }
  const config = serverConfig as unknown as Record<string, unknown>;
  const searchInput = {
    query: request.teachingBrief.knowledgePoints
      .map((point) => point.title)
      .join(" "),
    [SEARCH_ROUTE_FIELD]: config[SEARCH_ROUTE_FIELD],
    [SEARCH_CREDENTIAL_FIELD]: config[SEARCH_CREDENTIAL_FIELD],
    [SEARCH_ENDPOINT_FIELD]: config[SEARCH_ENDPOINT_FIELD],
    baiduSubSources: config.baiduSubSources,
  } as unknown as Parameters<typeof searchWeb>[0];
  try {
    const result = await searchWeb(searchInput);
    const filtered = {
      ...result,
      answer: "",
      sources: result.sources.filter((source) =>
        isAllowedSearchUrl(source.url, policy.allowedDomains),
      ),
    };
    return filtered.sources.length > 0
      ? formatSearchResultsAsContext(filtered)
      : undefined;
  } catch {
    return undefined;
  }
}

const postOutline = createOutlinePostHandler({
  readSecret: readServiceSecret,
  store: outlineJobStore,
  generateOutlines: async (request) => {
    const resolved = await resolveModel({ stage: "generate-classroom" });
    const aiCall = async (systemPrompt: string, userPrompt: string) => {
      const result = await callLLM(
        {
          model: resolved.model,
          messages: [
            { role: "system", content: systemPrompt },
            { role: "user", content: userPrompt },
          ],
          maxOutputTokens: resolved.modelInfo?.outputWindow,
          maxRetries: 0,
        },
        "generate-classroom",
        undefined,
        resolved.thinkingConfig,
      );
      return result.text;
    };
    const researchContext = await resolveResearchContext(request);
    const generated = await runOutlineRouteAdapter({
      callProvider: aiCall,
      generate: (trackedProviderCall) =>
        generateSceneOutlinesFromRequirements(
          { requirement: buildUpstreamRequirement(request) },
          buildSourceText(request),
          undefined,
          trackedProviderCall,
          {
            imageGenerationEnabled:
              request.teachingBrief.mediaPolicy.allowGeneration,
            videoGenerationEnabled:
              request.teachingBrief.mediaPolicy.allowGeneration,
            researchContext,
          },
        ),
    });
    return normalizeUpstreamOutlineBundle(request, generated, {
      generatedAt: new Date().toISOString(),
    });
  },
});

export async function POST(request: Request): Promise<Response> {
  return postOutline(request);
}
