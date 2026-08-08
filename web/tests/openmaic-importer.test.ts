import assert from "node:assert/strict";
import test from "node:test";
import type { Slide } from "@openmaic/dsl";

import {
  OPENMAIC_IMPORTER_VENDOR_URL,
  createImporterLoader,
  loadOpenMaicImporterFromVendor,
  mergeImportedSlides,
  mergeImportedSlidesIntoHistory,
  type ImportedMedia,
  type OpenMaicImporterModule,
} from "../lib/openmaic-adapter/importer";
import type { ClassroomDocument } from "../lib/openmaic-adapter/contracts";
import { createHistory, undo } from "../lib/openmaic-adapter/editor-history";

const SHA256 = "a".repeat(64);
const NOW = "2026-07-30T12:00:00+08:00";

function slideFixture(overrides: Partial<Slide> = {}): Slide {
  return {
    id: "upstream-slide-1",
    viewportSize: 1_000,
    viewportRatio: 9 / 16,
    theme: {
      backgroundColor: "#ffffff",
      themeColors: ["#111111", "#ffffff"],
      fontColor: "#111111",
      fontName: "Arial",
    },
    elements: [],
    ...overrides,
  };
}

function classroomFixture(): ClassroomDocument {
  return {
    schemaVersion: "1.0",
    classroomId: "classroom-1",
    classroomVersionId: "version-1",
    contentMode: "open_creation",
    openCreation: true,
    openmaic: {
      dslVersion: "0.1.0",
      stage: { id: "stage-1", name: "Editor", createdAt: NOW, updatedAt: NOW },
      scenes: [{
        id: "scene-existing",
        stageId: "stage-1",
        title: "Existing",
        order: 0,
        type: "slide",
        content: {
          type: "slide",
          canvas: {
            id: "canvas-existing",
            viewportSize: 1_000,
            viewportRatio: 9 / 16,
            elements: [],
          },
        },
        actions: [],
      }],
    },
    interactionIds: [],
    sourceRefs: [],
    knowledgePointMappings: [{
      knowledgePointId: "kp-existing",
      sceneIds: ["scene-existing"],
      sourceRefs: [],
    }],
    mediaManifest: [{
      mediaId: "media-existing",
      relativePath: "media/media-existing.png",
      mimeType: "image/png",
      sha256: "b".repeat(64),
      sizeBytes: 8,
    }],
    fileSha256: SHA256,
    exportManifest: [],
    generationMetadata: {
      generator: "test",
      generatorVersion: "1",
      modelId: "model",
      generatedAt: NOW,
      teachingBriefId: "brief-1",
      teachingBriefSha256: SHA256,
      templateId: "template-1",
      templateVersion: "1",
    },
    auditMetadata: {
      templateId: "template-1",
      templateVersion: "1",
      teachingBriefId: "brief-1",
      teachingBriefSha256: SHA256,
      parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: NOW },
    migrationRecords: [],
  };
}

function mediaReceipt(
  mediaId: string,
  relativePath = `media/${mediaId}.png`,
  mimeType = "image/png",
): ImportedMedia {
  return {
    mediaId,
    relativePath,
    readUrl: `/api/v1/classrooms/asset-1/draft-media/${mediaId}`,
    mimeType,
    sizeBytes: 5,
    sha256: SHA256,
  };
}

test("the browser importer is loaded only from the same-origin vendor URL", async () => {
  let importedUrl = "";
  let headInput: RequestInfo | URL | undefined;
  let headInit: RequestInit | undefined;
  const fakeModule: OpenMaicImporterModule = {
    async importPptx() {
      return [];
    },
  };

  const loaded = await loadOpenMaicImporterFromVendor(
    async (input, init) => {
      headInput = input;
      headInit = init;
      return new Response(null, {
        status: 200,
        headers: { "Content-Type": "text/javascript" },
      });
    },
    async url => {
      importedUrl = url;
      return fakeModule;
    },
  );

  assert.equal(loaded, fakeModule);
  assert.equal(headInput, OPENMAIC_IMPORTER_VENDOR_URL);
  assert.equal(importedUrl, OPENMAIC_IMPORTER_VENDOR_URL);
  assert.deepEqual(headInit, {
    method: "HEAD",
    cache: "no-store",
    credentials: "same-origin",
  });
});

test("a missing browser parser fails before evaluating a response body as code", async () => {
  let imported = false;
  await assert.rejects(
    loadOpenMaicImporterFromVendor(
      async () => new Response(null, { status: 404 }),
      async () => {
        imported = true;
        return {
          async importPptx() {
            return [];
          },
        };
      },
    ),
    /PPTX parser is unavailable/i,
  );
  assert.equal(imported, false);
});

function withBrowserWindow<T>(run: () => Promise<T>): Promise<T> {
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {},
  });
  return run().finally(() => {
    if (original) {
      Object.defineProperty(globalThis, "window", original);
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
  });
}

test("PPTX importer is loaded only after a browser action", async () => {
  let loadCount = 0;
  const fakeImporter: OpenMaicImporterModule = {
    async importPptx(_input, options) {
      assert.ok(options?.upload);
      const mediaUrl = await options.upload(
        new Blob(["image"], { type: "image/png" }),
        "slide.png",
        "images",
      );
      return [
        slideFixture({
          elements: [{
            id: "image-1",
            type: "image",
            left: 0,
            top: 0,
            width: 100,
            height: 100,
            rotate: 0,
            fixedRatio: true,
            src: mediaUrl,
          }],
        }),
      ];
    },
  };
  const load = createImporterLoader(async () => {
    loadCount += 1;
    return fakeImporter;
  });
  const uploaded: ImportedMedia = {
    mediaId: "media-1",
    relativePath: "media/media-1.png",
    readUrl: "/api/v1/classrooms/asset-1/draft-media/media-1",
    mimeType: "image/png",
    sizeBytes: 5,
    sha256: SHA256,
  };

  assert.equal(load.loaded, false);
  await assert.rejects(
    load.importPptx(new ArrayBuffer(8), async () => uploaded),
    /browser-only/i,
  );
  assert.equal(load.loaded, false, "SSR must not evaluate the importer module");
  assert.equal(loadCount, 0);

  const result = await withBrowserWindow(() =>
    load.importPptx(new ArrayBuffer(8), async () => uploaded),
  );

  assert.equal(load.loaded, true);
  assert.equal(loadCount, 1);
  assert.deepEqual(result, {
    slides: [slideFixture({
      elements: [{
        id: "image-1",
        type: "image",
        left: 0,
        top: 0,
        width: 100,
        height: 100,
        rotate: 0,
        fixedRatio: true,
        src: "/api/v1/classrooms/asset-1/draft-media/media-1",
      }],
    })],
    media: [uploaded],
  });
});

test("imported media must resolve through a controlled yFeiSTAI route", async () => {
  const load = createImporterLoader(async () => ({
    async importPptx(_input, options) {
      await options?.upload?.(new Blob(["image"]), "slide.png");
      return [];
    },
  }));

  await withBrowserWindow(() =>
    assert.rejects(
      load.importPptx(new ArrayBuffer(8), async () => ({
        mediaId: "media-1",
        relativePath: "media/media-1.png",
        readUrl: "https://files.openmaic.example/media-1",
        mimeType: "image/png",
        sizeBytes: 5,
        sha256: SHA256,
      })),
      /controlled yFeiSTAI media route/i,
    ),
  );
});

test("media ids cannot widen controlled-route matching", async () => {
  const load = createImporterLoader(async () => ({
    async importPptx(_input, options) {
      await options?.upload?.(new Blob(["image"]), "slide.png");
      return [];
    },
  }));

  await withBrowserWindow(() =>
    assert.rejects(
      load.importPptx(new ArrayBuffer(8), async () => ({
        mediaId: ".*",
        relativePath: "media/media-1.png",
        readUrl: "/api/v1/classrooms/asset-1/draft-media/other-media",
        mimeType: "image/png",
        sizeBytes: 5,
        sha256: SHA256,
      })),
      /controlled yFeiSTAI media route/i,
    ),
  );
});

test("upload fallbacks cannot leave data or blob media in imported slides", async () => {
  for (const fallbackUrl of ["data:image/png;base64,ZmFrZQ==", "blob:unsafe"]) {
    const load = createImporterLoader(async () => ({
      async importPptx(_input, options) {
        try {
          await options?.upload?.(new Blob(["image"]), "slide.png");
        } catch {
          // The upstream importer deliberately falls back to its local URL.
        }
        return [slideFixture({
          elements: [{
            id: "image-1",
            type: "image",
            left: 0,
            top: 0,
            width: 100,
            height: 100,
            rotate: 0,
            fixedRatio: true,
            src: fallbackUrl,
          }],
        })];
      },
    }));

    await withBrowserWindow(() =>
      assert.rejects(
        load.importPptx(new ArrayBuffer(8), async () => {
          throw new Error("upload failed");
        }),
        /uploaded through a controlled yFeiSTAI media route/i,
      ),
    );
  }
});

test("slides cannot invent controlled media URLs that were not uploaded", async () => {
  const load = createImporterLoader(async () => ({
    async importPptx() {
      return [slideFixture({
        background: {
          type: "image",
          image: {
            src: "/api/v1/classrooms/asset-1/draft-media/not-uploaded",
            size: "cover",
          },
        },
      })];
    },
  }));

  await withBrowserWindow(() =>
    assert.rejects(
      load.importPptx(new ArrayBuffer(8), async () => {
        throw new Error("unused");
      }),
      /uploaded through a controlled yFeiSTAI media route/i,
    ),
  );
});

test("the real OpenMAIC Slide media fields accept only exact upload receipts", async () => {
  const receipts = [
    mediaReceipt("image"),
    mediaReceipt("pattern"),
    mediaReceipt("video", "media/video.mp4", "video/mp4"),
    mediaReceipt("poster"),
    mediaReceipt("audio", "media/audio.mp3", "audio/mpeg"),
    mediaReceipt("background"),
  ];
  const load = createImporterLoader(async () => ({
    async importPptx(_input, options) {
      assert.ok(options?.upload);
      const urls: string[] = [];
      for (let index = 0; index < receipts.length; index += 1) {
        urls.push(await options.upload(new Blob(["media"]), `${index}.bin`));
      }
      return [slideFixture({
        elements: [
          {
            id: "image-element",
            type: "image",
            left: 0,
            top: 0,
            width: 100,
            height: 100,
            rotate: 0,
            fixedRatio: true,
            src: urls[0],
          },
          {
            id: "shape-element",
            type: "shape",
            left: 0,
            top: 0,
            width: 100,
            height: 100,
            rotate: 0,
            viewBox: [100, 100],
            path: "M0 0 H100 V100 H0 Z",
            fixedRatio: false,
            fill: "#ffffff",
            pattern: urls[1],
          },
          {
            id: "video-element",
            type: "video",
            left: 0,
            top: 0,
            width: 100,
            height: 100,
            rotate: 0,
            autoplay: false,
            src: urls[2],
            mediaRef: urls[2],
            poster: urls[3],
          },
          {
            id: "audio-element",
            type: "audio",
            left: 0,
            top: 0,
            width: 100,
            height: 100,
            rotate: 0,
            fixedRatio: true,
            color: "#111111",
            loop: false,
            autoplay: false,
            src: urls[4],
          },
        ],
        background: {
          type: "image",
          image: { src: urls[5], size: "cover" },
        },
      })];
    },
  }));
  let uploadIndex = 0;

  const result = await withBrowserWindow(() =>
    load.importPptx(new ArrayBuffer(8), async () => receipts[uploadIndex++]),
  );

  assert.equal(result.slides.length, 1);
  assert.deepEqual(result.media, receipts);
});

test("pattern, mediaRef, poster, and background references reject unuploaded values", async () => {
  const unsafeReferences = [
    ["pattern", "data:image/png;base64,ZmFrZQ=="],
    ["mediaRef", "blob:unsafe"],
    ["poster", "https://cdn.example/poster.png"],
    ["src", "tenant-a/private/object-key.png"],
  ] as const;

  for (const [field, value] of unsafeReferences) {
    const load = createImporterLoader(async () => ({
      async importPptx() {
        if (field === "src") {
          return [slideFixture({
            background: { type: "image", image: { src: value, size: "cover" } },
          })];
        }
        return [slideFixture({
          elements: [{
            id: "video-element",
            type: "video",
            left: 0,
            top: 0,
            width: 100,
            height: 100,
            rotate: 0,
            autoplay: false,
            [field]: value,
          }],
        })];
      },
    }));

    await withBrowserWindow(() =>
      assert.rejects(
        load.importPptx(new ArrayBuffer(8), async () => mediaReceipt("unused")),
        /uploaded through a controlled yFeiSTAI media route/i,
      ),
    );
  }
});

test("mergeImportedSlides adds portable slide scenes and only referenced media", () => {
  const original = classroomFixture();
  const referenced = mediaReceipt("referenced");
  const unused = mediaReceipt("unused");
  const slide = slideFixture({
    id: "upstream-canvas-id",
    sectionTag: { id: "section-1", title: "Imported title" },
    elements: [{
      id: "image-element",
      type: "image",
      left: 0,
      top: 0,
      width: 100,
      height: 100,
      rotate: 0,
      fixedRatio: true,
      src: referenced.readUrl,
    }],
    background: {
      type: "image",
      image: { src: referenced.readUrl, size: "cover" },
    },
  });
  const generated = new Map<string, number>();
  const idFactory = (prefix: string) => {
    const count = (generated.get(prefix) ?? 0) + 1;
    generated.set(prefix, count);
    if (count === 1) return `${prefix}-existing`;
    return `${prefix}-imported`;
  };

  const merged = mergeImportedSlides(original, [slide], [referenced, unused], idFactory);

  assert.notEqual(merged, original);
  assert.deepEqual(original.openmaic.scenes.map(scene => scene.id), ["scene-existing"]);
  assert.deepEqual(merged.openmaic.scenes.map(scene => scene.order), [0, 1]);
  assert.deepEqual(merged.knowledgePointMappings, original.knowledgePointMappings);
  const imported = merged.openmaic.scenes[1];
  assert.equal(imported.id, "scene-imported");
  assert.equal(imported.stageId, "stage-1");
  assert.equal(imported.title, "Imported title");
  assert.equal(imported.type, "slide");
  assert.deepEqual(imported.actions, []);
  assert.equal(imported.content.canvas.id, "canvas-imported");
  assert.equal(
    ((imported.content.canvas.elements as Array<{ src: string }>)[0]).src,
    referenced.relativePath,
  );
  assert.equal(
    ((imported.content.canvas.background as { image: { src: string } }).image.src),
    referenced.relativePath,
  );
  assert.deepEqual(merged.mediaManifest, [
    original.mediaManifest[0],
    {
      mediaId: referenced.mediaId,
      relativePath: referenced.relativePath,
      mimeType: referenced.mimeType,
      sha256: referenced.sha256,
      sizeBytes: referenced.sizeBytes,
    },
  ]);
  assert.doesNotMatch(JSON.stringify(merged), /draft-media|readUrl|expiresAt/);
});

test("one imported deck is one undoable editor history transaction", () => {
  const original = classroomFixture();
  const referenced = mediaReceipt("referenced");
  const slide = slideFixture({
    elements: [{
      id: "image-element",
      type: "image",
      left: 0,
      top: 0,
      width: 100,
      height: 100,
      rotate: 0,
      fixedRatio: true,
      src: referenced.readUrl,
    }],
  });
  let id = 0;

  const history = mergeImportedSlidesIntoHistory(
    createHistory(original),
    [slide],
    [referenced],
    prefix => `${prefix}-import-${++id}`,
  );

  assert.equal(history.past.length, 1);
  assert.equal(history.present.openmaic.scenes.length, 2);
  assert.deepEqual(undo(history).present, original);
});

test("mergeImportedSlides rejects transient, external, and unknown media references", () => {
  for (const src of [
    "data:image/png;base64,ZmFrZQ==",
    "blob:unsafe",
    "https://cdn.example/image.png",
    "tenant-a/private/object-key.png",
    "/api/v1/classrooms/asset-1/draft-media/not-uploaded",
  ]) {
    const slide = slideFixture({
      elements: [{
        id: "image-element",
        type: "image",
        left: 0,
        top: 0,
        width: 100,
        height: 100,
        rotate: 0,
        fixedRatio: true,
        src,
      }],
    });
    assert.throws(
      () => mergeImportedSlides(classroomFixture(), [slide], [], value => `${value}-1`),
      /uploaded classroom media receipt/i,
    );
  }
});

test("aborting an import prevents a completed parse from being published", async () => {
  let finishParse: ((slides: Slide[]) => void) | undefined;
  let markParseStarted: (() => void) | undefined;
  const parseStarted = new Promise<void>(resolve => {
    markParseStarted = resolve;
  });
  const load = createImporterLoader(async () => ({
    importPptx() {
      return new Promise(resolve => {
        finishParse = resolve;
        markParseStarted?.();
      });
    },
  }));
  const controller = new AbortController();

  const pending = withBrowserWindow(() =>
    load.importPptx(
      new ArrayBuffer(8),
      async () => {
        throw new Error("unused");
      },
      controller.signal,
    ),
  );
  await parseStarted;
  controller.abort();
  assert.ok(finishParse);
  finishParse([]);

  await assert.rejects(
    pending,
    error => error instanceof DOMException && error.name === "AbortError",
  );
});

test("concurrent browser imports evaluate the importer module once", async () => {
  let loadCount = 0;
  const load = createImporterLoader(async () => {
    loadCount += 1;
    return {
      async importPptx() {
        return [];
      },
    };
  });

  await withBrowserWindow(() =>
    Promise.all([
      load.importPptx(new ArrayBuffer(8), async () => {
        throw new Error("unused");
      }),
      load.importPptx(new ArrayBuffer(8), async () => {
        throw new Error("unused");
      }),
    ]),
  );

  assert.equal(loadCount, 1);
});
