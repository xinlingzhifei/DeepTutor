import {
  expect,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";

import {
  apiPayload,
  type StoredTheme,
} from "./baseline-api-fixtures";
import { assertOnlyClassroomInfrastructureWebSockets } from "./classroom-network-audit";
import { installVisualStability } from "./visual-stability";

const LOCAL_WEB_PORT = process.env.PW_WEB_PORT || "3000";
export const CLASSROOM_BASE_URL =
  process.env.WEB_BASE_URL || `http://127.0.0.1:${LOCAL_WEB_PORT}`;
const READY_TIMEOUT_MS = 60_000;

export const CLASSROOM_THEMES = [
  { name: "snow", stored: "snow" },
  { name: "cream", stored: "light" },
  { name: "dark", stored: "dark" },
  { name: "glass", stored: "glass" },
] as const;
export const CLASSROOM_SCENES = [
  "slide",
  "quiz",
  "interactive",
  "pbl",
] as const;

export type ClassroomVisualHost = "editor" | "player";
export type ClassroomVisualLanguage = "zh" | "en";
export type ClassroomVisualMotion = "normal" | "reduced";

export interface ClassroomVisualCase {
  host: ClassroomVisualHost;
  theme: (typeof CLASSROOM_THEMES)[number];
  scene: (typeof CLASSROOM_SCENES)[number];
  viewport: { name: "desktop" | "mobile"; width: number; height: number };
  language: ClassroomVisualLanguage;
  motion: ClassroomVisualMotion;
}

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
] as const;
const LANGUAGES = ["zh", "en"] as const;
const MOTIONS = ["normal", "reduced"] as const;
const HOSTS = ["editor", "player"] as const;
const SCENE_TITLES = {
  slide: "Energy transfer",
  quiz: "Check your model",
  interactive: "Build the pathway",
  pbl: "Design a thermal shelter",
} as const;
const SLIDE_BODY = "How does energy move?";
const QUIZ_BODY = "Which observation best supports energy transfer?";
const INTERACTIVE_BODY = "Build an energy pathway";
const PBL_BODY =
  "Design a compact shelter that keeps an ice sample stable for thirty minutes. Use measured evidence to justify each material choice.";

/**
 * Keep the required theme x scene surface exhaustive while assigning the
 * remaining dimensions as balanced covering factors. Each host gets eight
 * screenshots per viewport, language, and motion preference.
 */
export const CLASSROOM_VISUAL_CASES: readonly ClassroomVisualCase[] =
  HOSTS.flatMap((host, hostIndex) =>
    CLASSROOM_THEMES.flatMap((theme, themeIndex) =>
      CLASSROOM_SCENES.map((scene, sceneIndex) => {
        const p = hostIndex;
        const t0 = themeIndex & 1;
        const t1 = (themeIndex >> 1) & 1;
        const s0 = sceneIndex & 1;
        const s1 = (sceneIndex >> 1) & 1;
        return {
          host,
          theme,
          scene,
          viewport: VIEWPORTS[p ^ t0 ^ s0],
          language: LANGUAGES[p ^ t1 ^ s1],
          motion: MOTIONS[p ^ t0 ^ t1 ^ s0 ^ s1],
        };
      }),
    ),
  );

export function classroomVisualCasesFor(
  host: ClassroomVisualHost,
): readonly ClassroomVisualCase[] {
  return CLASSROOM_VISUAL_CASES.filter(visualCase => visualCase.host === host);
}

export interface ClassroomNetworkAudit {
  externalRequests: string[];
  unexpectedApiRequests: string[];
  consoleErrors: string[];
  pageErrors: string[];
  requestFailures: Array<{ error: string; method: string; url: string }>;
  httpErrors: Array<{ status: number; url: string }>;
  webSocketUrls: string[];
  requests: Array<{ method: string; url: string }>;
  responses: Array<{
    contentType: string | null;
    method: string;
    status: number;
    url: string;
  }>;
  exportCreates: Array<{
    body: unknown;
    headers: Record<string, string>;
  }>;
  exportStatusGets: string[];
}

function exportJob(format: string, status: "queued" | "succeeded") {
  const succeeded = status === "succeeded";
  return {
    job_id: "visual-export",
    job_kind: "export",
    phase: "export",
    status,
    progress_percent: succeeded ? 100 : 0,
    waiting_reason: null,
    cancellable: !succeeded,
    retryable: false,
    outline: null,
    error_category: null,
    error_code: null,
    retry_of_job_id: null,
    export_format: format,
    download_ready: succeeded,
  };
}

function json(route: Route, payload: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

export async function installClassroomNetworkAudit(
  page: Page,
  storedTheme: StoredTheme,
): Promise<ClassroomNetworkAudit> {
  const audit: ClassroomNetworkAudit = {
    externalRequests: [],
    unexpectedApiRequests: [],
    consoleErrors: [],
    pageErrors: [],
    requestFailures: [],
    httpErrors: [],
    webSocketUrls: [],
    requests: [],
    responses: [],
    exportCreates: [],
    exportStatusGets: [],
  };
  const baseOrigin = new URL(CLASSROOM_BASE_URL).origin;

  page.on("console", message => {
    if (message.type() === "error") audit.consoleErrors.push(message.text());
  });
  page.on("pageerror", error => audit.pageErrors.push(error.message));
  page.on("websocket", webSocket =>
    audit.webSocketUrls.push(webSocket.url()),
  );
  page.on("request", request => {
    audit.requests.push({ method: request.method(), url: request.url() });
  });
  page.on("requestfailed", request => {
    audit.requestFailures.push({
      error: request.failure()?.errorText ?? "request failed",
      method: request.method(),
      url: request.url(),
    });
  });
  page.on("response", response => {
    const entry = {
      contentType: response.headers()["content-type"] ?? null,
      method: response.request().method(),
      status: response.status(),
      url: response.url(),
    };
    audit.responses.push(entry);
    if (
      new URL(entry.url).origin === baseOrigin &&
      entry.status >= 400
    ) {
      audit.httpErrors.push({ status: entry.status, url: entry.url });
    }
  });

  await page.route("**/*", async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      await route.continue();
      return;
    }
    if (url.origin !== baseOrigin) {
      audit.externalRequests.push(`${request.method()} ${request.url()}`);
      await route.abort("blockedbyclient");
      return;
    }

    if (
      request.method() === "POST" &&
      url.pathname ===
        "/api/v1/classrooms/visual-classroom/draft/exports"
    ) {
      const body = request.postDataJSON() as unknown;
      audit.exportCreates.push({ body, headers: request.headers() });
      const format =
        typeof body === "object" && body !== null && "format" in body
          ? String(body.format)
          : "";
      await json(route, exportJob(format, "queued"));
      return;
    }
    if (
      request.method() === "GET" &&
      url.pathname === "/api/v1/classroom-exports/visual-export"
    ) {
      audit.exportStatusGets.push(url.pathname);
      await json(route, exportJob("pptx", "succeeded"));
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    const payload = apiPayload(url.pathname, storedTheme);
    if (request.method() === "GET" && payload !== undefined) {
      await json(route, payload);
      return;
    }

    audit.unexpectedApiRequests.push(
      `${request.method()} ${url.pathname}${url.search}`,
    );
    await route.fulfill({ status: 501, body: "unhandled visual fixture API" });
  });

  return audit;
}

async function installDeterministicEnvironment(
  page: Page,
  visualCase: ClassroomVisualCase,
) {
  await page.setViewportSize({
    width: visualCase.viewport.width,
    height: visualCase.viewport.height,
  });
  await page.emulateMedia({
    colorScheme:
      visualCase.theme.stored === "dark" ||
      visualCase.theme.stored === "glass"
        ? "dark"
        : "light",
    reducedMotion: visualCase.motion === "reduced" ? "reduce" : "no-preference",
  });
  await page.addInitScript(
    ({ language, theme }) => {
      if (window === window.top) {
        localStorage.setItem("deeptutor-theme", theme);
        localStorage.setItem("deeptutor-language", language);
        localStorage.setItem("deeptutor.sidebarCollapsed", "0");
      }
    },
    {
      language: visualCase.language,
      theme: visualCase.theme.stored,
    },
  );
}

export async function openClassroomVisualFixture(
  page: Page,
  visualCase: ClassroomVisualCase,
) {
  const { host } = visualCase;
  await installDeterministicEnvironment(page, visualCase);
  const params = new URLSearchParams({
    host,
    scene: visualCase.scene,
    theme: visualCase.theme.name,
  });
  const response = await page.goto(
    `/visual-baseline/classroom?${params.toString()}`,
    { waitUntil: "domcontentloaded" },
  );
  expect(response?.ok()).toBe(true);
  await installVisualStability(page);

  const root = page.getByTestId("classroom-visual-root");
  await expect(root).toBeVisible({ timeout: READY_TIMEOUT_MS });
  await expect(root).toHaveAttribute("data-hydrated", "true", {
    timeout: READY_TIMEOUT_MS,
  });
  await expect(root).toHaveAttribute("data-host", host);
  await expect(root).toHaveAttribute("data-scene", visualCase.scene);
  await expect(root).toHaveAttribute("data-theme", visualCase.theme.name);
  await page.waitForFunction(
    language => document.documentElement.lang === language,
    visualCase.language,
    { timeout: READY_TIMEOUT_MS },
  );
  if (host === "player") {
    await expect(
      root.locator("[data-playback-state]:not([data-playback-state='switching'])"),
    ).toBeVisible({ timeout: READY_TIMEOUT_MS });
  }
  await waitForClassroomSceneBody(root, visualCase);
  await expect(root.locator(".animate-spin:visible")).toHaveCount(0, {
    timeout: READY_TIMEOUT_MS,
  });
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(
    () =>
      new Promise<void>(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );

  const environment = await page.evaluate(() => ({
    language: document.documentElement.lang,
    storedTheme: localStorage.getItem("deeptutor-theme"),
    classes: Array.from(document.documentElement.classList),
    reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
    viewportWidth: innerWidth,
    documentWidth: document.documentElement.scrollWidth,
  }));
  expect(environment.language).toBe(visualCase.language);
  expect(environment.storedTheme).toBe(visualCase.theme.stored);
  expect(environment.classes.includes("dark")).toBe(
    visualCase.theme.stored === "dark" || visualCase.theme.stored === "glass",
  );
  expect(environment.classes.includes("theme-glass")).toBe(
    visualCase.theme.stored === "glass",
  );
  expect(environment.classes.includes("theme-snow")).toBe(
    visualCase.theme.stored === "snow",
  );
  expect(environment.reducedMotion).toBe(visualCase.motion === "reduced");
  expect(environment.documentWidth).toBeLessThanOrEqual(
    environment.viewportWidth,
  );

  if (host === "editor") {
    await expect(
      page.getByRole("button", {
        name:
          visualCase.language === "zh" ? "保存草稿" : "Save draft",
      }),
    ).toBeVisible();
  } else {
    await expect(
      page.getByRole("button", {
        name: visualCase.language === "zh" ? "播放" : "Play",
      }),
    ).toBeVisible();
  }
  return root;
}

export function classroomSnapshotName(
  visualCase: ClassroomVisualCase,
) {
  return [
    "classroom",
    visualCase.host,
    visualCase.theme.name,
    visualCase.scene,
    visualCase.viewport.name,
    visualCase.language,
    visualCase.motion,
  ].join("-") + ".png";
}

async function waitForClassroomSceneBody(
  root: Locator,
  visualCase: ClassroomVisualCase,
) {
  if (visualCase.host === "editor") {
    if (visualCase.scene === "slide") {
      await expect(root.locator("[data-marquee-surface]")).toBeVisible({
        timeout: READY_TIMEOUT_MS,
      });
      await expect(root.getByText(SLIDE_BODY, { exact: true })).toBeVisible();
      return;
    }
    if (visualCase.scene === "quiz") {
      const prompt = root.locator("fieldset textarea").first();
      await expect(prompt).toBeVisible({
        timeout: READY_TIMEOUT_MS,
      });
      await expect(prompt).toHaveValue(QUIZ_BODY);
      return;
    }
    if (visualCase.scene === "interactive") {
      const interactiveHtml = root.locator('textarea[spellcheck="false"]');
      await expect(interactiveHtml).toBeVisible({ timeout: READY_TIMEOUT_MS });
      await expect(interactiveHtml).toHaveValue(new RegExp(INTERACTIVE_BODY));
      return;
    }
    const scenario = root.locator("textarea.min-h-28");
    await expect(scenario).toBeVisible({
      timeout: READY_TIMEOUT_MS,
    });
    await expect(scenario).toHaveValue(PBL_BODY);
    return;
  }

  await expect(
    root.getByRole("heading", {
      name: SCENE_TITLES[visualCase.scene],
      exact: true,
    }),
  ).toBeVisible({ timeout: READY_TIMEOUT_MS });
  if (visualCase.scene === "slide") {
    await expect(root.getByText(SLIDE_BODY, { exact: true })).toBeVisible();
    return;
  }
  if (visualCase.scene === "quiz") {
    await expect(
      root.getByRole("heading", { name: QUIZ_BODY, exact: true }),
    ).toBeVisible();
    return;
  }
  if (visualCase.scene === "interactive") {
    const iframe = root.locator('iframe[title="Build the pathway"]');
    await expect(iframe).toBeVisible();
    const interactiveFrame = iframe.contentFrame();
    await expect(
      interactiveFrame.getByRole("heading", {
        name: INTERACTIVE_BODY,
        exact: true,
      }),
    ).toBeVisible({ timeout: READY_TIMEOUT_MS });
    await expect(
      interactiveFrame.getByRole("button", {
        name: "Complete pathway",
        exact: true,
      }),
    ).toBeVisible();
    return;
  }
  await expect(root.getByText(PBL_BODY, { exact: true })).toBeVisible();
}

export function expectCleanClassroomAudit(audit: ClassroomNetworkAudit) {
  expect(audit.externalRequests).toEqual([]);
  expect(audit.unexpectedApiRequests).toEqual([]);
  expect(audit.pageErrors).toEqual([]);
  expect(audit.consoleErrors).toEqual([]);
  expect(
    audit.requestFailures.filter(failure => {
      if (
        failure.method !== "HEAD" ||
        failure.error !== "net::ERR_ABORTED" ||
        new URL(failure.url).pathname !== "/vendor/maic-importer/index.js"
      ) {
        return true;
      }
      // Chromium reports Next's completed static-file HEAD probe as aborted
      // after exposing the successful headers. The importer test separately
      // requires the matching 2xx response and JavaScript content type.
      return !audit.responses.some(
        response =>
          response.method === failure.method &&
          response.url === failure.url &&
          response.status >= 200 &&
          response.status < 300,
      );
    }),
  ).toEqual([]);
  expect(audit.httpErrors).toEqual([]);
  assertOnlyClassroomInfrastructureWebSockets(
    audit.webSocketUrls,
    CLASSROOM_BASE_URL,
  );
}
