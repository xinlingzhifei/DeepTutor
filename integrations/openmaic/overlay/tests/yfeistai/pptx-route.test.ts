import { createHash } from "node:crypto";
import JSZip from "jszip";
import { expect, test, vi } from "vitest";
import { canonicalExportJson, type ExportGenerationRequest, type ExportArtifactOutput } from "../../lib/yfeistai/export-generation";

const captured = vi.hoisted(() => ({
  exportPptx: null as null | ((request: ExportGenerationRequest) => Promise<ExportArtifactOutput>),
}));
vi.mock("../../lib/yfeistai/export-generation", async (original) => ({
  ...await original<Record<string, unknown>>(),
  createExportPostHandler: (dependencies: typeof captured) => {
    captured.exportPptx = dependencies.exportPptx;
    return async () => new Response(null, { status: 202 });
  },
}));
import "../../app/api/yfeistai/v1/exports/route";

function request(elements: unknown[]): ExportGenerationRequest {
  const metadata = {
    teachingBriefId: "brief-a", teachingBriefSha256: "b".repeat(64),
    templateId: "template-a", templateVersion: "1",
  };
  const document = {
    schemaVersion: "1.0", classroomId: "classroom-a", classroomVersionId: "version-a",
    contentMode: "open_creation", openCreation: true,
    openmaic: {
      dslVersion: "0.1.0",
      stage: { id: "stage-a", name: "Geometry", createdAt: "2026-11-01T00:00:00Z", updatedAt: "2026-11-01T00:00:00Z" },
      scenes: [{ id: "scene-a", stageId: "stage-a", title: "Right triangle", order: 0, type: "slide", content: { type: "slide", canvas: { elements } }, actions: [] }],
    },
    interactionIds: [], sourceRefs: [],
    knowledgePointMappings: [{ knowledgePointId: "pythagorean-theorem", sceneIds: ["scene-a"], sourceRefs: [] }],
    mediaManifest: [], exportManifest: [], migrationRecords: [],
    generationMetadata: { ...metadata, generator: "openmaic", generatorVersion: "0.3.1", modelId: "test", generatedAt: "2026-11-01T00:00:00Z" },
    auditMetadata: metadata,
    validationResult: { valid: true, issues: [], validatedAt: "2026-11-01T00:00:00Z" },
  };
  const hash = (value: unknown) => createHash("sha256").update(canonicalExportJson(value)).digest("hex");
  const classroomDocument = { ...document, fileSha256: hash(document) };
  return {
    tenantId: "tenant-a", jobId: "export-a", classroomDocument,
    classroomDocumentSha256: hash(classroomDocument), mediaManifest: [],
  } as unknown as ExportGenerationRequest;
}

test("exports native editable square roots and fractions without exposing formula source", async () => {
  const input = request([
    { id: "text-a", type: "text", left: 60, top: 60, width: 880, height: 50, content: "<p>勾股定理</p>" },
    { id: "sqrt", type: "latex", left: 60, top: 200, width: 400, height: 80, latex: "c = \\sqrt{3^2 + 4^2} = 5" },
    { id: "fraction", type: "latex", left: 520, top: 200, width: 400, height: 100, latex: "\\frac{3}{5}" },
  ]);
  const result = await captured.exportPptx!(input);
  const archive = await JSZip.loadAsync(result.bytes, { checkCRC32: true });
  const xml = await archive.file("ppt/slides/slide1.xml")!.async("string");
  expect(xml).toContain("勾股定理");
  expect(xml.match(/<m:oMath>/g)).toHaveLength(2);
  expect(xml).toContain("<m:rad>");
  expect(xml).toContain("<m:f>");
  expect(xml).not.toMatch(/\\sqrt|\\frac|&lt;p&gt;|undefined/);
  expect(xml).toContain(input.classroomDocumentSha256);
});

test("keeps ordinary slide text editable when the canvas has no formulas", async () => {
  const result = await captured.exportPptx!(request([
    { id: "text-a", type: "text", content: "<p>Editable lesson</p>" },
  ]));
  const archive = await JSZip.loadAsync(result.bytes, { checkCRC32: true });
  const xml = await archive.file("ppt/slides/slide1.xml")!.async("string");
  expect(xml).toContain("<a:t>Editable lesson</a:t>");
  expect(xml).not.toContain("<m:oMath>");
});
