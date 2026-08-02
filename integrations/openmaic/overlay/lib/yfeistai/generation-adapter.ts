type ProviderCall = (
  systemPrompt: string,
  userPrompt: string,
) => Promise<string>;

interface OutlineGeneratorResult<Result> {
  success: boolean;
  data?: Result | null;
}

const LANGUAGE_MODEL_PROVIDER_METHODS = new Set<PropertyKey>([
  "doGenerate",
  "doStream",
]);

class SanitizedProviderFailure extends Error {
  constructor() {
    super("OpenMAIC provider request failed.");
    this.name = "SanitizedProviderFailure";
  }
}

function trackLanguageModelProviderFailures<LanguageModel>(
  languageModel: LanguageModel,
  onFailure: (error: unknown) => void,
): LanguageModel {
  if (
    languageModel === null ||
    (typeof languageModel !== "object" && typeof languageModel !== "function")
  ) {
    return languageModel;
  }
  const target = languageModel as object;
  const trackedMethods = new Map<PropertyKey, unknown>();
  return new Proxy(target, {
    get(currentTarget, property) {
      const value = Reflect.get(currentTarget, property, currentTarget);
      if (
        !LANGUAGE_MODEL_PROVIDER_METHODS.has(property) ||
        typeof value !== "function"
      ) {
        return value;
      }
      const existing = trackedMethods.get(property);
      if (existing) {
        return existing;
      }
      const tracked = async (...args: unknown[]) => {
        try {
          return await Reflect.apply(value, currentTarget, args);
        } catch (error) {
          onFailure(error);
          throw new SanitizedProviderFailure();
        }
      };
      trackedMethods.set(property, tracked);
      return tracked;
    },
  }) as unknown as LanguageModel;
}

export async function runOutlineRouteAdapter<Result>(options: {
  callProvider: ProviderCall;
  generate(
    callProvider: ProviderCall,
  ): Promise<OutlineGeneratorResult<Result>>;
}): Promise<Result> {
  let providerFailure: unknown;
  let providerFailed = false;
  const rememberProviderFailure = (error: unknown) => {
    providerFailed = true;
    providerFailure = error;
  };
  const trackedProviderCall: ProviderCall = async (...args) => {
    try {
      return await options.callProvider(...args);
    } catch (error) {
      rememberProviderFailure(error);
      throw new SanitizedProviderFailure();
    }
  };
  let generated: OutlineGeneratorResult<Result>;
  try {
    generated = await options.generate(trackedProviderCall);
  } catch (error) {
    if (providerFailed) {
      throw providerFailure;
    }
    throw error;
  }
  if (
    !generated.success ||
    generated.data === null ||
    generated.data === undefined
  ) {
    if (providerFailed) {
      throw providerFailure;
    }
    throw new Error("upstream outline generation failed");
  }
  return generated.data;
}

export async function runSceneRouteAdapter<Outline, LanguageModel, Result>(
  options: {
    outline: Outline;
    languageDirective: string;
    languageModel: LanguageModel;
    callProvider: ProviderCall;
    generate(
      outline: Outline,
      callProvider: ProviderCall,
      options: {
        languageDirective: string;
        languageModel: LanguageModel;
      },
    ): Promise<Result | null>;
  },
): Promise<Result | null> {
  let providerFailure: unknown;
  let providerFailed = false;
  const rememberProviderFailure = (error: unknown) => {
    providerFailed = true;
    providerFailure = error;
  };
  const trackedProviderCall: ProviderCall = async (...args) => {
    try {
      return await options.callProvider(...args);
    } catch (error) {
      rememberProviderFailure(error);
      throw new SanitizedProviderFailure();
    }
  };
  const trackedLanguageModel = trackLanguageModelProviderFailures(
    options.languageModel,
    rememberProviderFailure,
  );
  let generated: Result | null;
  try {
    generated = await options.generate(
      options.outline,
      trackedProviderCall,
      {
        languageDirective: options.languageDirective,
        languageModel: trackedLanguageModel,
      },
    );
  } catch (error) {
    if (providerFailed) {
      throw providerFailure;
    }
    throw error;
  }
  if (generated === null && providerFailed) {
    throw providerFailure;
  }
  return generated;
}
