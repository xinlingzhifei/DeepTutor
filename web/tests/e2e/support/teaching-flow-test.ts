import { expect, test as base } from "@playwright/test";
import { createServer, type Server } from "node:http";

const DOWNLOAD_HOST = "127.0.0.1";
const DOWNLOAD_PORT = Number.parseInt(
  process.env.PW_TEACHING_DOWNLOAD_PORT || "18081",
  10,
);
const DOWNLOAD_PATH = "/api/v1/classroom-exports/export-teacher-2/download";
const DOWNLOAD_BODY = Buffer.from("e2e-pptx");

export interface TeachingDownloadState {
  baseURL: string;
  downloadCalls: number;
}

type TeachingWorkerFixtures = {
  teachingDownload: TeachingDownloadState;
};

function listen(server: Server): Promise<void> {
  return new Promise((resolve, reject) => {
    const handleError = (error: Error) => {
      server.off("listening", handleListening);
      reject(error);
    };
    const handleListening = () => {
      server.off("error", handleError);
      resolve();
    };
    server.once("error", handleError);
    server.once("listening", handleListening);
    server.listen(DOWNLOAD_PORT, DOWNLOAD_HOST);
  });
}

function close(server: Server): Promise<void> {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close(error => (error ? reject(error) : resolve()));
    server.closeAllConnections();
  });
}

export const test = base.extend<{}, TeachingWorkerFixtures>({
  teachingDownload: [
    async ({}, fixtureUse) => {
      const state: TeachingDownloadState = {
        baseURL: `http://${DOWNLOAD_HOST}:${DOWNLOAD_PORT}`,
        downloadCalls: 0,
      };
      const server = createServer((request, response) => {
        const requestUrl = new URL(request.url || "/", state.baseURL);
        if (request.method === "GET" && requestUrl.pathname === DOWNLOAD_PATH) {
          state.downloadCalls += 1;
          response.writeHead(200, {
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="energy-transfer.pptx"',
            "Content-Length": String(DOWNLOAD_BODY.byteLength),
            "Content-Type":
              "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          });
          response.end(DOWNLOAD_BODY);
          return;
        }
        response.writeHead(404, { "Content-Type": "text/plain" });
        response.end("not found");
      });

      await listen(server);
      try {
        await fixtureUse(state);
      } finally {
        await close(server);
      }
    },
    { auto: true, scope: "worker" },
  ],
});

export { expect };
