import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import net from "node:net";
import path from "node:path";

const WEB_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../..",
);
const port = Number(process.argv[2]);
const hostname = "127.0.0.1";
const MANAGED_HEAP_LIMIT_MB = 4096;
const nextBin = path.join(
  WEB_ROOT,
  "node_modules",
  "next",
  "dist",
  "bin",
  "next",
);
const serverProcess = spawn(
  process.execPath,
  [
    `--max-old-space-size=${MANAGED_HEAP_LIMIT_MB}`,
    nextBin,
    "dev",
    "--webpack",
    "--hostname",
    hostname,
    "--port",
    String(port),
  ],
  {
    cwd: WEB_ROOT,
    env: process.env,
    stdio: "inherit",
    windowsHide: true,
  },
);
let serverProcessError;
serverProcess.once("error", (error) => {
  serverProcessError = error;
});
let shuttingDown = false;

if (typeof serverProcess.pid === "number") {
  process.send?.({
    type: "starting",
    managerPid: process.pid,
    serverPid: serverProcess.pid,
  });
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function canConnect() {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: hostname, port });
    let settled = false;
    const finish = (connected) => {
      if (settled) {
        return;
      }
      settled = true;
      socket.destroy();
      resolve(connected);
    };

    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
    socket.setTimeout(500, () => finish(false));
  });
}

async function waitForPort(expectedOpen, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((await canConnect()) === expectedOpen) {
      return true;
    }
    if (expectedOpen && (hasExited(serverProcess) || serverProcessError)) {
      return false;
    }
    await delay(250);
  }
  return false;
}

function hasExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function getListenerPid() {
  if (process.platform !== "win32") {
    return null;
  }

  const result = spawnSync("netstat.exe", ["-ano", "-p", "TCP"], {
    encoding: "utf8",
    timeout: 10_000,
    windowsHide: true,
  });
  if (result.error) {
    throw result.error;
  }

  const processIds = [
    ...new Set(
      result.stdout
        .split(/\r?\n/)
        .map((line) => line.trim().split(/\s+/))
        .filter(
          (columns) =>
            columns[0] === "TCP" &&
            columns[1]?.endsWith(`:${port}`) &&
            columns[3] === "LISTENING",
        )
        .map((columns) => Number(columns[4]))
        .filter(Number.isInteger),
    ),
  ];
  if (processIds.length !== 1) {
    throw new Error(
      `Expected one listener on port ${port}, found PIDs ${processIds.join(", ") || "none"}.`,
    );
  }
  return processIds[0];
}

function waitForChildExit(child, timeoutMs) {
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

async function stopChild() {
  if (!hasExited(serverProcess)) {
    const gracefulExit = waitForChildExit(serverProcess, 30_000);
    serverProcess.kill("SIGTERM");
    if (!(await gracefulExit)) {
      const forcedExit = waitForChildExit(serverProcess, 5_000);
      serverProcess.kill("SIGKILL");
      if (!(await forcedExit)) {
        throw new Error(
          `Next.js CLI process PID ${serverProcess.pid} did not exit.`,
        );
      }
    }
  }

  if (!(await waitForPort(false, 15_000))) {
    throw new Error(`Next.js CLI left port ${port} open after shutdown.`);
  }
}

async function shutdown(exitCode = 0) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;

  try {
    await stopChild();
  } catch (error) {
    console.error(error);
    exitCode = 1;
  } finally {
    process.exit(exitCode);
  }
}

process.on("message", (message) => {
  if (message?.type === "shutdown") {
    void shutdown();
  }
});
process.once("disconnect", () => void shutdown());
process.once("SIGINT", () => void shutdown());
process.once("SIGTERM", () => void shutdown());

if (!(await waitForPort(true, 300_000))) {
  console.error(
    serverProcessError
      ? `Could not start Next.js CLI: ${serverProcessError.message}`
      : hasExited(serverProcess)
      ? `Next.js CLI exited before listening (exit code ${serverProcess.exitCode}, signal ${serverProcess.signalCode}).`
      : `Timed out waiting for Next.js CLI on port ${port}.`,
  );
  await shutdown(1);
} else {
  const listenerPid = getListenerPid();
  process.send?.({
    type: "listening",
    listenerPid,
    managerPid: process.pid,
    serverPid: serverProcess.pid,
  });

  serverProcess.once("exit", (code, signal) => {
    if (!shuttingDown) {
      console.error(
        `Next.js CLI exited unexpectedly (exit code ${code}, signal ${signal}).`,
      );
      process.exit(code || 1);
    }
  });
}
