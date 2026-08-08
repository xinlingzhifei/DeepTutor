import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { describe, expect, test } from "vitest";

import { ArtifactStore } from "../../lib/yfeistai/artifact-manifest";
import { ContentOutputRegistry } from "../../lib/yfeistai/content-generation";
import { durableFile } from "../../lib/yfeistai/durable-state";
import {
  ExportInputStagingStore,
  type ExportInputDeclaration,
} from "../../lib/yfeistai/export-input-staging";
import { canonicalJson } from "../../lib/yfeistai/outline-generation";
import {
  asPortableDocument,
  type PortableClassroomDocument,
} from "../../lib/yfeistai/portable-classroom";

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function classroomDocument(): Record<string, unknown> {
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
    mediaManifest: [],
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
  };
  const { fileSha256: _ignored, ...withoutFileHash } = document;
  document.fileSha256 = sha256(canonicalJson(withoutFileHash));
  return document;
}

function fixture(jobId = "export-job-a") {
  const document = classroomDocument();
  const body = new TextEncoder().encode(canonicalJson(document));
  const declaration: ExportInputDeclaration = {
    schemaVersion: 1,
    tenantId: "tenant-a",
    jobId,
    idempotencyKey: `idem-${jobId}`,
    classroomDocumentSha256: sha256(body),
    mediaManifestSha256: sha256(canonicalJson([])),
    sourceManifestSha256: "c".repeat(64),
    files: [
      {
        fileId: "file-document",
        kind: "document",
        mediaId: null,
        relativePath: "classroom.json",
        mimeType: "application/json",
        sha256: sha256(body),
        sizeBytes: body.byteLength,
      },
    ],
  };
  return { body, declaration };
}

function stores() {
  const root = mkdtempSync(path.join(tmpdir(), "export-input-staging-"));
  const artifacts = new ArtifactStore(path.join(root, "artifacts"));
  const outputs = new ContentOutputRegistry(path.join(root, "state"));
  return {
    outputs,
    staging: new ExportInputStagingStore(
      path.join(root, "state"),
      artifacts,
      outputs,
      () => new Date("2026-08-04T00:00:00.000Z"),
    ),
  };
}

async function* chunks(value: Uint8Array) {
  const middle = Math.floor(value.byteLength / 2);
  yield value.slice(0, middle);
  yield value.slice(middle);
}

describe("export input staging", () => {
  test("accepts durable media entries without deprecated download metadata", () => {
    const document = classroomDocument();
    document.mediaManifest = [
      {
        mediaId: "media-a",
        relativePath: "media/media-a.png",
        mimeType: "image/png",
        sha256: "a".repeat(64),
        sizeBytes: 8,
      },
    ];

    expect(asPortableDocument(document).mediaManifest).toEqual(
      document.mediaManifest,
    );
  });

  test("rejects explicit null for optional media download metadata", () => {
    const document = classroomDocument();
    document.mediaManifest = [
      {
        mediaId: "media-a",
        relativePath: "media/media-a.png",
        mimeType: "image/png",
        sha256: "a".repeat(64),
        sizeBytes: 8,
        temporaryDownloadPath: null,
      },
    ];

    expect(() => asPortableDocument(document)).toThrow(/non-empty string/i);
  });

  test("keeps export manifest download metadata strict", () => {
    const document = classroomDocument();
    document.exportManifest = [
      {
        format: "pptx",
        relativePath: "exports/classroom.pptx",
        sha256: "a".repeat(64),
        sizeBytes: 8,
        mimeType:
          "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      },
    ];

    expect(() => asPortableDocument(document)).toThrow(/invalid field set/i);
  });

  test("commits an immutable input before making it visible to export", async () => {
    const { outputs, staging } = stores();
    const { body, declaration } = fixture();
    const reservation = staging.reserve(declaration);

    await staging.upload(
      declaration.tenantId,
      declaration.jobId,
      declaration.idempotencyKey,
      declaration.files[0].fileId,
      declaration.files[0].mimeType,
      chunks(body),
    );
    const receipt = await staging.commit(
      declaration.tenantId,
      declaration.jobId,
      declaration.idempotencyKey,
      reservation.declarationSha256,
    );
    const replay = await staging.commit(
      declaration.tenantId,
      declaration.jobId,
      declaration.idempotencyKey,
      reservation.declarationSha256,
    );

    expect(replay).toEqual(receipt);
    expect(
      outputs.resolve(
        declaration.tenantId,
        declaration.classroomDocumentSha256,
        declaration.mediaManifestSha256,
        declaration.jobId,
      ),
    ).toMatchObject({ sourceJobId: declaration.jobId });
  });

  test("binds the same immutable input to independent export jobs", async () => {
    const { outputs, staging } = stores();
    const first = fixture("export-job-first");
    const second = fixture("export-job-second");

    for (const current of [first, second]) {
      const reservation = staging.reserve(current.declaration);
      await staging.upload(
        current.declaration.tenantId,
        current.declaration.jobId,
        current.declaration.idempotencyKey,
        current.declaration.files[0].fileId,
        current.declaration.files[0].mimeType,
        chunks(current.body),
      );
      await staging.commit(
        current.declaration.tenantId,
        current.declaration.jobId,
        current.declaration.idempotencyKey,
        reservation.declarationSha256,
      );
    }

    expect(
      outputs.resolve(
        first.declaration.tenantId,
        first.declaration.classroomDocumentSha256,
        first.declaration.mediaManifestSha256,
        first.declaration.jobId,
      ),
    ).toMatchObject({ sourceJobId: first.declaration.jobId });
    expect(
      outputs.resolve(
        second.declaration.tenantId,
        second.declaration.classroomDocumentSha256,
        second.declaration.mediaManifestSha256,
        second.declaration.jobId,
      ),
    ).toMatchObject({ sourceJobId: second.declaration.jobId });
  });

  test("does not conflict with the generation registry for the same hashes", async () => {
    const { outputs, staging } = stores();
    const current = fixture("export-job-after-generation");
    const generated = classroomDocument() as unknown as PortableClassroomDocument;
    outputs.registerPayload(
      current.declaration.tenantId,
      generated,
      generated.mediaManifest,
      "generation-job",
    );
    const reservation = staging.reserve(current.declaration);
    await staging.upload(
      current.declaration.tenantId,
      current.declaration.jobId,
      current.declaration.idempotencyKey,
      current.declaration.files[0].fileId,
      current.declaration.files[0].mimeType,
      chunks(current.body),
    );

    await staging.commit(
      current.declaration.tenantId,
      current.declaration.jobId,
      current.declaration.idempotencyKey,
      reservation.declarationSha256,
    );

    expect(
      outputs.resolve(
        current.declaration.tenantId,
        current.declaration.classroomDocumentSha256,
        current.declaration.mediaManifestSha256,
      ),
    ).toMatchObject({ sourceJobId: "generation-job" });
    expect(
      outputs.resolve(
        current.declaration.tenantId,
        current.declaration.classroomDocumentSha256,
        current.declaration.mediaManifestSha256,
        current.declaration.jobId,
      ),
    ).toMatchObject({ sourceJobId: current.declaration.jobId });
    expect(
      outputs.resolve(
        current.declaration.tenantId,
        current.declaration.classroomDocumentSha256,
        current.declaration.mediaManifestSha256,
        "other-export-job",
      ),
    ).toBeNull();
  });

  test("rejects a bound record whose media source job was tampered", () => {
    const root = mkdtempSync(path.join(tmpdir(), "export-input-binding-"));
    const outputs = new ContentOutputRegistry(root);
    const document = classroomDocument() as unknown as PortableClassroomDocument;
    const hashes = outputs.registerPayload(
      "tenant-a",
      document,
      document.mediaManifest,
      "export-job-a",
      "export-job-a",
    );
    const target = durableFile(
      root,
      "content-outputs",
      "payloads",
      [
        "tenant-a",
        hashes.classroomDocumentSha256,
        hashes.mediaManifestSha256,
        "export-job-a",
      ],
      "payload.json",
    );
    const record = JSON.parse(readFileSync(target, "utf8")) as {
      output: { sourceJobId: string };
    };
    record.output.sourceJobId = "other-export-job";
    writeFileSync(target, canonicalJson(record), "utf8");

    expect(() =>
      outputs.resolve(
        "tenant-a",
        hashes.classroomDocumentSha256,
        hashes.mediaManifestSha256,
        "export-job-a",
      ),
    ).toThrow(/integrity/i);
  });

  test("rejects mismatched streamed bytes without publishing an input", async () => {
    const { outputs, staging } = stores();
    const { body, declaration } = fixture("export-job-b");
    staging.reserve(declaration);
    const tampered = body.slice();
    tampered[tampered.byteLength - 1] ^= 1;

    await expect(
      staging.upload(
        declaration.tenantId,
        declaration.jobId,
        declaration.idempotencyKey,
        declaration.files[0].fileId,
        declaration.files[0].mimeType,
        chunks(tampered),
      ),
    ).rejects.toThrow(/integrity/i);
    expect(
      outputs.resolve(
        declaration.tenantId,
        declaration.classroomDocumentSha256,
        declaration.mediaManifestSha256,
        declaration.jobId,
      ),
    ).toBeNull();
  });
});
