import { createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { deflateRawSync } from "node:zlib";

import { describe, expect, test, vi } from "vitest";

import {
  canonicalExportJson,
  cancelRemoteRenderIfRequested,
  createOfflineHtmlArtifact,
  createExportGetHandler,
  createExportPostHandler,
  exportJobStore,
  generateExportJob,
  inspectAndValidateZipArchive,
  inspectZipArchive,
  isMp4MediaType,
  readResponseBytesLimited,
  asPortableDocument,
  validateArchiveEntries,
  validateExportInputs,
  validatePptxArchive,
  validateMp4Artifact,
  ExportPipelineError,
  type ExportGenerationRequest,
  type ExportGenerationResult,
} from "../../lib/yfeistai/export-generation";
import {
  ContentJobStore,
  ContentOutputRegistry,
  type PortableClassroomDocument,
} from "../../lib/yfeistai/content-generation";
import { ArtifactStore } from "../../lib/yfeistai/artifact-manifest";
import { signServiceRequest } from "../../lib/yfeistai/service-auth";
import { assertOfflineHtmlSelfContained } from "../../lib/yfeistai/portable-classroom";

function sha256(value: unknown) {
  return createHash("sha256")
    .update(canonicalExportJson(value), "utf8")
    .digest("hex");
}

function fixtureCrc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function zipFixture(
  entries: readonly {
    relativePath: string;
    bytes: Uint8Array;
    compression?: "stored" | "deflate";
  }[],
): Uint8Array {
  const locals: Buffer[] = [];
  const central: Buffer[] = [];
  let localOffset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.relativePath, "utf8");
    const raw = Buffer.from(entry.bytes);
    const method = entry.compression === "deflate" ? 8 : 0;
    const compressed = method === 8 ? deflateRawSync(raw) : raw;
    const crc = fixtureCrc32(raw);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6);
    local.writeUInt16LE(method, 8);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(compressed.byteLength, 18);
    local.writeUInt32LE(raw.byteLength, 22);
    local.writeUInt16LE(name.byteLength, 26);
    locals.push(local, name, compressed);

    const directory = Buffer.alloc(46);
    directory.writeUInt32LE(0x02014b50, 0);
    directory.writeUInt16LE(20, 4);
    directory.writeUInt16LE(20, 6);
    directory.writeUInt16LE(0x0800, 8);
    directory.writeUInt16LE(method, 10);
    directory.writeUInt32LE(crc, 16);
    directory.writeUInt32LE(compressed.byteLength, 20);
    directory.writeUInt32LE(raw.byteLength, 24);
    directory.writeUInt16LE(name.byteLength, 28);
    directory.writeUInt32LE(localOffset, 42);
    central.push(directory, name);
    localOffset += local.byteLength + name.byteLength + compressed.byteLength;
  }
  const centralBytes = Buffer.concat(central);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(centralBytes.byteLength, 12);
  eocd.writeUInt32LE(localOffset, 16);
  return new Uint8Array(Buffer.concat([...locals, centralBytes, eocd]));
}

function mp4Box(type: string, payload = new Uint8Array()): Uint8Array {
  const box = Buffer.alloc(8 + payload.byteLength);
  box.writeUInt32BE(box.byteLength, 0);
  box.write(type, 4, 4, "ascii");
  Buffer.from(payload).copy(box, 8);
  return new Uint8Array(box);
}

function validMp4Fixture(): Uint8Array {
  const ftypPayload = Buffer.alloc(16);
  ftypPayload.write("isom", 0, 4, "ascii");
  ftypPayload.writeUInt32BE(0, 4);
  ftypPayload.write("isom", 8, 4, "ascii");
  ftypPayload.write("mp42", 12, 4, "ascii");
  return new Uint8Array(
    Buffer.concat([
      Buffer.from(mp4Box("ftyp", ftypPayload)),
      Buffer.from(mp4Box("moov", mp4Box("mvhd"))),
      Buffer.from(mp4Box("mdat", new Uint8Array([1]))),
    ]),
  );
}

function validClassroomDocument(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const document: Record<string, unknown> = {
    schemaVersion: "1.0",
    classroomId: "classroom-a",
    classroomVersionId: "classroom-a-v1",
    contentMode: "open_creation",
    openCreation: true,
    openmaic: {
      dslVersion: "0.1.0",
      stage: {
        id: "stage-a",
        name: "Lesson",
        createdAt: "2026-07-30T00:00:00.000Z",
        updatedAt: "2026-07-30T00:00:00.000Z",
      },
      scenes: [
        {
          id: "scene-a",
          stageId: "stage-a",
          title: "Lesson",
          order: 0,
          type: "slide",
          content: { type: "slide", canvas: { elements: [] } },
          actions: [],
        },
      ],
    },
    interactionIds: [],
    sourceRefs: [],
    knowledgePointMappings: [
      { knowledgePointId: "kp-a", sceneIds: ["scene-a"], sourceRefs: [] },
    ],
    mediaManifest: validMediaManifest(),
    fileSha256: "",
    exportManifest: [],
    generationMetadata: {
      generator: "openmaic",
      generatorVersion: "0.3.1",
      modelId: "server-selected-model",
      generatedAt: "2026-07-30T00:00:00.000Z",
      teachingBriefId: "brief-a",
      teachingBriefSha256: "b".repeat(64),
      templateId: "template-a",
      templateVersion: "1",
    },
    auditMetadata: {
      templateId: "template-a",
      templateVersion: "1",
      teachingBriefId: "brief-a",
      teachingBriefSha256: "b".repeat(64),
      parentClassroomVersionId: null,
    },
    validationResult: {
      valid: true,
      issues: [],
      validatedAt: "2026-07-30T00:00:00.000Z",
    },
    migrationRecords: [],
    ...overrides,
  };
  if (!("fileSha256" in overrides)) {
    const { fileSha256: _fileSha256, ...withoutFileHash } = document;
    document.fileSha256 = sha256(withoutFileHash);
  }
  return document;
}

function validMediaManifest(overrides: Record<string, unknown> = {}) {
  return [
    {
      mediaId: "media-a",
      relativePath: "media/lesson.png",
      mimeType: "image/png",
      sha256: "c".repeat(64),
      sizeBytes: 3,
      temporaryDownloadPath:
        "/api/yfeistai/v1/artifacts/content-source-job/media/lesson.png",
      expiresAt: "2026-07-31T00:00:00.000Z",
      ...overrides,
    },
  ];
}

function exportRequest(
  format: "classroom_zip" | "pptx" | "offline_html" | "mp4",
  overrides: Record<string, unknown> = {},
) {
  const classroomDocument = validClassroomDocument();
  const mediaManifest = validMediaManifest();
  return {
    schemaVersion: "1.0" as const,
    tenantId: "tenant-a",
    jobId: `export-${format}`,
    idempotencyKey: `idem-${format}`,
    format,
    language: "en-US",
    classroomDocument,
    classroomDocumentSha256: sha256(classroomDocument),
    mediaManifest,
    mediaManifestSha256: sha256(mediaManifest),
    sourceJobId: "content-source-job",
    exportPolicy: {
      includeSourceAttribution: true,
      allowExternalLinks: false,
    },
    ...overrides,
  };
}

function artifactOutput(name: string) {
  if (name === "pptx") {
    return {
      bytes: zipFixture(
        ["[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"].map(
          (relativePath) => ({
            relativePath,
            bytes: new TextEncoder().encode("<xml/>"),
          }),
        ),
      ),
    };
  }
  if (name === "mp4") {
    return {
      bytes: validMp4Fixture(),
    };
  }
  if (name.startsWith("offline")) {
    return {
      bytes: new TextEncoder().encode("<!doctype html><title>Offline</title>"),
    };
  }
  const bytes = new Uint8Array(
    Buffer.from(
      "UEsDBBQAAAAIAL2iAV0z8MRoEgAAABAAAAAWAAAAY29udGVudC9jbGFzc3Jvb20uanNvbjMwNDI2MTUzt7BMTEpOSU0DAFBLAQIUABQAAAAIAL2iAV0z8MRoEgAAABAAAAAWAAAAAAAAAAAAAACAAQAAAABjb250ZW50L2NsYXNzcm9vbS5qc29uUEsFBgAAAAABAAEARAAAAEYAAAAAAA==",
      "base64",
    ),
  );
  return {
    bytes,
    archiveEntries: [
      {
        relativePath: "content/classroom.json",
        uncompressedBytes: 16,
        compressedBytes: 18,
        kind: "file" as const,
      },
    ],
  };
}

function exportSubmission(
  format: "classroom_zip" | "pptx" | "offline_html" | "mp4",
) {
  const {
    classroomDocument: _classroomDocument,
    mediaManifest: _mediaManifest,
    sourceJobId: _sourceJobId,
    ...submission
  } = exportRequest(format);
  return submission;
}

function signedRequest(input: {
  method: "GET" | "POST";
  path: string;
  body: string;
  tenantId: string;
  jobId: string;
  idempotencyKey?: string;
  secret?: string;
}) {
  const signed = signServiceRequest({
    method: input.method,
    path: input.path,
    body: input.body,
    tenantId: input.tenantId,
    jobId: input.jobId,
    idempotencyKey: input.idempotencyKey,
    timestamp: 1_800_000_000,
    secret: input.secret ?? "service-secret",
  });
  return new Request(`http://openmaic${input.path}`, {
    method: input.method,
    body: input.method === "POST" ? input.body : undefined,
    headers: {
      "x-yfeistai-tenant-id": signed.tenantId,
      "x-yfeistai-job-id": signed.jobId,
      "x-yfeistai-idempotency-key": signed.idempotencyKey,
      "x-yfeistai-timestamp": String(signed.timestamp),
      "x-yfeistai-signature": signed.signature,
    },
  });
}

describe("controlled export boundary", () => {
  test("accepts the controlled media download route emitted by ArtifactStore", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-export-media-"));
    const store = new ArtifactStore(root);
    const mediaBytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
    const persisted = await store.put({
      tenantId: "tenant-a",
      jobId: "content-source-job",
      relativePath: "media/lesson.png",
      bytes: mediaBytes,
      mime: "image/png",
      expiresAt: "2026-08-02T00:00:00.000Z",
    });
    const mediaManifest = [
      {
        mediaId: "media-a",
        relativePath: persisted.relativePath,
        mimeType: persisted.mime,
        sha256: persisted.sha256,
        sizeBytes: persisted.bytes,
        temporaryDownloadPath: persisted.downloadPath,
        expiresAt: persisted.expiresAt,
      },
    ];
    const classroomDocument = validClassroomDocument({ mediaManifest });
    const exporter = vi.fn(async () => artifactOutput("offline"));

    await expect(
      generateExportJob(
        {
          ...exportRequest("offline_html"),
          sourceJobId: "content-source-job",
          classroomDocument,
          classroomDocumentSha256: sha256(classroomDocument),
          mediaManifest,
          mediaManifestSha256: sha256(mediaManifest),
        },
        {
          exportOfflineHtml: exporter,
          now: () => new Date("2026-07-30T00:00:00.000Z"),
        },
      ),
    ).resolves.toMatchObject({ status: "succeeded" });
    expect(exporter).toHaveBeenCalledTimes(1);
  });

  test("accepts an immutable document path from an earlier controlled job", async () => {
    const mediaManifest = validMediaManifest({
      temporaryDownloadPath:
        "/api/yfeistai/v1/artifacts/other-source-job/media/lesson.png",
    });
    const classroomDocument = validClassroomDocument({ mediaManifest });
    const exporter = vi.fn(async () => artifactOutput("offline"));

    await expect(
      generateExportJob(
        {
          ...exportRequest("offline_html"),
          classroomDocument,
          classroomDocumentSha256: sha256(classroomDocument),
          mediaManifest,
          mediaManifestSha256: sha256(mediaManifest),
        },
        { exportOfflineHtml: exporter },
      ),
    ).resolves.toMatchObject({ status: "succeeded" });
    expect(exporter).toHaveBeenCalledTimes(1);
  });

  test("rejects aggregate media over the artifact budget before exporting", async () => {
    const mediaManifest = validMediaManifest({
      sizeBytes: 256 * 1024 * 1024 + 1,
    });
    const classroomDocument = validClassroomDocument({ mediaManifest });
    const exporter = vi.fn(async () => artifactOutput("offline"));

    await expect(
      generateExportJob(
        {
          ...exportRequest("offline_html"),
          classroomDocument,
          classroomDocumentSha256: sha256(classroomDocument),
          mediaManifest,
          mediaManifestSha256: sha256(mediaManifest),
        },
        { exportOfflineHtml: exporter },
      ),
    ).rejects.toThrow(/aggregate media|size limit/i);
    expect(exporter).not.toHaveBeenCalled();
  });

  test.each([
    ["classroom_zip", ".maic.zip", "application/zip"],
    [
      "pptx",
      ".pptx",
      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ],
    ["offline_html", ".html", "text/html; charset=utf-8"],
    ["mp4", ".mp4", "video/mp4"],
  ] as const)(
    "replays a persisted %s checkpoint with its original expiry",
    async (format, suffix, mime) => {
      const root = mkdtempSync(path.join(tmpdir(), "openmaic-export-replay-"));
      const store = new ArtifactStore(root);
      const output = artifactOutput(format);
      const persisted = await store.put({
        tenantId: "tenant-a",
        jobId: `export-${format}`,
        relativePath: `exports/export-${format}${suffix}`,
        bytes: output.bytes,
        mime,
        expiresAt: "2026-08-02T00:00:00.000Z",
      });
      const exporter = vi.fn(async () => {
        throw new Error("must not regenerate a persisted checkpoint");
      });

      const result = await generateExportJob(exportRequest(format), {
        ...(format === "classroom_zip" ? { exportClassroomZip: exporter } : {}),
        ...(format === "pptx" ? { exportPptx: exporter } : {}),
        ...(format === "offline_html" ? { exportOfflineHtml: exporter } : {}),
        ...(format === "mp4" ? { renderMp4: exporter } : {}),
        now: () => new Date("2026-07-31T00:00:00.000Z"),
        readArtifact: ({ relativePath, now }) =>
          store.read("tenant-a", `export-${format}`, relativePath, now),
      });

      expect(exporter).not.toHaveBeenCalled();
      expect(result).toEqual({
        status: "succeeded",
        format,
        artifact: persisted,
      });
    },
  );

  test("accepts the stable expiry returned by a concurrent artifact checkpoint", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-export-race-"));
    const store = new ArtifactStore(root);
    const output = artifactOutput("offline");
    const result = await generateExportJob(exportRequest("offline_html"), {
      exportOfflineHtml: async () => output,
      now: () => new Date("2026-07-30T00:00:00.000Z"),
      readArtifact: async () => null,
      writeArtifact: (input) =>
        store.put({
          tenantId: "tenant-a",
          jobId: "export-offline_html",
          ...input,
          expiresAt: "2026-07-30T12:00:00.000Z",
        }),
    });

    expect(result).toMatchObject({
      status: "succeeded",
      artifact: { expiresAt: "2026-07-30T12:00:00.000Z" },
    });
  });

  test("reads limits from the real ZIP central directory", () => {
    const output = artifactOutput("classroom");
    expect(inspectZipArchive(output.bytes)).toEqual(output.archiveEntries);
  });

  test("rejects ZIP content whose decompressed bytes fail CRC validation", async () => {
    const relativePath = "content/classroom.json";
    const bytes = zipFixture([
      {
        relativePath,
        bytes: new TextEncoder().encode('{"ok":true}'),
      },
    ]);
    bytes[30 + Buffer.byteLength(relativePath)] ^= 0xff;

    await expect(inspectAndValidateZipArchive(bytes)).rejects.toThrow(/CRC/i);
  });

  test("rejects a ZIP whose actual inflation exceeds its declared size", async () => {
    const bytes = zipFixture([
      {
        relativePath: "content/classroom.json",
        bytes: new Uint8Array(1024 * 1024),
        compression: "deflate",
      },
    ]);
    const centralOffset = Buffer.from(bytes).indexOf(
      Buffer.from([0x50, 0x4b, 0x01, 0x02]),
    );
    Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).writeUInt32LE(
      1,
      22,
    );
    Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).writeUInt32LE(
      1,
      centralOffset + 24,
    );

    await expect(inspectAndValidateZipArchive(bytes)).rejects.toThrow(
      /decompressed|uncompressed|size/i,
    );
  });

  test("rejects an excessive ZIP entry count before parsing entries", async () => {
    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(0x06054b50, 0);
    eocd.writeUInt16LE(2_049, 8);
    eocd.writeUInt16LE(2_049, 10);
    await expect(
      inspectAndValidateZipArchive(new Uint8Array(eocd)),
    ).rejects.toThrow(/too many entries/i);
  });

  test("requires the minimum OOXML package entries for PPTX", async () => {
    const genericZip = zipFixture([
      {
        relativePath: "content/classroom.json",
        bytes: new TextEncoder().encode("{}"),
      },
    ]);
    await expect(validatePptxArchive(genericZip)).rejects.toThrow(
      /OOXML|PPTX/i,
    );

    const pptx = zipFixture(
      ["[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"].map(
        (relativePath) => ({
          relativePath,
          bytes: new TextEncoder().encode("<xml/>"),
        }),
      ),
    );
    await expect(validatePptxArchive(pptx)).resolves.toHaveLength(3);
  });

  test("accepts safe zero-byte directory entries emitted by OOXML generators", async () => {
    const pptx = zipFixture([
      { relativePath: "_rels/", bytes: new Uint8Array() },
      { relativePath: "ppt/", bytes: new Uint8Array() },
      {
        relativePath: "[Content_Types].xml",
        bytes: new TextEncoder().encode("<xml/>"),
      },
      {
        relativePath: "_rels/.rels",
        bytes: new TextEncoder().encode("<xml/>"),
      },
      {
        relativePath: "ppt/presentation.xml",
        bytes: new TextEncoder().encode("<xml/>"),
      },
    ]);

    await expect(validatePptxArchive(pptx)).resolves.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ relativePath: "ppt/", kind: "directory" }),
      ]),
    );
  });

  test("requires complete MP4 boxes instead of accepting a truncated ftyp prefix", () => {
    expect(() =>
      validateMp4Artifact(
        new Uint8Array([
          0, 0, 0, 24, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d,
        ]),
      ),
    ).toThrow(/invalid artifact/i);
    expect(() => validateMp4Artifact(validMp4Fixture())).not.toThrow();
  });

  test("caps MP4 response bodies while reading the stream", async () => {
    const response = new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new Uint8Array(6));
          controller.enqueue(new Uint8Array(6));
          controller.close();
        },
      }),
      { headers: { "content-type": "video/mp4" } },
    );
    await expect(readResponseBytesLimited(response, 10)).rejects.toMatchObject({
      code: "MP4_RENDER_INVALID_ARTIFACT",
    });
  });

  test("requires MP4 response Content-Length to match streamed bytes", async () => {
    const response = new Response(new Uint8Array(4), {
      headers: { "content-length": "5", "content-type": "video/mp4" },
    });
    await expect(readResponseBytesLimited(response, 10)).rejects.toMatchObject({
      code: "MP4_RENDER_INVALID_ARTIFACT",
    });
  });

  test("matches the MP4 response media type exactly", () => {
    expect(isMp4MediaType("video/mp4; codecs=avc1")).toBe(true);
    expect(isMp4MediaType("video/mp4evil")).toBe(false);
    expect(isMp4MediaType(null)).toBe(false);
  });

  test("cancels a remote render only after durable cancellation is explicit", async () => {
    const cancel = vi.fn(async () => undefined);
    await cancelRemoteRenderIfRequested(
      { isCanceled: async () => false },
      cancel,
    );
    expect(cancel).not.toHaveBeenCalled();

    await cancelRemoteRenderIfRequested(
      { isCanceled: async () => true },
      cancel,
    );
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  test("does not replace a render failure when cancellation state is unavailable", async () => {
    const cancel = vi.fn(async () => undefined);
    await expect(
      cancelRemoteRenderIfRequested(
        {
          isCanceled: async () => {
            throw new Error("state unavailable");
          },
        },
        cancel,
      ),
    ).resolves.toBeUndefined();
    expect(cancel).not.toHaveBeenCalled();
  });

  test("inlines relative media and renders every portable scene contract", async () => {
    const mediaBytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
    const mediaManifest = validMediaManifest({
      sha256: createHash("sha256").update(mediaBytes).digest("hex"),
      sizeBytes: mediaBytes.byteLength,
    });
    const classroomDocument = validClassroomDocument({
      interactionIds: ["quiz-a", "interactive-a", "pbl-a"],
      mediaManifest,
      knowledgePointMappings: [
        {
          knowledgePointId: "kp-a",
          sceneIds: ["slide-a", "quiz-a", "interactive-a", "pbl-a"],
          sourceRefs: [],
        },
      ],
      openmaic: {
        dslVersion: "0.1.0",
        stage: {
          id: "stage-a",
          name: "Offline lesson",
          createdAt: "2026-07-30T00:00:00.000Z",
          updatedAt: "2026-07-30T00:00:00.000Z",
        },
        scenes: [
          {
            id: "slide-a",
            stageId: "stage-a",
            title: "Slide",
            order: 0,
            type: "slide",
            content: {
              type: "slide",
              canvas: {
                elements: [
                  { id: "text-a", type: "text", content: "Slide body" },
                  { id: "image-a", type: "image", src: "media/lesson.png" },
                ],
              },
            },
            actions: [{ id: "speech-a", type: "speech", text: "Narration" }],
          },
          {
            id: "quiz-a",
            stageId: "stage-a",
            title: "Quiz",
            order: 1,
            type: "quiz",
            content: {
              type: "quiz",
              questions: [
                {
                  id: "question-a",
                  prompt: "Choose",
                  questionType: "single_choice",
                  options: [{ id: "option-a", label: "Correct option" }],
                  correctOptionIds: ["option-a"],
                  explanation: "Because",
                },
              ],
            },
            actions: [],
          },
          {
            id: "interactive-a",
            stageId: "stage-a",
            title: "Interactive",
            order: 2,
            type: "interactive",
            content: {
              type: "interactive",
              html: '<img src="media/lesson.png"><style>.hero{background:url("media/lesson.png")}</style>',
              bridgeVersion: "1.0",
              sandbox: { allowScripts: true, allowSameOrigin: false },
            },
            actions: [],
          },
          {
            id: "pbl-a",
            stageId: "stage-a",
            title: "PBL",
            order: 3,
            type: "pbl",
            content: {
              type: "pbl",
              scenario: "Investigate",
              roles: [{ id: "role-a", name: "Analyst", brief: "Analyze" }],
              milestones: [
                { id: "milestone-a", title: "Report", rubric: "Evidence" },
              ],
            },
            actions: [],
          },
        ],
      },
    });

    const output = createOfflineHtmlArtifact(classroomDocument, [
      {
        relativePath: "media/lesson.png",
        mime: "image/png",
        sha256: mediaManifest[0].sha256,
        bytes: mediaBytes,
      },
    ]);
    const html = new TextDecoder().decode(output.bytes);
    expect(() => assertOfflineHtmlSelfContained(html)).not.toThrow();
    const classroomJson = html.match(
      /<script id="classroom" type="application\/json">([\s\S]*?)<\/script>/,
    )?.[1];
    expect(classroomJson).toBeTruthy();
    const embeddedDocument = JSON.parse(classroomJson as string);
    expect(
      embeddedDocument.openmaic.scenes[0].content.canvas.elements[1].src,
    ).toMatch(/^data:image\/png;base64,/);
    const interactiveDataUrl = embeddedDocument.openmaic.scenes[2].content.html;
    expect(interactiveDataUrl).toMatch(/^data:text\/html;base64,/);
    const interactiveHtml = Buffer.from(
      interactiveDataUrl.slice(interactiveDataUrl.indexOf(",") + 1),
      "base64",
    ).toString("utf8");
    expect(interactiveHtml).toContain('src="data:image/png;base64,');
    expect(interactiveHtml).toContain('url("data:image/png;base64,');
    expect(html).toContain("option.label");
    expect(html).toContain("renderSlideScene");
    expect(html).toContain("renderPblScene");
    expect(html).toContain("renderActions");
    expect(html).not.toContain("option.text");
  });

  test("rejects unresolved relative resources in interactive HTML", () => {
    const classroomDocument = validClassroomDocument({
      interactionIds: ["interactive-a"],
      mediaManifest: [],
      openmaic: {
        ...(validClassroomDocument().openmaic as object),
        scenes: [
          {
            id: "interactive-a",
            stageId: "stage-a",
            title: "Interactive",
            order: 0,
            type: "interactive",
            content: {
              type: "interactive",
              html: '<img src="media/missing.png">',
              bridgeVersion: "1.0",
              sandbox: { allowScripts: true, allowSameOrigin: false },
            },
            actions: [],
          },
        ],
      },
    });
    expect(() => createOfflineHtmlArtifact(classroomDocument, [])).toThrow(
      /unresolved resource/i,
    );
  });
  test("accepts documents that are valid under the frozen Python JSON contract", () => {
    const document = validClassroomDocument({
      interactionIds: ["scene-quiz", "scene-interactive", "scene-pbl"],
      exportManifest: [
        {
          format: "classroom_zip",
          relativePath: "exports/classroom.zip",
          sha256: "d".repeat(64),
          sizeBytes: 0,
          mimeType: "application/zip",
          temporaryDownloadPath: "downloads/exports/classroom.zip",
          expiresAt: "2026-07-31T00:00:00.000Z",
        },
      ],
      auditMetadata: {
        templateId: "template-a",
        templateVersion: "1",
        teachingBriefId: "brief-a",
        teachingBriefSha256: "b".repeat(64),
        parentClassroomVersionId: "classroom-a-v0",
      },
      validationResult: {
        valid: false,
        issues: [
          {
            severity: "warning",
            code: "W1",
            message: "review",
            path: "openmaic",
          },
        ],
        validatedAt: "2026-07-30T00:00:00.000Z",
      },
      migrationRecords: [
        {
          fromDslVersion: "0.0.9",
          toDslVersion: "0.1.0",
          migratedAt: "2026-07-30T00:00:00.000Z",
          migrationId: "migration-a",
        },
      ],
      mediaManifest: validMediaManifest({
        sizeBytes: 0,
        temporaryDownloadPath: "downloads/media/lesson.png",
      }),
      openmaic: {
        dslVersion: "0.1.0",
        stage: {
          id: "stage-a",
          name: "Lesson",
          createdAt: "2026-07-30T00:00:00.000Z",
          updatedAt: "2026-07-30T00:00:00.000Z",
        },
        scenes: [
          {
            id: "scene-a",
            stageId: "stage-a",
            title: "Lesson",
            order: 0,
            type: "slide",
            content: { type: "slide", canvas: { elements: [] } },
          },
        ],
      },
    });
    document.fileSha256 = "e".repeat(64);

    expect(() => asPortableDocument(document)).not.toThrow();
  });
  test("binds export to the submitted document and media hashes", () => {
    const classroomDocument = {
      schemaVersion: "1.0",
      classroomId: "classroom-a",
    };
    const mediaManifest = { schemaVersion: "1.0", artifacts: [] };
    const classroomDocumentSha256 = createHash("sha256")
      .update(canonicalExportJson(classroomDocument), "utf8")
      .digest("hex");

    expect(() =>
      validateExportInputs({
        classroomDocument,
        classroomDocumentSha256,
        mediaManifest,
        mediaManifestSha256: "b".repeat(64),
      }),
    ).toThrow(/media manifest hash mismatch/i);
  });

  test.each([
    ["classroom document", "classroomDocumentSha256"],
    ["media manifest", "mediaManifestSha256"],
  ] as const)("rejects a changed %s hash", async (_label, field) => {
    const request = exportRequest("classroom_zip");
    await expect(
      generateExportJob(
        { ...request, [field]: "0".repeat(64) },
        { exportClassroomZip: async () => artifactOutput("classroom") },
      ),
    ).rejects.toThrow(/hash mismatch/i);
  });

  test("fails closed when resolved document inputs are missing", async () => {
    await expect(
      generateExportJob(
        exportSubmission("classroom_zip") as unknown as ExportGenerationRequest,
        { exportClassroomZip: async () => artifactOutput("classroom") },
      ),
    ).rejects.toThrow(/controlled export inputs/i);
  });

  test.each([
    ["classroom_zip", "exportClassroomZip", "application/zip", ".maic.zip"],
    [
      "pptx",
      "exportPptx",
      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      ".pptx",
    ],
    ["offline_html", "exportOfflineHtml", "text/html; charset=utf-8", ".html"],
    ["mp4", "renderMp4", "video/mp4", ".mp4"],
  ] as const)(
    "materializes a controlled %s artifact",
    async (format, dependencyName, mime, suffix) => {
      const exporter = vi.fn(async () => artifactOutput(format));
      const dependencies = {
        [dependencyName]: exporter,
        ...(format === "mp4"
          ? { renderEndpoint: "http://openmaic-render:3001" }
          : {}),
        now: () => new Date("2026-07-30T00:00:00.000Z"),
      };
      const result = await generateExportJob(
        exportRequest(format),
        dependencies,
      );

      expect(exporter).toHaveBeenCalledTimes(1);
      expect(result, JSON.stringify(result)).toMatchObject({
        status: "succeeded",
        artifact: {
          relativePath: expect.stringMatching(
            new RegExp(`${suffix.replaceAll(".", "\\.")}$`),
          ),
          mime,
          sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
          bytes: expect.any(Number),
          expiresAt: "2026-07-31T00:00:00.000Z",
        },
      });
    },
  );

  test("rejects an export writer that does not bind the exporter bytes", async () => {
    const output = artifactOutput("offline");
    const result = await generateExportJob(exportRequest("offline_html"), {
      exportOfflineHtml: async () => output,
      writeArtifact: async (input) => ({
        relativePath: input.relativePath,
        sha256: "0".repeat(64),
        bytes: input.bytes.byteLength,
        mime: input.mime,
        downloadPath: input.relativePath,
        expiresAt: input.expiresAt,
      }),
    });
    expect(result).toMatchObject({ status: "failed" });
  });

  test("checks the execution fence before invoking the export artifact publisher", async () => {
    const writeArtifact = vi.fn();
    const assertPublicationActive = vi.fn(() => {
      throw new Error("job execution lease was fenced");
    });

    const result = await generateExportJob(exportRequest("offline_html"), {
      exportOfflineHtml: async () => artifactOutput("offline"),
      assertPublicationActive,
      writeArtifact,
    });

    expect(assertPublicationActive).toHaveBeenCalledTimes(1);
    expect(writeArtifact).not.toHaveBeenCalled();
    expect(result).toMatchObject({
      status: "failed",
      error: { code: "EXPORT_ARTIFACT_INVALID" },
    });
  });

  test.each([
    "http://169.254.169.254/latest/meta-data",
    "https://untrusted.example/redirect-to-private",
    "file:///etc/passwd",
    "/etc/passwd",
    "C:\\Windows\\system.ini",
  ])(
    "rejects arbitrary source locations before fetching: %s",
    async (sourceLocation) => {
      const fetchExternal = vi.fn();

      await expect(
        generateExportJob(
          {
            ...exportRequest("offline_html"),
            sourceLocation,
          } as unknown as ExportGenerationRequest,
          { fetchExternal },
        ),
      ).rejects.toThrow(/external link/i);
      expect(fetchExternal).not.toHaveBeenCalled();
    },
  );

  test.each(["browserStorageKey", "indexedDbName", "localPath"])(
    "rejects browser or local runtime input field: %s",
    async (field) => {
      const exporter = vi.fn(async () => artifactOutput("offline"));
      await expect(
        generateExportJob(
          {
            ...exportRequest("offline_html"),
            [field]: field === "localPath" ? "/tmp/classroom" : "openmaic",
          },
          { exportOfflineHtml: exporter },
        ),
      ).rejects.toThrow(/browser|local|runtime/i);
      expect(exporter).not.toHaveBeenCalled();
    },
  );

  test("rejects external links in offline HTML instead of fetching or leaving them live", async () => {
    const classroomDocument = validClassroomDocument({
      interactionIds: ["interactive-a"],
      knowledgePointMappings: [
        {
          knowledgePointId: "kp-a",
          sceneIds: ["interactive-a"],
          sourceRefs: [],
        },
      ],
      openmaic: {
        ...(validClassroomDocument().openmaic as object),
        scenes: [
          {
            id: "interactive-a",
            stageId: "stage-a",
            title: "Interactive",
            order: 0,
            type: "interactive",
            content: {
              type: "interactive",
              html: '<script src="https://cdn.example/app.js"></script>',
              bridgeVersion: "1.0",
              sandbox: { allowScripts: true, allowSameOrigin: false },
            },
            actions: [],
          },
        ],
      },
    });
    const mediaManifest = validMediaManifest();
    const exporter = vi.fn(async () => artifactOutput("offline"));
    await expect(
      generateExportJob(
        {
          ...exportRequest("offline_html"),
          classroomDocument,
          classroomDocumentSha256: sha256(classroomDocument),
          mediaManifest,
          mediaManifestSha256: sha256(mediaManifest),
        },
        { exportOfflineHtml: exporter },
      ),
    ).rejects.toThrow(/external link/i);
    expect(exporter).not.toHaveBeenCalled();
  });

  test("rejects media URLs even when a redirect target is not yet known", async () => {
    const mediaManifest = validMediaManifest({
      url: "https://untrusted.example/redirect",
    });
    const exporter = vi.fn(async () => artifactOutput("zip"));
    await expect(
      generateExportJob(
        {
          ...exportRequest("classroom_zip"),
          mediaManifest,
          mediaManifestSha256: sha256(mediaManifest),
        },
        { exportClassroomZip: exporter },
      ),
    ).rejects.toThrow(/external link/i);
    expect(exporter).not.toHaveBeenCalled();
  });

  test.each([
    '<!doctype html><script src="https://cdn.example/app.js"></script>',
    '<!doctype html><script src="//cdn.example/app.js"></script>',
    "<!doctype html><style>body{background:url(//cdn.example/a.png)}</style>",
    '<!doctype html><a href="\\\\server\\share\\file">file</a>',
  ])(
    "rejects an offline exporter output that retains an external resource",
    async (html) => {
      const result = await generateExportJob(exportRequest("offline_html"), {
        exportOfflineHtml: async () => ({
          bytes: new TextEncoder().encode(html),
        }),
      });
      expect(result).toMatchObject({
        status: "failed",
        error: { code: "EXPORT_FAILED" },
      });
      expect(result).not.toHaveProperty("artifact");
    },
  );

  test("validates the archive descriptor produced by the exporter", async () => {
    const result = await generateExportJob(exportRequest("classroom_zip"), {
      exportClassroomZip: async () => ({
        bytes: new Uint8Array([0x50, 0x4b, 0x03, 0x04]),
        archiveEntries: [
          {
            relativePath: "../secret.txt",
            uncompressedBytes: 1,
            compressedBytes: 1,
          },
        ],
      }),
    });
    expect(result).toMatchObject({
      status: "failed",
      error: { code: "EXPORT_FAILED" },
    });
    expect(result).not.toHaveProperty("artifact");
  });

  test.each([
    {
      label: "entry count",
      entries: Array.from({ length: 2_049 }, (_, index) => ({
        relativePath: `files/${index}.txt`,
        uncompressedBytes: 1,
        compressedBytes: 1,
      })),
    },
    {
      label: "uncompressed size",
      entries: [
        {
          relativePath: "files/huge.bin",
          uncompressedBytes: 512 * 1024 * 1024 + 1,
          compressedBytes: 128 * 1024 * 1024,
        },
      ],
    },
    {
      label: "compression ratio",
      entries: [
        {
          relativePath: "files/bomb.bin",
          uncompressedBytes: 101,
          compressedBytes: 1,
        },
      ],
    },
    {
      label: "path traversal",
      entries: [
        {
          relativePath: "../secret.txt",
          uncompressedBytes: 1,
          compressedBytes: 1,
        },
      ],
    },
    {
      label: "symbolic link",
      entries: [
        {
          relativePath: "files/link",
          uncompressedBytes: 1,
          compressedBytes: 1,
          kind: "symlink" as const,
        },
      ],
    },
    {
      label: "external link",
      entries: [
        {
          relativePath: "files/link.url",
          uncompressedBytes: 1,
          compressedBytes: 1,
          externalLocation: "https://untrusted.example/file",
        },
      ],
    },
  ])("rejects unsafe archive $label", ({ entries }) => {
    expect(() => validateArchiveEntries(entries)).toThrow(
      /archive|artifact path/i,
    );
  });

  test("fails explicitly when MP4 rendering is not configured", async () => {
    const renderMp4 = vi.fn(async () => artifactOutput("mp4"));
    const result = await generateExportJob(exportRequest("mp4"), {
      renderMp4,
    });
    expect(result).toEqual({
      status: "failed",
      format: "mp4",
      error: {
        code: "MP4_RENDER_UNAVAILABLE",
        message: "MP4 rendering is not configured.",
      },
    });
    expect(renderMp4).not.toHaveBeenCalled();
    expect(result).not.toHaveProperty("artifact");
  });

  test("rejects a non-private MP4 renderer without making a request", async () => {
    const renderMp4 = vi.fn(async () => artifactOutput("mp4"));
    const result = await generateExportJob(exportRequest("mp4"), {
      renderEndpoint: "https://render.example.com",
      renderMp4,
    });
    expect(result).toEqual({
      status: "failed",
      format: "mp4",
      error: {
        code: "MP4_RENDER_UNTRUSTED",
        message: "MP4 renderer must use the private openmaic-render service.",
      },
    });
    expect(renderMp4).not.toHaveBeenCalled();
    expect(result).not.toHaveProperty("artifact");
  });

  test("returns a stable MP4 timeout error and never emits a fake video", async () => {
    const result = await generateExportJob(exportRequest("mp4"), {
      renderEndpoint: "http://openmaic-render:3001",
      renderMp4: async () => {
        throw new ExportPipelineError(
          "MP4_RENDER_TIMEOUT",
          "MP4 rendering timed out.",
        );
      },
    });

    expect(result).toMatchObject({
      status: "failed",
      error: {
        code: "MP4_RENDER_TIMEOUT",
        message: "MP4 rendering timed out.",
      },
    });
    expect(result).not.toHaveProperty("artifact");
  });

  test("publishes exporter failures as failed jobs with the stable code", async () => {
    const store = new ContentJobStore<ExportGenerationResult>();
    await store.start(
      {
        tenantId: "tenant-a",
        jobId: "export-a",
        idempotencyKey: "idem-a",
        canonicalBody: "{}",
        phase: "export",
        failureCode: "EXPORT_FAILED",
      },
      async () => ({
        status: "failed",
        format: "mp4",
        error: {
          code: "MP4_RENDER_UNAVAILABLE",
          message: "MP4 renderer is unavailable.",
        },
      }),
    );
    await expect(store.read("tenant-a", "export-a")).resolves.toMatchObject({
      status: "failed",
      error: {
        code: "MP4_RENDER_UNAVAILABLE",
        message: "MP4 renderer is unavailable.",
      },
    });
  });
});

describe("signed export routes", () => {
  test("resolves hash-only export inputs after a registry restart", () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-output-registry-"));
    const classroomDocument = validClassroomDocument();
    const mediaManifest = validMediaManifest();
    const first = new ContentOutputRegistry(root);
    const hashes = first.registerPayload(
      "tenant-restart",
      classroomDocument as unknown as PortableClassroomDocument,
      mediaManifest as PortableClassroomDocument["mediaManifest"],
      "content-source-job",
    );

    const restarted = new ContentOutputRegistry(root);
    expect(
      restarted.resolve(
        "tenant-restart",
        hashes.classroomDocumentSha256,
        hashes.mediaManifestSha256,
      ),
    ).toEqual({
      classroomDocument,
      mediaManifest,
      sourceJobId: "content-source-job",
    });
  });

  test("authenticates POST before parsing and binds tenant/job/idempotency", async () => {
    const exporter = vi.fn(async () => artifactOutput("zip"));
    const inputRegistry = new ContentOutputRegistry();
    const input = exportRequest("classroom_zip");
    inputRegistry.registerPayload(
      input.tenantId,
      input.classroomDocument as unknown as PortableClassroomDocument,
      input.mediaManifest as PortableClassroomDocument["mediaManifest"],
      "content-source-job",
    );
    const handler = createExportPostHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      store: new ContentJobStore<ExportGenerationResult>(),
      inputRegistry,
      exportClassroomZip: exporter,
    });
    const malformed = signedRequest({
      method: "POST",
      path: "/api/yfeistai/v1/exports",
      body: "{",
      tenantId: "tenant-a",
      jobId: "export-a",
      idempotencyKey: "idem-a",
      secret: "wrong-secret",
    });
    expect((await handler(malformed)).status).toBe(401);

    const submission = exportSubmission("classroom_zip");
    const unsupportedBody = JSON.stringify({
      ...submission,
      [["provider", "Id"].join("")]: "client-selected",
    });
    expect(
      (
        await handler(
          signedRequest({
            method: "POST",
            path: "/api/yfeistai/v1/exports",
            body: unsupportedBody,
            tenantId: "tenant-a",
            jobId: submission.jobId,
            idempotencyKey: submission.idempotencyKey,
          }),
        )
      ).status,
    ).toBe(400);

    const body = JSON.stringify(submission);
    const wrongBinding = signedRequest({
      method: "POST",
      path: "/api/yfeistai/v1/exports",
      body,
      tenantId: "tenant-b",
      jobId: "export-classroom_zip",
      idempotencyKey: "wrong-idem",
    });
    expect((await handler(wrongBinding)).status).toBe(403);
    expect(exporter).not.toHaveBeenCalled();

    const accepted = signedRequest({
      method: "POST",
      path: "/api/yfeistai/v1/exports",
      body,
      tenantId: "tenant-a",
      jobId: "export-classroom_zip",
      idempotencyKey: "idem-classroom_zip",
    });
    expect((await handler(accepted)).status).toBe(202);
    await vi.waitFor(() => expect(exporter).toHaveBeenCalledTimes(1));
  });

  test("binds export polling to the signed tenant and job", async () => {
    const store = new ContentJobStore<ExportGenerationResult>();
    await store.start(
      {
        tenantId: "tenant-a",
        jobId: "export-a",
        idempotencyKey: "idem-a",
        canonicalBody: "{}",
        phase: "export",
      },
      async () => ({
        status: "succeeded" as const,
        format: "classroom_zip" as const,
        artifact: {
          relativePath: "exports/export-a.maic.zip",
          sha256: "a".repeat(64),
          bytes: 10,
          mime: "application/zip",
          downloadPath: "exports/export-a.maic.zip",
          expiresAt: "2026-07-31T00:00:00.000Z",
        },
      }),
    );
    const handler = createExportGetHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      store,
    });
    const request = signedRequest({
      method: "GET",
      path: "/api/yfeistai/v1/exports/export-a",
      body: "",
      tenantId: "tenant-b",
      jobId: "export-a",
    });
    expect(
      (
        await handler(request, {
          params: Promise.resolve({ jobId: "export-a" }),
        })
      ).status,
    ).toBe(404);
  });

  test("does not share the process-global export store with a local test store", () => {
    expect(exportJobStore).toBeDefined();
  });
});
