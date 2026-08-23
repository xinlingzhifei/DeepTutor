import { chromium } from "@playwright/test";
import {
  fork,
  spawnSync,
  type ChildProcess,
} from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import { apiPayload } from "./baseline-api-fixtures";

const WEB_ROOT = path.resolve(__dirname, "../../..");
const LOCAL_WEB_PORT = process.env.PW_WEB_PORT || "3000";
const LOCAL_WEB_BASE_URL = `http://127.0.0.1:${LOCAL_WEB_PORT}`;
const LOCAL_SERVER_URL = `http://127.0.0.1:${LOCAL_WEB_PORT}/login`;
const WARMUP_TIMEOUT_MS = 6 * 60 * 1000;
const MANAGED_HEAP_LIMIT_MB = 4096;
const BASELINE_ROUTES = [
  "/login",
  "/home",
  "/knowledge",
  "/settings/appearance",
  "/settings/llm",
  "/space/learning",
  "/visual-baseline/classroom?host=editor&scene=slide&theme=snow",
] as const;
const SERVER_ENTRYPOINT = path.join(__dirname, "managed-next-server.mjs");
const IMPORTER_SYNC_ENTRYPOINT = path.join(
  WEB_ROOT,
  "scripts",
  "sync-openmaic-importer.mjs",
);
const FONT_MOCKS_PATH = path.join(__dirname, "google-font-mocks.cjs");
const FONT_FIXTURES: ReadonlyArray<{
  path: string;
  sha256: string;
  normalizeLineEndings?: boolean;
}> = [
  {
    path: FONT_MOCKS_PATH,
    sha256: "70317cf519c126537f3e14f591cdc4bdbb3fc12812fbf9a3adbc9838e60e6ce5",
    normalizeLineEndings: true,
  },
  {
    path: path.join(__dirname, "fonts", "geist-latin.woff2"),
    sha256: "19f9c92546aa300c312235e3125af1b81394d8db9a4bc4a425cd5b641d2d54e1",
  },
  {
    path: path.join(__dirname, "fonts", "lora-latin.woff2"),
    sha256: "ddb8c66035104e233fc024669183aad3738b6daa16deee2ebb1241bd0f98ace1",
  },
] as const;

type ManagedServerPids = {
  listenerPid: number | null;
  managerPid: number;
  serverPid: number;
};

function verifyFontFixtures(): void {
  for (const fixture of FONT_FIXTURES) {
    const contents = fs.readFileSync(fixture.path);
    const actual = createHash("sha256")
      .update(
        fixture.normalizeLineEndings
          ? contents.toString("utf8").replace(/\r\n/g, "\n")
          : contents,
      )
      .digest("hex");
    if (actual !== fixture.sha256) {
      throw new Error(
        `Font fixture checksum mismatch for ${fixture.path}: expected ${fixture.sha256}, received ${actual}.`,
      );
    }
  }
}

function syncOpenMaicImporter(): void {
  const result = spawnSync(process.execPath, [IMPORTER_SYNC_ENTRYPOINT], {
    cwd: WEB_ROOT,
    encoding: "utf8",
    timeout: 120_000,
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `OpenMAIC importer sync failed (${result.status}): ${result.stderr.trim()}`,
    );
  }
  if (result.stdout.trim()) {
    console.log(`[baseline-web-server] ${result.stdout.trim()}`);
  }
}

function getServerStatus(
  pathname: string,
  timeoutMs: number,
): Promise<number | undefined> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (status?: number) => {
      if (!settled) {
        settled = true;
        resolve(status);
      }
    };
    const request = http.get(`${LOCAL_WEB_BASE_URL}${pathname}`, (response) => {
      response.destroy();
      finish(response.statusCode);
    });

    request.once("error", () => finish());
    request.setTimeout(timeoutMs, () => {
      request.destroy();
      finish();
    });
  });
}

function isPortOpen(timeoutMs = 1_000): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({
      host: "127.0.0.1",
      port: Number(LOCAL_WEB_PORT),
    });
    let settled = false;
    const finish = (open: boolean) => {
      if (settled) {
        return;
      }
      settled = true;
      socket.destroy();
      resolve(open);
    };

    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
    socket.setTimeout(timeoutMs, () => finish(false));
  });
}

async function warmServer(): Promise<void> {
  for (const pathname of BASELINE_ROUTES) {
    const status = await getServerStatus(pathname, WARMUP_TIMEOUT_MS);
    if (status === undefined || status < 200 || status >= 400) {
      throw new Error(
        `Next.js server warm-up for ${LOCAL_WEB_BASE_URL}${pathname} returned ${status ?? "no response"}.`,
      );
    }
  }

  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const context = await browser.newContext();
    try {
      await context.route("**/*", async (route) => {
        const request = route.request();
        const url = new URL(request.url());
        if (url.origin !== LOCAL_WEB_BASE_URL) {
          await route.abort();
          return;
        }
        if (url.pathname.startsWith("/api/")) {
          const payload = apiPayload(url.pathname, "snow");
          if (request.method() !== "GET" || payload === undefined) {
            await route.abort();
            return;
          }
          await route.fulfill({
            body: JSON.stringify(payload),
            contentType: "application/json",
            status: 200,
          });
          return;
        }
        await route.continue();
      });

      const page = await context.newPage();
      try {
        for (const pathname of BASELINE_ROUTES) {
          const url = `${LOCAL_WEB_BASE_URL}${pathname}`;
          let response;
          try {
            response = await page.goto(url, {
              timeout: WARMUP_TIMEOUT_MS,
              waitUntil: "domcontentloaded",
            });
          } catch (error) {
            throw new Error(
              `Next.js client warm-up for ${url} failed: ${
                error instanceof Error ? error.message : String(error)
              }`,
            );
          }
          const status = response?.status();
          if (status === undefined || status < 200 || status >= 400) {
            throw new Error(
              `Next.js client warm-up for ${url} returned ${status ?? "no response"}.`,
            );
          }
        }
      } finally {
        await page.close();
      }
    } finally {
      await context.close();
    }
  } finally {
    await browser.close();
  }
}

function hasExited(child: ChildProcess): boolean {
  return child.exitCode !== null || child.signalCode !== null;
}

function waitForServerProcess(
  child: ChildProcess,
): Promise<ManagedServerPids> {
  return new Promise((resolve, reject) => {
    let startingPids: Omit<ManagedServerPids, "listenerPid"> | undefined;
    const timeout = setTimeout(() => {
      cleanup();
      reject(
        new Error(
          `Timed out waiting for the Next.js server process on port ${LOCAL_WEB_PORT}.`,
        ),
      );
    }, 300_000);
    const cleanup = () => {
      clearTimeout(timeout);
      child.off("message", onMessage);
      child.off("exit", onExit);
    };
    const onMessage = (message: unknown) => {
      if (
        typeof message === "object" &&
        message !== null &&
        "type" in message &&
        message.type === "starting" &&
        "managerPid" in message &&
        "serverPid" in message &&
        typeof message.managerPid === "number" &&
        typeof message.serverPid === "number"
      ) {
        startingPids = {
          managerPid: message.managerPid,
          serverPid: message.serverPid,
        };
        console.log(
          `[baseline-web-server] starting manager PID ${message.managerPid}, Next CLI PID ${message.serverPid}, port ${LOCAL_WEB_PORT}`,
        );
        return;
      }
      if (
        typeof message !== "object" ||
        message === null ||
        !("type" in message) ||
        message.type !== "listening" ||
        !("listenerPid" in message) ||
        !("managerPid" in message) ||
        !("serverPid" in message) ||
        (message.listenerPid !== null &&
          typeof message.listenerPid !== "number") ||
        typeof message.managerPid !== "number" ||
        typeof message.serverPid !== "number"
      ) {
        return;
      }
      cleanup();
      resolve({
        listenerPid: message.listenerPid,
        managerPid: message.managerPid,
        serverPid: message.serverPid,
      });
    };
    const onExit = (code: number | null, signal: NodeJS.Signals | null) => {
      cleanup();
      const processDetails = startingPids
        ? ` (manager PID ${startingPids.managerPid}, Next CLI PID ${startingPids.serverPid})`
        : "";
      reject(
        new Error(
          `Next.js server process exited before listening${processDetails} (exit code ${code}, signal ${signal}).`,
        ),
      );
    };

    child.on("message", onMessage);
    child.once("exit", onExit);
  });
}

function waitForExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    if (hasExited(child)) {
      resolve(true);
      return;
    }

    const timeout = setTimeout(() => {
      child.off("exit", onExit);
      resolve(false);
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timeout);
      resolve(true);
    };
    child.once("exit", onExit);
  });
}

async function stopServerProcess(child: ChildProcess): Promise<void> {
  if (hasExited(child)) {
    return;
  }

  if (child.connected) {
    child.send({ type: "shutdown" });
  }
  if (await waitForExit(child, 30_000)) {
    return;
  }

  if (process.platform === "win32") {
    const result = spawnSync(
      "taskkill.exe",
      ["/PID", String(child.pid), "/T", "/F"],
      { encoding: "utf8", windowsHide: true },
    );
    if (result.error) {
      throw result.error;
    }
    if (
      result.status !== 0 &&
      !hasExited(child) &&
      typeof child.pid === "number" &&
      isProcessAlive(child.pid)
    ) {
      throw new Error(
        `Could not terminate managed Next.js process tree PID ${child.pid}: ${result.stderr.trim()}`,
      );
    }
  } else if (typeof child.pid === "number") {
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") {
        throw error;
      }
    }
  }
  if (!(await waitForExit(child, 5_000))) {
    throw new Error(`Next.js server process PID ${child.pid} did not exit.`);
  }
}

function isProcessAlive(processId: number): boolean {
  try {
    process.kill(processId, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

export default async function setupManagedWebServer(): Promise<
  () => Promise<void>
> {
  if (await isPortOpen()) {
    throw new Error(
      `Refusing to reuse an unmanaged server at ${LOCAL_SERVER_URL}. Set WEB_BASE_URL explicitly when testing an external server.`,
    );
  }

  const previousWebBaseUrl = process.env.WEB_BASE_URL;
  process.env.WEB_BASE_URL = LOCAL_WEB_BASE_URL;
  const restoreWebBaseUrl = () => {
    if (previousWebBaseUrl === undefined) {
      delete process.env.WEB_BASE_URL;
      return;
    }
    process.env.WEB_BASE_URL = previousWebBaseUrl;
  };

  verifyFontFixtures();
  syncOpenMaicImporter();
  const child = fork(SERVER_ENTRYPOINT, [LOCAL_WEB_PORT], {
    cwd: WEB_ROOT,
    env: {
      ...process.env,
      NODE_OPTIONS: `--max-old-space-size=${MANAGED_HEAP_LIMIT_MB}`,
      NODE_ENV: "development",
      NEXT_FONT_GOOGLE_MOCKED_RESPONSES: FONT_MOCKS_PATH,
      PW_VISUAL_BASELINE: "1",
    },
    execArgv: [`--max-old-space-size=${MANAGED_HEAP_LIMIT_MB}`],
    detached: process.platform !== "win32",
    stdio: ["ignore", "inherit", "inherit", "ipc"],
  });

  let pids: ManagedServerPids;
  try {
    pids = await waitForServerProcess(child);
    console.log(
      `[baseline-web-server] started manager PID ${pids.managerPid}, Next CLI PID ${pids.serverPid}, listener PID ${pids.listenerPid}, port ${LOCAL_WEB_PORT}`,
    );
    await warmServer();
  } catch (error) {
    await stopServerProcess(child);
    restoreWebBaseUrl();
    throw error;
  }

  return async () => {
    try {
      await stopServerProcess(child);
      if (
        (pids.listenerPid !== null && isProcessAlive(pids.listenerPid)) ||
        isProcessAlive(pids.serverPid) ||
        isProcessAlive(pids.managerPid)
      ) {
        throw new Error(
          `Managed server processes did not exit (manager PID ${pids.managerPid}, Next CLI PID ${pids.serverPid}, listener PID ${pids.listenerPid}).`,
        );
      }
      if (await isPortOpen()) {
        throw new Error(
          `Next.js dev server at ${LOCAL_SERVER_URL} remained available after shutdown.`,
        );
      }
      console.log(
        `[baseline-web-server] stopped manager PID ${pids.managerPid}, Next CLI PID ${pids.serverPid}, listener PID ${pids.listenerPid}, port ${LOCAL_WEB_PORT}`,
      );
    } finally {
      restoreWebBaseUrl();
    }
  };
}
