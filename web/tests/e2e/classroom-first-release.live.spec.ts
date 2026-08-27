import { expect, test, type Browser, type Page } from "@playwright/test";
import {
  ensureLiveCourseGenerationPolicy,
  LiveFixtureContext,
  loginLiveIdentity,
  provisionLiveFixture,
  type LiveApiRequestContext,
  type LiveEvidence,
  type LiveProvisionedFixture,
  type LiveTenantRecord,
} from "./support/classroom-first-release-live-fixture";
import {
  runLiveContentOperationsRecipe,
  runLiveStudentFullRecipe,
  runLiveStudentMicroRecipe,
  runLiveTeacherRecipe,
} from "./support/classroom-first-release-live-flows";

const LIVE_TEST_TIMEOUT_MS = 12 * 60_000;
const ACTION_TIMEOUT_MS = 30_000;

const TAILWIND_ROUTES = [
  "/login",
  "/home",
  "/knowledge",
  "/settings/appearance",
  "/settings/llm",
  "/space/learning",
] as const;
const TAILWIND_VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
] as const;
const TAILWIND_APPEARANCES = ["snow", "light", "dark", "glass"] as const;
const TAILWIND_CASES = TAILWIND_ROUTES.flatMap((route) =>
  TAILWIND_VIEWPORTS.flatMap((viewport) =>
    TAILWIND_APPEARANCES.map((appearance) => ({
      route,
      viewport,
      appearance,
    })),
  ),
);
const TAILWIND_CASE_KEYS = new Set(TAILWIND_CASES.map((visualCase) =>
  `${visualCase.route}\u0000${visualCase.viewport.name}\u0000${visualCase.appearance}`,
));
if (TAILWIND_CASES.length !== 48 || TAILWIND_CASE_KEYS.size !== 48) {
  throw new Error("live Tailwind matrix is incomplete");
}

type TailwindCase = (typeof TAILWIND_CASES)[number];
type TailwindAppearance = (typeof TAILWIND_APPEARANCES)[number];

const TAILWIND_THEME_LABELS: Record<TailwindAppearance, RegExp> = {
  snow: /^(?:Default|默认)$/,
  light: /^(?:Cream|奶油)$/,
  dark: /^(?:Dark|深色)$/,
  glass: /^(?:Glass|琉璃)$/,
};

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

function safeTenantOption(value: string): string {
  if (!/^[A-Za-z0-9_.:-]+$/.test(value)) {
    throw new Error("live visual tenant option is invalid");
  }
  return value;
}

async function selectLiveVisualTenant(
  page: Page,
  tenant: LiveTenantRecord,
): Promise<void> {
  const safeTenantId = safeTenantOption(tenant.tenantId);
  const label = page
    .locator("div[aria-label]:visible")
    .filter({ hasText: tenant.name });
  const action = page
    .locator("button:visible")
    .filter({ hasText: tenant.name });
  const select = page.locator(
    `select:visible:has(option[value="${safeTenantId}"])`,
  );

  await expect
    .poll(
      async () =>
        (await label.count()) +
        (await action.count()) +
        (await select.count()),
      { timeout: ACTION_TIMEOUT_MS },
    )
    .toBe(1);
  if ((await label.count()) === 1) return;
  if (
    (await select.count()) === 1 &&
    (await select.inputValue()) === tenant.tenantId
  ) {
    return;
  }

  const switched = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      new URL(response.url()).pathname === "/api/v1/tenants/active",
    { timeout: ACTION_TIMEOUT_MS },
  );
  if ((await action.count()) === 1) {
    await action.click();
  } else if ((await select.count()) === 1) {
    await select.selectOption(tenant.tenantId);
  } else {
    throw new Error("live visual tenant switcher is missing");
  }
  const response = await switched;
  if (response.status() !== 200) {
    throw new Error("live visual tenant selection failed");
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error("live visual tenant selection response is invalid");
  }
  if (
    payload === null ||
    typeof payload !== "object" ||
    Array.isArray(payload) ||
    (payload as Record<string, unknown>).active_tenant_id !== tenant.tenantId
  ) {
    throw new Error("live visual tenant selection response is invalid");
  }
  await expect
    .poll(async () => {
      if ((await label.count()) === 1) return true;
      return (
        (await select.count()) === 1 &&
        (await select.inputValue()) === tenant.tenantId
      );
    })
    .toBe(true);
}

async function assertLiveRouteLandmark(
  page: Page,
  route: (typeof TAILWIND_ROUTES)[number],
): Promise<void> {
  const pathname = new URL(page.url()).pathname.replace(/\/+$/, "") || "/";
  expect(pathname).toBe(route);
  if (route === "/login") {
    await expect(page.locator("form")).toBeVisible({
      timeout: ACTION_TIMEOUT_MS,
    });
    await expect(page.locator("#username")).toBeVisible();
    await expect(page.locator("#password")).toBeVisible();
    return;
  }

  const main = page.getByRole("main");
  await expect(main).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  if (route === "/home") {
    await expect(main.locator('textarea[maxlength="32000"]')).toBeVisible({
      timeout: ACTION_TIMEOUT_MS,
    });
    return;
  }
  const landmark =
    route === "/knowledge"
      ? /Knowledge Center|知识中心/
      : route === "/settings/appearance"
        ? /Appearance|外观/
        : route === "/settings/llm"
          ? /LLM/
          : /Mastery Path|精通之路/;
  await expect(
    main.getByRole("heading", { level: 1, name: landmark }),
  ).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  if (route === "/space/learning") {
    await expect(main.locator("aside .animate-spin")).toHaveCount(0, {
      timeout: ACTION_TIMEOUT_MS,
    });
  }
}

async function navigateToLiveSettingsRoute(
  page: Page,
  route: "/settings/appearance" | "/settings/llm",
): Promise<void> {
  const settingsRead = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname === "/api/v1/settings",
    { timeout: ACTION_TIMEOUT_MS },
  );
  const statusRead = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname === "/api/v1/system/status",
    { timeout: ACTION_TIMEOUT_MS },
  );
  await page.goto(route);
  const [settingsResponse, statusResponse] = await Promise.all([
    settingsRead,
    statusRead,
  ]);
  if (!settingsResponse.ok() || !statusResponse.ok()) {
    throw new Error("live visual settings read failed");
  }
  await assertLiveRouteLandmark(page, route);
}

async function selectLiveVisualTheme(
  page: Page,
  appearance: TailwindAppearance,
): Promise<void> {
  await navigateToLiveSettingsRoute(page, "/settings/appearance");
  const themeButton = page.getByRole("button", {
    name: TAILWIND_THEME_LABELS[appearance],
  });
  await expect(themeButton).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  if ((await themeButton.getAttribute("aria-pressed")) !== "true") {
    const persisted = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        new URL(response.url()).pathname === "/api/v1/settings/ui",
      { timeout: ACTION_TIMEOUT_MS },
    );
    await themeButton.click();
    const response = await persisted;
    if (!response.ok()) {
      throw new Error("live visual theme persistence failed");
    }
  }
  await expect(themeButton).toHaveAttribute("aria-pressed", "true");
  await expect
    .poll(() =>
      page.evaluate(() => localStorage.getItem("deeptutor-theme")),
    )
    .toBe(appearance);
}

async function runLiveTailwindCase(
  browser: Browser,
  baseUrl: string,
  fixture: LiveProvisionedFixture,
  visualCase: TailwindCase,
): Promise<void> {
  const identity = fixture.identities.find(
    (candidate) => candidate.role === "teacher",
  );
  if (!identity?.userId) throw new Error("live visual identity is incomplete");

  const context = await browser.newContext({
    baseURL: baseUrl,
    locale: "en-US",
    timezoneId: "UTC",
    viewport: { width: 1440, height: 900 },
    colorScheme:
      visualCase.appearance === "dark" || visualCase.appearance === "glass"
        ? "dark"
        : "light",
  });
  try {
    await context.addInitScript((appearance) => {
      if (window === window.top) {
        localStorage.setItem("deeptutor-theme", appearance);
      }
    }, visualCase.appearance);
    const page = await context.newPage();
    let consoleErrorCount = 0;
    let pageErrorCount = 0;
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrorCount += 1;
    });
    page.on("pageerror", () => {
      pageErrorCount += 1;
    });

    let routeReady = false;
    if (visualCase.route !== "/login") {
      await loginLiveIdentity(page, identity);
      await selectLiveVisualTenant(page, fixture.tenant);
      if (
        visualCase.route === "/settings/appearance" ||
        visualCase.route === "/settings/llm"
      ) {
        await selectLiveVisualTheme(page, visualCase.appearance);
        routeReady = visualCase.route === "/settings/appearance";
      }
    }
    await page.setViewportSize({
      width: visualCase.viewport.width,
      height: visualCase.viewport.height,
    });
    if (!routeReady) {
      if (visualCase.route === "/settings/llm") {
        await navigateToLiveSettingsRoute(page, visualCase.route);
      } else {
        await page.goto(visualCase.route);
      }
    }
    await assertLiveRouteLandmark(page, visualCase.route);
    await page.evaluate(async () => {
      await document.fonts.ready;
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      );
    });

    const environment = await page.evaluate(() => ({
      storedTheme: localStorage.getItem("deeptutor-theme"),
      classes: Array.from(document.documentElement.classList),
      viewportWidth: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
    }));
    expect(environment.storedTheme).toBe(visualCase.appearance);
    expect(environment.viewportWidth).toBe(visualCase.viewport.width);
    expect(environment.documentWidth).toBeLessThanOrEqual(
      environment.viewportWidth,
    );
    expect(environment.classes.includes("dark")).toBe(
      visualCase.appearance === "dark" || visualCase.appearance === "glass",
    );
    expect(environment.classes.includes("theme-glass")).toBe(
      visualCase.appearance === "glass",
    );
    expect(environment.classes.includes("theme-snow")).toBe(
      visualCase.appearance === "snow",
    );
    expect(consoleErrorCount).toBe(0);
    expect(pageErrorCount).toBe(0);
  } finally {
    await context.close();
  }
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

for (const visualCase of TAILWIND_CASES) {
  test(
    `[release-evidence:tailwind4_visual_matrix] route=${visualCase.route} viewport=${visualCase.viewport.name} appearance=${visualCase.appearance}`,
    async ({ browser, request }) => {
      const baseUrl = liveBaseUrl();
      const fixture = await provisionLiveFixture(
        liveFixtureContext(request, "tailwind4_visual_matrix"),
        { roles: ["teacher"] },
      );
      await runLiveTailwindCase(browser, baseUrl, fixture, visualCase);
    },
  );
}

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
