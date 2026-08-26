import { test } from "@playwright/test";
import {
  ensureLiveCourseGenerationPolicy,
  LiveFixtureContext,
  provisionLiveFixture,
  type LiveApiRequestContext,
  type LiveEvidence,
} from "./support/classroom-first-release-live-fixture";
import {
  runLiveContentOperationsRecipe,
  runLiveStudentFullRecipe,
  runLiveStudentMicroRecipe,
  runLiveTeacherRecipe,
} from "./support/classroom-first-release-live-flows";

const LIVE_TEST_TIMEOUT_MS = 12 * 60_000;

type PublicEnvironmentName =
  | "YFEISTAI_RELEASE_RUN_ID"
  | "YFEISTAI_ENVIRONMENT_ID"
  | "YFEISTAI_EVIDENCE"
  | "WEB_BASE_URL";

function requiredPublicEnvironment(name: PublicEnvironmentName): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error("live browser environment is incomplete");
  return value;
}

function requiredFixtureToken(): string {
  const value = process.env.YFEISTAI_LIVE_FIXTURE_TOKEN?.trim();
  if (!value) throw new Error("live browser environment is incomplete");
  return value;
}

function liveBaseUrl(): string {
  const value = requiredPublicEnvironment("WEB_BASE_URL");
  try {
    const parsed = new URL(value);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password
    ) {
      throw new Error("invalid");
    }
  } catch {
    throw new Error("live browser base URL is invalid");
  }
  return value;
}

function liveFixtureContext(
  request: LiveApiRequestContext,
  evidence: LiveEvidence,
): LiveFixtureContext {
  if (requiredPublicEnvironment("YFEISTAI_EVIDENCE") !== evidence) {
    throw new Error("live browser evidence selection is invalid");
  }
  return new LiveFixtureContext({
    request,
    adminToken: requiredFixtureToken(),
    releaseRun: requiredPublicEnvironment("YFEISTAI_RELEASE_RUN_ID"),
    environment: requiredPublicEnvironment("YFEISTAI_ENVIRONMENT_ID"),
    evidence,
  });
}

test.describe.configure({ mode: "serial", timeout: LIVE_TEST_TIMEOUT_MS });

test(
  "[release-evidence:teacher_flow] prepares and submits a real classroom",
  async ({ browser, request }) => {
    const baseUrl = liveBaseUrl();
    const fixture = await provisionLiveFixture(
      liveFixtureContext(request, "teacher_flow"),
      {
        roles: ["teacher"],
        catalog: { controlledSource: true, enrollRoles: [] },
      },
    );
    await runLiveTeacherRecipe(browser, baseUrl, fixture);
  },
);

test(
  "[release-evidence:student_micro_flow] generates and opens a real micro classroom",
  async ({ browser, request }) => {
    const baseUrl = liveBaseUrl();
    const fixtureContext = liveFixtureContext(request, "student_micro_flow");
    const fixture = await provisionLiveFixture(fixtureContext, {
      roles: ["student"],
      catalog: { controlledSource: false, enrollRoles: ["student"] },
    });
    if (!fixture.catalog) throw new Error("live student catalog is incomplete");
    await ensureLiveCourseGenerationPolicy(
      fixtureContext,
      fixture.catalog.course,
    );
    await runLiveStudentMicroRecipe(browser, baseUrl, fixture);
  },
);

test(
  "[release-evidence:student_full_flow] edits confirms and opens a real full classroom",
  async ({ browser, request }) => {
    const baseUrl = liveBaseUrl();
    const fixtureContext = liveFixtureContext(request, "student_full_flow");
    const fixture = await provisionLiveFixture(fixtureContext, {
      roles: ["student"],
      catalog: { controlledSource: false, enrollRoles: ["student"] },
    });
    if (!fixture.catalog) throw new Error("live student catalog is incomplete");
    await ensureLiveCourseGenerationPolicy(
      fixtureContext,
      fixture.catalog.course,
    );
    await runLiveStudentFullRecipe(browser, baseUrl, fixture);
  },
);

test(
  "[release-evidence:content_operations_flow] separates author reviewer and publisher",
  async ({ browser, request }) => {
    const baseUrl = liveBaseUrl();
    const fixture = await provisionLiveFixture(
      liveFixtureContext(request, "content_operations_flow"),
      {
        roles: ["author", "reviewer", "publisher"],
        catalog: { controlledSource: true, enrollRoles: [] },
      },
    );
    await runLiveContentOperationsRecipe(browser, baseUrl, fixture);
  },
);
