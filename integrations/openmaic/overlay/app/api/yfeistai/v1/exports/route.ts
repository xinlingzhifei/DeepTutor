import JSZip from "jszip";
import PptxGenJS from "pptxgenjs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { latexToOmml } from "@/lib/export/latex-to-omml";
import type { JsonValue } from "../../../../../lib/yfeistai/portable-classroom";

import {
  artifactStore,
  normalizeArtifactPath,
} from "../../../../../lib/yfeistai/artifact-manifest";
import {
  type ArchiveEntryDescriptor,
  type ExportArtifactOutput,
  type ExportGenerationRequest,
  type ExporterContext,
  ExportPipelineError,
  asPortableDocument,
  canonicalExportJson,
  cancelRemoteRenderIfRequested,
  createOfflineHtmlArtifact,
  createRenderHtmlArtifact,
  slideElementText,
  createExportPostHandler,
  exportJobStore,
  inspectZipArchive,
  isMp4MediaType,
  readResponseBytesLimited,
  validateArchiveEntries,
  validateMp4Artifact,
} from "../../../../../lib/yfeistai/export-generation";
import {
  readServiceSecret,
} from "../../../../../lib/yfeistai/service-auth";
import {
  configuredOpenMaicStateRoot,
  durableFile,
  exactDurableRecord,
  readDurableJson,
  writeDurableJsonExclusive,
} from "../../../../../lib/yfeistai/durable-state";

interface MediaInput {
  relativePath: string;
  mime: string;
  sha256: string;
  bytes: Uint8Array;
}

function manifestArtifacts(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) {
    throw new Error("media manifest is invalid");
  }
  return value.map((item) => {
    if (item === null || typeof item !== "object" || Array.isArray(item)) {
      throw new Error("media manifest entry is invalid");
    }
    return item as Record<string, unknown>;
  });
}

async function loadMedia(
  request: ExportGenerationRequest,
): Promise<MediaInput[]> {
  const result: MediaInput[] = [];
  if (manifestArtifacts(request.mediaManifest).length > 0 && !request.sourceJobId) {
    throw new Error("media source job binding is missing");
  }
  for (const item of manifestArtifacts(request.mediaManifest)) {
    if (
      typeof item.relativePath !== "string" ||
      typeof item.sha256 !== "string"
    ) {
      throw new Error("media manifest entry is invalid");
    }
    const declaredPath = item.relativePath;
    const declaredSha256 = item.sha256;
    const stored = await artifactStore.read(
      request.tenantId,
      request.sourceJobId as string,
      declaredPath,
    );
    if (!stored) {
      throw new Error("manifest media is unavailable");
    }
    const relativePath = normalizeArtifactPath(declaredPath);
    const mime =
      typeof item.mime === "string"
        ? item.mime
        : typeof item.mimeType === "string"
          ? item.mimeType
          : stored.entry.mime;
    const declaredBytes =
      typeof item.bytes === "number"
        ? item.bytes
        : typeof item.sizeBytes === "number"
          ? item.sizeBytes
          : -1;
    if (
      stored.entry.relativePath !== relativePath ||
      stored.entry.sha256 !== declaredSha256 ||
      stored.entry.mime !== mime ||
      stored.entry.bytes !== declaredBytes
    ) {
      throw new Error("manifest media metadata does not match its artifact");
    }
    result.push({
      relativePath,
      mime,
      sha256: declaredSha256,
      bytes: stored.bytes,
    });
  }
  return result;
}

function archiveDescriptor(
  relativePath: string,
  bytes: Uint8Array,
): ArchiveEntryDescriptor {
  return {
    relativePath,
    uncompressedBytes: bytes.byteLength,
    compressedBytes: Math.max(1, bytes.byteLength),
    kind: "file",
  };
}

async function exportClassroomZip(
  request: ExportGenerationRequest,
): Promise<ExportArtifactOutput> {
  const zip = new JSZip();
  const documentBytes = new TextEncoder().encode(
    canonicalExportJson(asPortableDocument(request.classroomDocument)),
  );
  zip.file("classroom.json", documentBytes);
  const archiveEntries = [archiveDescriptor("classroom.json", documentBytes)];
  for (const media of await loadMedia(request)) {
    const relativePath = normalizeArtifactPath(media.relativePath);
    zip.file(relativePath, media.bytes);
    archiveEntries.push(archiveDescriptor(relativePath, media.bytes));
  }
  validateArchiveEntries(archiveEntries);
  const bytes = await zip.generateAsync({
      type: "uint8array",
      compression: "DEFLATE",
      compressionOptions: { level: 6 },
    });
  return {
    bytes,
    archiveEntries: inspectZipArchive(bytes),
  };
}

function sceneSummary(scene: Record<string, unknown>): string {
  const content =
    scene.content &&
    typeof scene.content === "object" &&
    !Array.isArray(scene.content)
      ? (scene.content as Record<string, unknown>)
      : {};
  if (scene.type === "quiz" && Array.isArray(content.questions)) {
    return content.questions
      .map((question) =>
        question &&
        typeof question === "object" &&
        !Array.isArray(question) &&
        typeof (question as Record<string, unknown>).prompt === "string"
          ? (question as Record<string, unknown>).prompt
          : "",
      )
      .filter(Boolean)
      .join("\n");
  }
  if (scene.type === "interactive") {
    const html = typeof content.html === "string" ? content.html : "";
    const preview = html
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
      .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    return [
      "Interactive scene (static preview)",
      preview ||
        "Live interaction content is available in the original classroom.",
    ].join("\n\n");
  }
  if (scene.type === "pbl") {
    const roles = Array.isArray(content.roles)
      ? content.roles
          .map((role) =>
            role && typeof role === "object" && !Array.isArray(role)
              ? `${String((role as Record<string, unknown>).name)}: ${String((role as Record<string, unknown>).brief)}`
              : "",
          )
          .filter(Boolean)
      : [];
    const milestones = Array.isArray(content.milestones)
      ? content.milestones
          .map((milestone) =>
            milestone &&
            typeof milestone === "object" &&
            !Array.isArray(milestone)
              ? `${String((milestone as Record<string, unknown>).title)}: ${String((milestone as Record<string, unknown>).rubric)}`
              : "",
          )
          .filter(Boolean)
      : [];
    return [
      "Project-based learning scene (static preview)",
      String(content.scenario),
      roles.length > 0 ? `Roles\n${roles.join("\n")}` : "",
      milestones.length > 0 ? `Milestones\n${milestones.join("\n")}` : "",
    ]
      .filter(Boolean)
      .join("\n\n");
  }
  const canvas =
    content.canvas &&
    typeof content.canvas === "object" &&
    !Array.isArray(content.canvas)
      ? (content.canvas as Record<string, unknown>)
      : {};
  const elements = Array.isArray(canvas.elements) ? canvas.elements : [];
  return elements
    .map((element) => {
      if (
        element === null ||
        typeof element !== "object" ||
        Array.isArray(element)
      ) {
        return "";
      }
      const record = element as Record<string, unknown>;
      return slideElementText(record);
    })
    .filter(Boolean)
    .join("\n");
}

async function exportPptx(
  request: ExportGenerationRequest,
): Promise<ExportArtifactOutput> {
  const document = asPortableDocument(request.classroomDocument);
  const presentation = new PptxGenJS();
  presentation.layout = "LAYOUT_WIDE";
  presentation.author = "yFeiSTAI";
  presentation.subject = document.classroomId;
  presentation.title = document.openmaic.stage.name;
  const media = await loadMedia(request);
  for (const scene of document.openmaic.scenes) {
    const slide = presentation.addSlide();
    slide.background = { color: "F7F8FA" };
    slide.addText(scene.title, {
      x: 0.65,
      y: 0.45,
      w: 12,
      h: 0.6,
      fontSize: 28,
      bold: true,
      color: "172033",
    });
    const sceneJson = canonicalExportJson(scene);
    const sceneMedia = media.filter((item) =>
      sceneJson.includes(item.relativePath),
    );
    const images = sceneMedia.filter((item) => item.mime.startsWith("image/"));
    const bodyWidth = images.length > 0 ? 7.2 : 11.8;
    const canvasElements = scene.content.type === "slide" &&
      Array.isArray(scene.content.canvas.elements)
      ? scene.content.canvas.elements.filter(
          (value): value is Record<string, JsonValue> =>
            value !== null && typeof value === "object" && !Array.isArray(value),
        )
      : [];
    if (canvasElements.some((element) => element.type === "latex")) {
      // Keep text and equations in their original relative positions. Flattening
      // the canvas into one text box exposes LaTeX source and loses its layout.
      const elements = canvasElements.filter((element) => element.type === "text" || element.type === "latex");
      if (!elements.every((element) =>
        [element.left, element.top, element.width, element.height].every(
          (value) => typeof value === "number" && Number.isFinite(value),
        ) && Number(element.width) > 0 && Number(element.height) > 0,
      )) {
        throw new Error("PPTX canvas element geometry is invalid");
      }
      const width = Math.max(1000, ...elements.map((element) => Number(element.left) + Number(element.width)));
      const height = Math.max(562.5, ...elements.map((element) => Number(element.top) + Number(element.height)));
      const scale = Math.min(bodyWidth / width, 5.4 / height);
      for (const element of elements) {
        const box = {
          x: 0.75 + Number(element.left) * scale,
          y: 1.2 + Number(element.top) * scale,
          w: Number(element.width) * scale,
          h: Number(element.height) * scale,
        };
        if (element.type === "latex" && typeof element.latex === "string") {
          const lines = (element.latex.match(/\\\\/g) || []).length + 1;
          const fontSize = Math.max(10, Math.round(box.h * 72 / (lines * 2.5)));
          const omml = latexToOmml(element.latex, fontSize);
          if (!omml) throw new Error("PPTX formula conversion failed");
          slide.addFormula({
            ...box, omml, fontSize,
            align: element.align === "left" || element.align === "right" ? element.align : "center",
          });
        } else {
          const text = slideElementText(element);
          if (!text) continue;
          const html = typeof element.content === "string" ? element.content : "";
          const fontPixels = Number(html.match(/font-size:\s*([\d.]+)px/i)?.[1] || 18);
          slide.addText(text, {
            ...box, fontSize: Math.max(10, fontPixels * scale * 72),
            fontFace: typeof element.defaultFontName === "string" ? element.defaultFontName : "Microsoft YaHei",
            color: typeof element.defaultColor === "string" && /^#[a-f\d]{6}$/i.test(element.defaultColor)
              ? element.defaultColor.slice(1) : "344054",
            bold: /<(strong|b)\b/i.test(html),
            valign: "top", margin: 0, fit: "shrink",
          });
        }
      }
    } else slide.addText(sceneSummary(scene as unknown as Record<string, unknown>), {
      x: 0.75,
      y: 1.35,
      w: bodyWidth,
      h: 5.4,
      fontSize: 17,
      color: "344054",
      valign: "top",
      margin: 0.08,
    });
    for (const [index, image] of images.entries()) {
      const target = index < 4 ? slide : presentation.addSlide();
      if (index >= 4) {
        target.background = { color: "F7F8FA" };
        target.addText(`${scene.title} — media ${index + 1}`, {
          x: 0.65, y: 0.45, w: 12, h: 0.6, fontSize: 24, bold: true,
        });
      }
      const slot = index < 4 ? index : 0;
      target.addImage({
        data: `data:${image.mime};base64,${Buffer.from(image.bytes).toString("base64")}`,
        x: index < 4 ? 8.2 + (slot % 2) * 2.2 : 1.2,
        y: index < 4 ? 1.45 + Math.floor(slot / 2) * 2.05 : 1.5,
        w: index < 4 ? 2.05 : 10.9,
        h: index < 4 ? 1.85 : 5.6,
      });
    }
    const nonImageMedia = sceneMedia.filter(
      (item) => !item.mime.startsWith("image/"),
    );
    if (nonImageMedia.length > 0) {
      slide.addText(
        `Audio/video retained in the controlled classroom package:\n${nonImageMedia
          .map((item) => `${item.relativePath} (SHA-256 ${item.sha256})`)
          .join("\n")}`,
        {
          x: 8.2,
          y: 5.05,
          w: 4.35,
          h: 0.8,
          fontSize: 10,
          color: "667085",
        },
      );
      for (const item of nonImageMedia) {
        if (!item.mime.startsWith("audio/") && !item.mime.startsWith("video/")) {
          throw new Error("PPTX media MIME is unsupported");
        }
        const mediaSlide = presentation.addSlide();
        mediaSlide.background = { color: "F7F8FA" };
        mediaSlide.addText(`${scene.title} — ${item.relativePath}`, {
          x: 0.65,
          y: 0.45,
          w: 12,
          h: 0.6,
          fontSize: 22,
          bold: true,
        });
        mediaSlide.addMedia({
          type: item.mime.startsWith("audio/") ? "audio" : "video",
          data: `${item.mime};base64,${Buffer.from(item.bytes).toString("base64")}`,
          x: 1.2,
          y: 1.5,
          w: 10.9,
          h: 4.8,
        });
        mediaSlide.addText(`SHA-256 ${item.sha256}`, {
          x: 1.2,
          y: 6.45,
          w: 10.9,
          h: 0.3,
          fontSize: 9,
          color: "667085",
        });
      }
    }
    {
      const configuredOrigin = process.env.YFEISTAI_PUBLIC_ORIGIN?.trim();
      let classroomUrl: string | null = null;
      if (configuredOrigin) {
        const parsed = new URL(configuredOrigin);
        if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password) {
          throw new Error("public classroom origin is invalid");
        }
        classroomUrl = new URL(
          `/classrooms/${encodeURIComponent(document.classroomId)}`,
          parsed,
        ).toString();
      }
      slide.addText(
        classroomUrl
          ? "Open the controlled original classroom"
          : `Controlled classroom: ${document.classroomId}`,
        {
        x: 0.75,
        y: 6.75,
        w: 5,
        h: 0.3,
        fontSize: 10,
        color: "667085",
        ...(classroomUrl ? { hyperlink: { url: classroomUrl } } : {}),
      });
      slide.addText(`Document SHA-256: ${request.classroomDocumentSha256}`, {
        x: 5.8,
        y: 6.75,
        w: 6.75,
        h: 0.3,
        fontSize: 8,
        color: "667085",
        align: "right",
        margin: 0,
      });
    }
  }
  const output = await presentation.write({ outputType: "nodebuffer" });
  const bytes =
    output instanceof Uint8Array
      ? new Uint8Array(output)
      : new Uint8Array(output as ArrayBuffer);
  return { bytes };
}

async function exportOfflineHtml(
  request: ExportGenerationRequest,
): Promise<ExportArtifactOutput> {
  const document = asPortableDocument(request.classroomDocument);
  return createOfflineHtmlArtifact(document, await loadMedia(request));
}

async function assertRenderActive(context: ExporterContext): Promise<void> {
  if (context.isCanceled && (await context.isCanceled())) {
    throw new ExportPipelineError(
      "JOB_CANCELED",
      "The export job was canceled.",
    );
  }
}

async function bestEffortCancelRenderJob(
  rendererBase: string,
  renderJobId: string,
  request: ExportGenerationRequest,
): Promise<void> {
  try {
    await nativeRendererFetch(
      rendererBase,
      `/render/${renderJobId}`,
      "DELETE",
      "",
      request,
      5_000,
    );
  } catch {
    // The caller's stable render failure remains authoritative.
  }
}

async function nativeRendererFetch(
  rendererBase: string,
  requestPath: string,
  method: "GET" | "POST" | "DELETE",
  body: string | FormData,
  request: ExportGenerationRequest,
  timeoutMilliseconds: number,
): Promise<Response> {
  // The pinned renderer has a native multipart API on the private network.
  // Tenant authentication and job ownership are enforced by this signed adapter.
  return rendererFetch(`${rendererBase}${requestPath}`, {
    method,
    headers: {
      "x-openmaic-client": request.tenantId,
    },
    body: method === "POST" ? body : undefined,
    signal: AbortSignal.timeout(Math.max(1, timeoutMilliseconds)),
  });
}

function persistedRenderJobId(
  request: ExportGenerationRequest,
  rendererBase: string,
): { target: string; renderJobId: string | null } {
  const target = durableFile(
    configuredOpenMaicStateRoot(),
    "render-jobs",
    "checkpoints",
    [request.tenantId, request.jobId],
    "render.json",
  );
  const raw = readDurableJson(target);
  if (!raw) {
    return { target, renderJobId: null };
  }
  const record = exactDurableRecord(raw, "render checkpoint", [
    "version",
    "tenantId",
    "jobId",
    "idempotencyKey",
    "rendererBase",
    "renderJobId",
  ]);
  if (
    record.version !== 1 ||
    record.tenantId !== request.tenantId ||
    record.jobId !== request.jobId ||
    record.idempotencyKey !== request.idempotencyKey ||
    record.rendererBase !== rendererBase ||
    typeof record.renderJobId !== "string" ||
    record.renderJobId.length === 0
  ) {
    throw new ExportPipelineError(
      "MP4_RENDER_FAILED",
      "MP4 render checkpoint is invalid.",
    );
  }
  return { target, renderJobId: record.renderJobId };
}

async function rendererFetch(
  url: string,
  init: RequestInit,
): Promise<Response> {
  try {
    return await fetch(url, { ...init, redirect: "error" });
  } catch (error) {
    if (
      error instanceof Error &&
      (error.name === "TimeoutError" || error.name === "AbortError")
    ) {
      throw new ExportPipelineError(
        "MP4_RENDER_TIMEOUT",
        "MP4 rendering timed out.",
      );
    }
    throw new ExportPipelineError(
      "MP4_RENDER_UNAVAILABLE",
      "MP4 renderer is unavailable.",
    );
  }
}

function assertRendererResponse(response: Response): void {
  if (response.ok) {
    return;
  }
  if (response.status >= 500) {
    throw new ExportPipelineError(
      "MP4_RENDER_UNAVAILABLE",
      "MP4 renderer is unavailable.",
    );
  }
  throw new ExportPipelineError(
    "MP4_RENDER_FAILED",
    "MP4 renderer rejected the job.",
  );
}

async function renderMp4(
  request: ExportGenerationRequest,
  context: ExporterContext,
): Promise<ExportArtifactOutput> {
  const endpoint = process.env.YFEISTAI_OPENMAIC_RENDER_ENDPOINT;
  if (!endpoint) {
    throw new ExportPipelineError(
      "MP4_RENDER_UNAVAILABLE",
      "MP4 rendering is not configured.",
    );
  }
  const rendererBase = endpoint.endsWith("/")
    ? endpoint.slice(0, -1)
    : endpoint;
  const deadline = Date.now() + 15 * 60_000;
  const remaining = (maximum: number) => Math.min(maximum, deadline - Date.now());
  await assertRenderActive(context);
  const checkpoint = persistedRenderJobId(request, rendererBase);
  let rawRenderJobId = checkpoint.renderJobId;
  if (!rawRenderJobId) {
    const media = await loadMedia(request);
    const gsap = await readFile(path.join(process.cwd(), "public/vendor/gsap.min.js"), "utf8");
    const project = new JSZip();
    project.file("index.html", createRenderHtmlArtifact(request.classroomDocument, media, gsap).bytes);
    const zipBytes = await project.generateAsync({ type: "uint8array", compression: "DEFLATE" });
    validateArchiveEntries(inspectZipArchive(zipBytes));
    const body = new FormData();
    body.set("project", new Blob([Uint8Array.from(zipBytes).buffer], { type: "application/zip" }), "classroom.zip");
    body.set("fps", "24");
    body.set("quality", "standard");
    body.set("format", "mp4");
    const submit = await nativeRendererFetch(
      rendererBase,
      "/render",
      "POST",
      body,
      request,
      remaining(30_000),
    );
    assertRendererResponse(submit);
    let submitted: { jobId?: unknown };
    try {
      submitted = (await submit.json()) as { jobId?: unknown };
    } catch {
      throw new ExportPipelineError(
        "MP4_RENDER_FAILED",
        "MP4 renderer returned an invalid response.",
      );
    }
    if (typeof submitted.jobId !== "string" || submitted.jobId.length === 0) {
      throw new ExportPipelineError(
        "MP4_RENDER_FAILED",
        "MP4 renderer returned an invalid response.",
      );
    }
    rawRenderJobId = submitted.jobId;
    const saved = writeDurableJsonExclusive(checkpoint.target, {
      version: 1,
      tenantId: request.tenantId,
      jobId: request.jobId,
      idempotencyKey: request.idempotencyKey,
      rendererBase,
      renderJobId: rawRenderJobId,
    });
    if (!saved) {
      rawRenderJobId = persistedRenderJobId(request, rendererBase).renderJobId;
    }
  }
  if (!rawRenderJobId) {
    throw new ExportPipelineError("MP4_RENDER_FAILED", "MP4 render checkpoint is invalid.");
  }
  const renderJobId = encodeURIComponent(rawRenderJobId);
  try {
    for (;;) {
      if (Date.now() >= deadline) {
        throw new ExportPipelineError(
          "MP4_RENDER_TIMEOUT",
          "MP4 rendering timed out.",
        );
      }
      await assertRenderActive(context);
      const status = await nativeRendererFetch(
        rendererBase,
        `/render/${renderJobId}`,
        "GET",
        "",
        request,
        remaining(5_000),
      );
      assertRendererResponse(status);
      let state: { status?: unknown };
      try {
        state = (await status.json()) as { status?: unknown };
      } catch {
        throw new ExportPipelineError(
          "MP4_RENDER_FAILED",
          "MP4 renderer returned an invalid response.",
        );
      }
      if (state.status === "failed") {
        throw new ExportPipelineError(
          "MP4_RENDER_FAILED",
          "MP4 renderer failed.",
        );
      }
      if (state.status === "succeeded") {
        await assertRenderActive(context);
        const download = await nativeRendererFetch(
          rendererBase,
          `/render/${renderJobId}/download`,
          "GET",
          "",
          request,
          remaining(30_000),
        );
        assertRendererResponse(download);
        if (!isMp4MediaType(download.headers.get("content-type"))) {
          throw new ExportPipelineError(
            "MP4_RENDER_INVALID_ARTIFACT",
            "MP4 renderer returned an invalid artifact.",
          );
        }
        const bytes = await readResponseBytesLimited(download);
        validateMp4Artifact(bytes);
        return { bytes };
      }
      if (state.status !== "queued" && state.status !== "running") {
        throw new ExportPipelineError(
          "MP4_RENDER_FAILED",
          "MP4 renderer returned an invalid response.",
        );
      }
      await new Promise<void>((resolve) =>
        setTimeout(resolve, Math.min(1_000, Math.max(1, deadline - Date.now()))),
      );
    }
  } catch (error) {
    await cancelRemoteRenderIfRequested(context, () =>
      bestEffortCancelRenderJob(rendererBase, renderJobId, request),
    );
    throw error;
  }
}

const renderEndpoint = process.env.YFEISTAI_OPENMAIC_RENDER_ENDPOINT;
const postExport = createExportPostHandler({
  readSecret: readServiceSecret,
  store: exportJobStore,
  artifactStore,
  exportClassroomZip,
  exportPptx,
  exportOfflineHtml,
  renderEndpoint,
  renderMp4,
});

export async function POST(request: Request): Promise<Response> {
  return postExport(request);
}
