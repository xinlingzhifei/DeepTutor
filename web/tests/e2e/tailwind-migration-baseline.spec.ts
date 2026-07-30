import { expect, test, type Page, type Route } from "@playwright/test";

const FIXED_NOW = Date.parse("2026-01-15T14:30:00.000Z");
const BASE_URL =
  process.env.WEB_BASE_URL ||
  process.env.NEXT_PUBLIC_API_BASE ||
  "http://localhost:3000";
const STABILITY_CSS = `
  *, *::before, *::after {
    animation-delay: 0s !important;
    animation-duration: 0s !important;
    animation-iteration-count: 1 !important;
    caret-color: transparent !important;
    scroll-behavior: auto !important;
    transition: none !important;
  }

  nextjs-portal {
    display: none !important;
  }
`;

const ROUTES = [
  { path: "/login", snapshot: "login", ready: "form", readyImage: null },
  {
    path: "/home",
    snapshot: "home",
    ready: 'img[src="/provider-icons/openai.svg"]',
    readyImage: "/provider-icons/openai.svg",
  },
  {
    path: "/knowledge",
    snapshot: "knowledge",
    ready: "text=Default RAG",
    readyImage: null,
  },
  {
    path: "/settings/appearance",
    snapshot: "settings-appearance",
    ready: ".md-code-block__code",
    readyImage: null,
  },
  {
    path: "/settings/llm",
    snapshot: "settings-llm",
    ready: 'img[src="/provider-icons/openai.svg"]',
    readyImage: "/provider-icons/openai.svg",
  },
  {
    path: "/space/learning",
    snapshot: "space-learning",
    ready: "text=Linear Algebra",
    readyImage: null,
  },
] as const;

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
] as const;

const THEMES = [
  { name: "snow", stored: "snow" },
  { name: "cream", stored: "light" },
  { name: "dark", stored: "dark" },
  { name: "glass", stored: "glass" },
] as const;

type StoredTheme = (typeof THEMES)[number]["stored"];

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

function apiPayload(pathname: string, theme: StoredTheme): unknown {
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
    default:
      return undefined;
  }
}

async function installDeterministicEnvironment(
  page: Page,
  storedTheme: StoredTheme,
) {
  await page.emulateMedia({
    colorScheme:
      storedTheme === "dark" || storedTheme === "glass" ? "dark" : "light",
    reducedMotion: "reduce",
  });

  await page.addInitScript(
    ({ fixedNow, theme }) => {
      const NativeDate = window.Date;
      const FrozenDate = function (
        this: unknown,
        ...args: unknown[]
      ): string | Date {
        if (!new.target) return new NativeDate(fixedNow).toString();
        return Reflect.construct(
          NativeDate,
          args.length ? args : [fixedNow],
          new.target,
        ) as Date;
      } as unknown as DateConstructor;

      Object.setPrototypeOf(FrozenDate, NativeDate);
      Object.defineProperty(FrozenDate, "prototype", {
        value: NativeDate.prototype,
      });
      FrozenDate.now = () => fixedNow;
      FrozenDate.parse = NativeDate.parse;
      FrozenDate.UTC = NativeDate.UTC;
      Object.defineProperty(window, "Date", { value: FrozenDate });
      Object.defineProperty(window.Math, "random", {
        value: () => 0,
      });

      localStorage.setItem("deeptutor-theme", theme);
      localStorage.setItem("deeptutor-language", "zh");
      localStorage.setItem("deeptutor.sidebarCollapsed", "0");
      localStorage.setItem("deeptutor.code-block-theme", "oneLight");
      localStorage.setItem("deeptutor.code-block-show-line-numbers", "false");
      localStorage.setItem("deeptutor.code-block-wrap-long-lines", "false");
    },
    { fixedNow: FIXED_NOW, theme: storedTheme },
  );
}

async function mockBaselineApis(
  page: Page,
  storedTheme: StoredTheme,
  unexpectedRequests: string[],
) {
  const baseOrigin = new URL(BASE_URL).origin;

  await page.route("**/*", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.protocol !== "http:" && url.protocol !== "https:") {
      await route.continue();
      return;
    }

    if (url.origin !== baseOrigin) {
      unexpectedRequests.push(`external ${request.method()} ${request.url()}`);
      await route.abort("blockedbyclient");
      return;
    }

    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }

    const payload = apiPayload(url.pathname, storedTheme);
    if (request.method() !== "GET" || payload === undefined) {
      unexpectedRequests.push(
        `unhandled ${request.method()} ${url.pathname}${url.search}`,
      );
      await route.abort("blockedbyclient");
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    });
  });
}

function normalizePathname(pathname: string) {
  return pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
}

async function waitForStablePage(
  page: Page,
  ready: string,
  readyImage: string | null,
) {
  await page.waitForLoadState("domcontentloaded");
  await page.waitForFunction(() => document.documentElement.lang === "zh");
  await page.addStyleTag({ content: STABILITY_CSS });
  await expect(page.locator(ready).first()).toBeVisible({ timeout: 15_000 });
  if (readyImage) {
    await expect(page.locator(`img[src="${readyImage}"]`).first()).toBeVisible({
      timeout: 15_000,
    });
    await page.waitForFunction(
      (src) =>
        Array.from(document.images).some(
          (image) =>
            image.getAttribute("src") === src &&
            image.complete &&
            image.naturalWidth > 0,
        ),
      readyImage,
      { timeout: 15_000 },
    );
  }
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("body")).toBeVisible();
  await expect(page.locator(".animate-spin:visible")).toHaveCount(0, {
    timeout: 15_000,
  });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
}

test.use({
  baseURL: BASE_URL,
  channel: "chromium",
  locale: "en-US",
  timezoneId: "UTC",
});
test.describe.configure({ mode: "serial" });

test.describe("Tailwind 3 migration visual baseline", () => {
  for (const routeTarget of ROUTES) {
    for (const viewport of VIEWPORTS) {
      for (const theme of THEMES) {
        test(`${routeTarget.snapshot} / ${viewport.name} / ${theme.name}`, async ({
          page,
        }) => {
          test.setTimeout(60_000);

          const consoleErrors: string[] = [];
          const pageErrors: string[] = [];
          const unexpectedRequests: string[] = [];

          page.on("console", (message) => {
            if (message.type() === "error") {
              consoleErrors.push(message.text());
            }
          });
          page.on("pageerror", (error) => pageErrors.push(error.message));

          await page.setViewportSize({
            width: viewport.width,
            height: viewport.height,
          });
          await installDeterministicEnvironment(page, theme.stored);
          await mockBaselineApis(page, theme.stored, unexpectedRequests);

          const response = await page.goto(routeTarget.path, {
            waitUntil: "domcontentloaded",
          });
          expect(response?.ok()).toBe(true);
          await waitForStablePage(
            page,
            routeTarget.ready,
            routeTarget.readyImage,
          );

          expect(normalizePathname(new URL(page.url()).pathname)).toBe(
            normalizePathname(routeTarget.path),
          );
          const environment = await page.evaluate(() => ({
            now: Date.now(),
            iso: new Date().toISOString(),
            storedTheme: localStorage.getItem("deeptutor-theme"),
            classes: Array.from(document.documentElement.classList),
          }));
          expect(environment.now).toBe(FIXED_NOW);
          expect(environment.iso).toBe("2026-01-15T14:30:00.000Z");
          expect(environment.storedTheme).toBe(theme.stored);
          expect(environment.classes.includes("dark")).toBe(
            theme.stored === "dark" || theme.stored === "glass",
          );
          expect(environment.classes.includes("theme-glass")).toBe(
            theme.stored === "glass",
          );
          expect(environment.classes.includes("theme-snow")).toBe(
            theme.stored === "snow",
          );

          expect(unexpectedRequests).toEqual([]);
          expect(pageErrors).toEqual([]);
          expect(consoleErrors).toEqual([]);

          await expect(page).toHaveScreenshot(
            `tailwind-v3-${routeTarget.snapshot}-${viewport.name}-${theme.name}.png`,
            {
              animations: "disabled",
              caret: "hide",
              fullPage: true,
              scale: "css",
            },
          );
        });
      }
    }
  }
});
