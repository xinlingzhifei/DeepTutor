import { expect, test } from "vitest";
import { classifyProviderFailure } from "../../lib/yfeistai/provider-error";

test("missing course model has an actionable secret-free terminal error", () => {
  const failure = classifyProviderFailure({cause: {
    code: "MODEL_NOT_CONFIGURED", message: "secret-canary",
  }});
  expect(failure).toEqual({code: "model_not_configured",
    message: "Configure the course model in Settings first."});
});

test("hostile code getters cannot break provider failure classification", () => {
  expect(classifyProviderFailure({status: 429,
    get code() { throw new Error("secret-canary"); },
  })?.code).toBe("provider_429");
});
