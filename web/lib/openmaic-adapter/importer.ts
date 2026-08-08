import type { DraftClassroomMedia } from "@/lib/classroom-api";
import type { Slide } from "@openmaic/dsl";

import type {
  ClassroomDocument,
  JsonObject,
  MediaManifestItem,
} from "./contracts";
import {
  pushHistory,
  type EditorHistory,
} from "./editor-history";

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
  ): Promise<Slide[]>;
}

export const OPENMAIC_IMPORTER_VENDOR_URL =
  "/vendor/maic-importer/index.js" as const;

type FetchVendor = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;
type ImportVendor = (url: string) => Promise<unknown>;

export interface ImportedSlides {
  slides: Slide[];
  media: ImportedMedia[];
}

export type ImportIdFactory = (prefix: "scene" | "canvas") => string;

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
  "relativePath",
  "readUrl",
  "mimeType",
  "sizeBytes",
  "sha256",
]);
const MEDIA_URL_FIELDS = new Set([
  "src",
  "poster",
  "pattern",
  "mediaRef",
  "mediaUrl",
  "imageUrl",
  "audioUrl",
  "videoUrl",
]);
const MAX_IMPORTED_NODES = 100_000;
const MAX_IMPORTED_DEPTH = 64;
const MAX_ID_ATTEMPTS = 100;

function portableMediaPath(value: string): boolean {
  return (
    value.length > 0 &&
    value.trim() === value &&
    !value.includes("\\") &&
    !value.startsWith("/") &&
    !/[?#%\u0000-\u001f\u007f]/.test(value) &&
    !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(value) &&
    value.split("/").every(segment => segment !== "" && segment !== "." && segment !== "..")
  );
}

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
    typeof value.relativePath !== "string" ||
    !portableMediaPath(value.relativePath) ||
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
    relativePath: value.relativePath,
    readUrl: value.readUrl,
    mimeType: value.mimeType,
    sizeBytes: value.sizeBytes,
    sha256: value.sha256,
  };
}

interface MediaIndexes {
  byReadUrl: ReadonlyMap<string, ImportedMedia>;
  byRelativePath: ReadonlyMap<string, ImportedMedia>;
  ordered: readonly ImportedMedia[];
}

function indexImportedMedia(media: readonly ImportedMedia[]): MediaIndexes {
  const byReadUrl = new Map<string, ImportedMedia>();
  const byRelativePath = new Map<string, ImportedMedia>();
  const byId = new Map<string, ImportedMedia>();
  const ordered = media.map(validateImportedMedia);
  for (const item of ordered) {
    if (
      byReadUrl.has(item.readUrl) ||
      byRelativePath.has(item.relativePath) ||
      byId.has(item.mediaId)
    ) {
      throw new Error("Imported media receipts must have unique identities and paths");
    }
    byReadUrl.set(item.readUrl, item);
    byRelativePath.set(item.relativePath, item);
    byId.set(item.mediaId, item);
  }
  return { byReadUrl, byRelativePath, ordered };
}

function transformSlideMediaReferences(
  slides: readonly Slide[],
  media: readonly ImportedMedia[],
  replaceWithPortablePath: boolean,
): { slides: Slide[]; referencedMediaIds: ReadonlySet<string> } {
  const indexes = indexImportedMedia(media);
  const active = new WeakSet<object>();
  const referencedMediaIds = new Set<string>();
  let nodeCount = 0;

  const visit = (value: unknown, depth: number): unknown => {
    if (depth > MAX_IMPORTED_DEPTH) {
      throw new Error("Imported slides exceed the supported nesting depth");
    }
    if (typeof value !== "object" || value === null) return value;
    if (active.has(value)) {
      throw new Error("Imported slides must not contain cyclic data");
    }
    active.add(value);
    nodeCount += 1;
    if (nodeCount > MAX_IMPORTED_NODES) {
      throw new Error("Imported slides exceed the supported size");
    }

    if (Array.isArray(value)) {
      const result = value.map(item => visit(item, depth + 1));
      active.delete(value);
      return result;
    }
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      if (MEDIA_URL_FIELDS.has(key) && item !== null && item !== undefined) {
        const receipt = typeof item === "string"
          ? indexes.byReadUrl.get(item) ?? indexes.byRelativePath.get(item)
          : undefined;
        if (!receipt) {
          throw new Error(
            "Imported media must be uploaded through a controlled yFeiSTAI media route and match an uploaded classroom media receipt",
          );
        }
        referencedMediaIds.add(receipt.mediaId);
        result[key] = replaceWithPortablePath ? receipt.relativePath : item;
      } else {
        result[key] = visit(item, depth + 1);
      }
    }
    active.delete(value);
    return result;
  };

  return {
    slides: visit(slides, 0) as Slide[],
    referencedMediaIds,
  };
}

function validateSlideMediaUrls(
  slides: readonly Slide[],
  media: readonly ImportedMedia[],
): void {
  transformSlideMediaReferences(slides, media, false);
}

function cloneDocument(input: ClassroomDocument): ClassroomDocument {
  return JSON.parse(JSON.stringify(input)) as ClassroomDocument;
}

function nextUniqueId(
  prefix: "scene" | "canvas",
  usedIds: Set<string>,
  idFactory: ImportIdFactory,
): string {
  for (let attempt = 0; attempt < MAX_ID_ATTEMPTS; attempt += 1) {
    const candidate = idFactory(prefix);
    if (
      typeof candidate === "string" &&
      candidate.length > 0 &&
      candidate.trim() === candidate &&
      !/[\u0000-\u001f\u007f]/.test(candidate) &&
      !usedIds.has(candidate)
    ) {
      usedIds.add(candidate);
      return candidate;
    }
  }
  throw new Error(`Unable to allocate a unique imported ${prefix} ID`);
}

function manifestItem(receipt: ImportedMedia): MediaManifestItem {
  return {
    mediaId: receipt.mediaId,
    relativePath: receipt.relativePath,
    mimeType: receipt.mimeType,
    sha256: receipt.sha256,
    sizeBytes: receipt.sizeBytes,
  };
}

function sameManifestBinding(
  current: MediaManifestItem,
  receipt: ImportedMedia,
): boolean {
  return (
    current.mediaId === receipt.mediaId &&
    current.relativePath === receipt.relativePath &&
    current.mimeType === receipt.mimeType &&
    current.sha256 === receipt.sha256 &&
    current.sizeBytes === receipt.sizeBytes
  );
}

export function mergeImportedSlides(
  document: ClassroomDocument,
  slides: readonly Slide[],
  receipts: readonly ImportedMedia[],
  idFactory: ImportIdFactory,
): ClassroomDocument {
  if (slides.length === 0) {
    throw new Error("Imported presentation must contain at least one slide");
  }
  const indexes = indexImportedMedia(receipts);
  const transformed = transformSlideMediaReferences(slides, indexes.ordered, true);
  const next = cloneDocument(document);
  next.openmaic.scenes = next.openmaic.scenes.map((scene, order) => ({
    ...scene,
    order,
  }));

  const usedIds = new Set<string>();
  for (const scene of next.openmaic.scenes) {
    usedIds.add(scene.id);
    if (scene.type === "slide" && typeof scene.content.canvas.id === "string") {
      usedIds.add(scene.content.canvas.id);
    }
  }

  transformed.slides.forEach((slide, index) => {
    const sceneId = nextUniqueId("scene", usedIds, idFactory);
    const canvasId = nextUniqueId("canvas", usedIds, idFactory);
    const title = slide.sectionTag?.title?.trim() || `Imported slide ${index + 1}`;
    next.openmaic.scenes.push({
      id: sceneId,
      stageId: next.openmaic.stage.id,
      title,
      order: next.openmaic.scenes.length,
      type: "slide",
      content: {
        type: "slide",
        canvas: {
          ...(slide as unknown as JsonObject),
          id: canvasId,
        },
      },
      actions: [],
    });
  });

  for (const receipt of indexes.ordered) {
    if (!transformed.referencedMediaIds.has(receipt.mediaId)) continue;
    const existing = next.mediaManifest.find(
      item => item.mediaId === receipt.mediaId || item.relativePath === receipt.relativePath,
    );
    if (existing) {
      if (!sameManifestBinding(existing, receipt)) {
        throw new Error("Imported media conflicts with an existing classroom media binding");
      }
      continue;
    }
    next.mediaManifest.push(manifestItem(receipt));
  }
  return next;
}

export function mergeImportedSlidesIntoHistory(
  history: EditorHistory<ClassroomDocument>,
  slides: readonly Slide[],
  receipts: readonly ImportedMedia[],
  idFactory: ImportIdFactory,
): EditorHistory<ClassroomDocument> {
  return pushHistory(
    history,
    mergeImportedSlides(history.present, slides, receipts, idFactory),
  );
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
