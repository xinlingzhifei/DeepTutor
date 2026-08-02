import assert from "node:assert/strict";
import test from "node:test";

import {
  OPENMAIC_IMPORTER_VENDOR_URL,
  createImporterLoader,
  loadOpenMaicImporterFromVendor,
  type ImportedMedia,
  type OpenMaicImporterModule,
} from "../lib/openmaic-adapter/importer";

const SHA256 = "a".repeat(64);

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
      return [{ id: "slide-1", mediaUrl }];
    },
  };
  const load = createImporterLoader(async () => {
    loadCount += 1;
    return fakeImporter;
  });
  const uploaded: ImportedMedia = {
    mediaId: "media-1",
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
    slides: [
      {
        id: "slide-1",
        mediaUrl: "/api/v1/classrooms/asset-1/draft-media/media-1",
      },
    ],
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
        return [{ elements: [{ type: "image", src: fallbackUrl }] }];
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
      return [
        {
          background: {
            image: {
              src: "/api/v1/classrooms/asset-1/draft-media/not-uploaded",
            },
          },
        },
      ];
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

test("aborting an import prevents a completed parse from being published", async () => {
  let finishParse: ((slides: unknown[]) => void) | undefined;
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
