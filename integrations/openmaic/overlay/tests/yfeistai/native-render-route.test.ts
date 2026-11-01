import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import JSZip from "jszip";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ExportGenerationRequest, ExporterContext, ExportArtifactOutput } from "../../lib/yfeistai/export-generation";

const captured = vi.hoisted(() => ({
  render: null as null | ((request: ExportGenerationRequest, context: ExporterContext) => Promise<ExportArtifactOutput>),
}));
vi.mock("pptxgenjs", () => ({ default: class {} }));
vi.mock("node:fs/promises", () => ({ readFile: async () => "/* vendored GSAP */" }));
vi.mock("../../lib/yfeistai/export-generation", async (original) => ({
  ...await original<Record<string, unknown>>(),
  createRenderHtmlArtifact: () => ({ bytes: new TextEncoder().encode("<!doctype html><main>Lesson</main>") }),
  createExportPostHandler: (dependencies: { renderMp4: typeof captured.render }) => {
    captured.render = dependencies.renderMp4;
    return async () => new Response(null, { status: 202 });
  },
}));

beforeEach(async () => {
  vi.resetModules();
  vi.stubEnv("YFEISTAI_OPENMAIC_RENDER_ENDPOINT", "http://openmaic-render:9000");
  vi.stubEnv("YFEISTAI_OPENMAIC_STATE_ROOT", mkdtempSync(path.join(tmpdir(), "native-render-route-")));
  await import("../../app/api/yfeistai/v1/exports/route");
});
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

function request(jobId: string): ExportGenerationRequest {
  return {
    tenantId: "tenant-a", jobId, idempotencyKey: jobId,
    classroomDocument: {}, mediaManifest: [],
  } as ExportGenerationRequest;
}

function box(type: string, payload = Buffer.alloc(0)): Buffer {
  const header = Buffer.alloc(8);
  header.writeUInt32BE(8 + payload.length);
  header.write(type, 4, 4, "ascii");
  return Buffer.concat([header, payload]);
}

test("submits a native multipart project, polls and downloads the rendered video", async () => {
  const media = Buffer.concat([
    box("ftyp", Buffer.from("isom0000isommp42")),
    box("moov", box("mvhd")), box("mdat", Buffer.from([1])),
  ]);
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json({ jobId: "render-a" }, { status: 202 }))
    .mockResolvedValueOnce(Response.json({ status: "succeeded" }))
    .mockResolvedValueOnce(new Response(media, { headers: { "content-type": "video/mp4" } }));
  vi.stubGlobal("fetch", fetcher);
  const output = await captured.render!(request("export-a"), {});
  expect(Buffer.from(output.bytes)).toEqual(media);
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
    "http://openmaic-render:9000/render",
    "http://openmaic-render:9000/render/render-a",
    "http://openmaic-render:9000/render/render-a/download",
  ]);
  const init = fetcher.mock.calls[0][1] as RequestInit;
  expect(init.method).toBe("POST");
  expect(init.headers).toMatchObject({ "x-openmaic-client": "tenant-a" });
  expect(init.redirect).toBe("error");
  const form = init.body as FormData;
  expect(form.get("fps")).toBe("24");
  expect(form.get("format")).toBe("mp4");
  const archive = await JSZip.loadAsync(await (form.get("project") as File).arrayBuffer());
  expect(await archive.file("index.html")!.async("string")).toContain("Lesson");
});

test("cancels an already submitted native render when the export is canceled", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json({ jobId: "render-b" }, { status: 202 }))
    .mockResolvedValueOnce(Response.json({ cancelled: true }));
  vi.stubGlobal("fetch", fetcher);
  let checks = 0;
  await expect(captured.render!(request("export-b"), { isCanceled: async () => ++checks > 1 }))
    .rejects.toMatchObject({ code: "JOB_CANCELED" });
  expect(fetcher.mock.calls[1][0]).toBe("http://openmaic-render:9000/render/render-b");
  expect(fetcher.mock.calls[1][1]).toMatchObject({ method: "DELETE", redirect: "error" });
});
