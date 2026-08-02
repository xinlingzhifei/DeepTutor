import type { DraftClassroomMedia } from "@/lib/classroom-api";

export type ImportedMedia = DraftClassroomMedia;
export type UploadImportedMedia = (
  blob: Blob,
  filename: string,
  directory?: string,
) => Promise<ImportedMedia>;

type ImporterUpload = (
  blob: Blob,
  filename: string,
  directory?: string,
) => Promise<string>;

export interface OpenMaicImporterModule {
  importPptx(
    input: File | Blob | ArrayBuffer,
    options?: { upload?: ImporterUpload },
  ): Promise<unknown[]>;
}

export const OPENMAIC_IMPORTER_VENDOR_URL =
  "/vendor/maic-importer/index.js" as const;

type FetchVendor = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;
type ImportVendor = (url: string) => Promise<unknown>;

export interface ImportedSlides {
  slides: unknown[];
  media: ImportedMedia[];
}

export interface ImporterLoader {
  readonly loaded: boolean;
  importPptx(
    input: File | Blob | ArrayBuffer,
    upload: UploadImportedMedia,
    signal?: AbortSignal,
  ): Promise<ImportedSlides>;
}

const MEDIA_FIELDS = new Set([
  "mediaId",
  "readUrl",
  "mimeType",
  "sizeBytes",
  "sha256",
]);
const MEDIA_URL_FIELDS = new Set([
  "src",
  "poster",
  "mediaUrl",
  "imageUrl",
  "audioUrl",
  "videoUrl",
]);
const MAX_IMPORTED_NODES = 100_000;
const MAX_IMPORTED_DEPTH = 64;

function controlledMediaUrl(value: string, mediaId: string): boolean {
  const encodedMediaId = encodeURIComponent(mediaId);
  const suffix = `/${encodedMediaId}`;
  if (!value.endsWith(suffix)) return false;
  const prefix = value.slice(0, -suffix.length);
  return (
    /^\/api\/v1\/classrooms\/[^/]+\/draft-media$/.test(prefix) ||
    /^\/api\/v1\/classrooms\/versions\/[^/]+\/media$/.test(prefix)
  );
}

function validateImportedMedia(value: ImportedMedia): ImportedMedia {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Imported media metadata is invalid");
  }
  for (const key of Object.keys(value)) {
    if (!MEDIA_FIELDS.has(key)) {
      throw new Error(`Unexpected imported media field: ${key}`);
    }
  }
  if (
    typeof value.mediaId !== "string" ||
    value.mediaId.length === 0 ||
    value.mediaId.trim() !== value.mediaId ||
    /[\u0000-\u001f\u007f]/.test(value.mediaId) ||
    typeof value.readUrl !== "string" ||
    !controlledMediaUrl(value.readUrl, value.mediaId) ||
    value.readUrl.includes("openmaic")
  ) {
    throw new Error("Imported media must use a controlled yFeiSTAI media route");
  }
  if (
    typeof value.mimeType !== "string" ||
    !/^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/i.test(
      value.mimeType,
    ) ||
    !Number.isSafeInteger(value.sizeBytes) ||
    value.sizeBytes < 0 ||
    typeof value.sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(value.sha256)
  ) {
    throw new Error("Imported media metadata is invalid");
  }
  return {
    mediaId: value.mediaId,
    readUrl: value.readUrl,
    mimeType: value.mimeType,
    sizeBytes: value.sizeBytes,
    sha256: value.sha256,
  };
}

function validateSlideMediaUrls(
  slides: unknown[],
  media: readonly ImportedMedia[],
): void {
  const uploadedUrls = new Set(media.map(item => item.readUrl));
  const visited = new WeakSet<object>();
  const active = new WeakSet<object>();
  let nodeCount = 0;

  const visit = (value: unknown, depth: number): void => {
    if (depth > MAX_IMPORTED_DEPTH) {
      throw new Error("Imported slides exceed the supported nesting depth");
    }
    if (typeof value !== "object" || value === null) return;
    if (active.has(value)) {
      throw new Error("Imported slides must not contain cyclic data");
    }
    if (visited.has(value)) return;
    visited.add(value);
    active.add(value);
    nodeCount += 1;
    if (nodeCount > MAX_IMPORTED_NODES) {
      throw new Error("Imported slides exceed the supported size");
    }

    if (Array.isArray(value)) {
      for (const item of value) visit(item, depth + 1);
      active.delete(value);
      return;
    }
    for (const [key, item] of Object.entries(value)) {
      if (MEDIA_URL_FIELDS.has(key) && item !== null && item !== undefined) {
        if (typeof item !== "string" || !uploadedUrls.has(item)) {
          throw new Error(
            "Imported media must be uploaded through a controlled yFeiSTAI media route",
          );
        }
      }
      visit(item, depth + 1);
    }
    active.delete(value);
  };

  visit(slides, 0);
}

function throwIfAborted(signal?: AbortSignal): void {
  signal?.throwIfAborted();
}

async function importOpenMaicVendor(url: string): Promise<unknown> {
  if (url !== OPENMAIC_IMPORTER_VENDOR_URL) {
    throw new Error("PPTX parser URL is invalid");
  }
  return import(
    /* webpackIgnore: true */
    /* turbopackIgnore: true */
    /* @vite-ignore */
    url
  );
}

export async function loadOpenMaicImporterFromVendor(
  fetchVendor: FetchVendor = fetch,
  importVendor: ImportVendor = importOpenMaicVendor,
  signal?: AbortSignal,
): Promise<OpenMaicImporterModule> {
  const request: RequestInit = {
    method: "HEAD",
    cache: "no-store",
    credentials: "same-origin",
  };
  if (signal !== undefined) request.signal = signal;
  const response = await fetchVendor(OPENMAIC_IMPORTER_VENDOR_URL, request);
  const contentType = response.headers.get("content-type");
  if (
    !response.ok ||
    response.redirected ||
    (contentType !== null && !/^(?:application|text)\/javascript(?:;|$)/i.test(contentType))
  ) {
    throw new Error("PPTX parser is unavailable");
  }
  signal?.throwIfAborted();
  const loaded = await importVendor(OPENMAIC_IMPORTER_VENDOR_URL);
  if (
    typeof loaded !== "object" ||
    loaded === null ||
    !("importPptx" in loaded) ||
    typeof loaded.importPptx !== "function"
  ) {
    throw new Error("PPTX parser is unavailable");
  }
  return loaded as OpenMaicImporterModule;
}

async function loadOpenMaicImporter(
  signal?: AbortSignal,
): Promise<OpenMaicImporterModule> {
  return loadOpenMaicImporterFromVendor(fetch, importOpenMaicVendor, signal);
}

export function createImporterLoader(
  loadModule: (signal?: AbortSignal) => Promise<OpenMaicImporterModule> =
    loadOpenMaicImporter,
): ImporterLoader {
  let modulePromise: Promise<OpenMaicImporterModule> | undefined;

  return {
    get loaded() {
      return modulePromise !== undefined;
    },

    async importPptx(input, upload, signal) {
      throwIfAborted(signal);
      if (typeof window === "undefined") {
        throw new Error("PPTX import is browser-only");
      }
      throwIfAborted(signal);
      modulePromise ??= loadModule(signal).catch(error => {
        modulePromise = undefined;
        throw error;
      });
      const importer = await modulePromise;
      throwIfAborted(signal);
      const media: ImportedMedia[] = [];
      const slides = await importer.importPptx(input, {
        upload: async (blob, filename, directory) => {
          throwIfAborted(signal);
          const item = validateImportedMedia(
            await upload(blob, filename, directory),
          );
          throwIfAborted(signal);
          media.push(item);
          return item.readUrl;
        },
      });
      throwIfAborted(signal);
      if (!Array.isArray(slides)) {
        throw new Error("Imported slides must be an array");
      }
      validateSlideMediaUrls(slides, media);
      return { slides, media };
    },
  };
}

const importerLoader = createImporterLoader();

export function importPptxInBrowser(
  input: File | Blob | ArrayBuffer,
  upload: UploadImportedMedia,
  signal?: AbortSignal,
): Promise<ImportedSlides> {
  return importerLoader.importPptx(input, upload, signal);
}
