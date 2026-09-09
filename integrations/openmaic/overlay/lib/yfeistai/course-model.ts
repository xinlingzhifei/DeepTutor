import { getModel, parseModelString } from "@/lib/ai/providers";
import { resolveModel } from "@/lib/server/resolve-model";
import { createOpenAI } from "@ai-sdk/openai";
import { readConfiguration } from "./provider-reader.cjs";

/** Resolve once per generation request; updates affect subsequent requests. */
export async function resolveCourseModel() {
  const config = readConfiguration();
  if (!config) {
    throw Object.assign(new Error("Configure the course model in Settings first."), {
      code: "MODEL_NOT_CONFIGURED",
    });
  }
  if (config.scope === "dedicated") {
    return resolveModel({ stage: "generate-classroom" });
  }
  const { providerId, modelId } = parseModelString(config.defaultModel);
  const credentials = config.providers[providerId];
  if (config.scope === "shared" && providerId === "openai") {
    // The settings catalog uses Chat Completions, including custom gateways.
    // Native model-name heuristics must not switch these requests to Responses.
    const model = createOpenAI({
      apiKey: credentials.apiKey,
      baseURL: credentials.baseUrl,
    }).chat(modelId);
    return { model, modelInfo: null, thinkingConfig: undefined };
  }
  const resolved = getModel({
    providerId,
    modelId,
    apiKey: credentials.apiKey,
    baseUrl: credentials.baseUrl,
  });
  return { ...resolved, thinkingConfig: undefined };
}
