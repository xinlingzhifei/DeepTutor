import { expect, test, type Page, type Route } from "@playwright/test";
import {
  apiPayload,
  FIXED_NOW,
  type StoredTheme,
} from "./support/baseline-api-fixtures";
import { installVisualStability } from "./support/visual-stability";
const LOCAL_WEB_PORT = process.env.PW_WEB_PORT || "3000";
const BASE_URL =
  process.env.WEB_BASE_URL || `http://127.0.0.1:${LOCAL_WEB_PORT}`;
const READY_TIMEOUT_MS = 30_000;

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
  await installVisualStability(page);
  await expect(page.locator(ready).first()).toBeVisible({
    timeout: READY_TIMEOUT_MS,
  });
  if (readyImage) {
    await expect(page.locator(`img[src="${readyImage}"]`).first()).toBeVisible({
      timeout: READY_TIMEOUT_MS,
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
      { timeout: READY_TIMEOUT_MS },
    );
  }
  await page.waitForFunction(
    () =>
      Array.from(document.images).every((image) => {
        const bounds = image.getBoundingClientRect();
        const style = getComputedStyle(image);
        const visible =
          bounds.width > 0 &&
          bounds.height > 0 &&
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          style.opacity !== "0";
        return !visible || (image.complete && image.naturalWidth > 0);
      }),
    undefined,
    { timeout: READY_TIMEOUT_MS },
  );
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("body")).toBeVisible();
  await expect(page.locator(".animate-spin:visible")).toHaveCount(0, {
    timeout: READY_TIMEOUT_MS,
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
              caret: "initial",
              fullPage: true,
              scale: "css",
            },
          );

          expect(unexpectedRequests).toEqual([]);
          expect(pageErrors).toEqual([]);
          expect(consoleErrors).toEqual([]);
        });
      }
    }
  }
});
