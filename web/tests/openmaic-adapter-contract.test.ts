import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  ClassroomCompatibilityError,
  classroomMediaUrl,
  mapClassroomTheme,
  parseYFeClassroomDocument,
  resolveClassroomMediaReferences,
} from "../lib/openmaic-adapter/contracts";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const NOW = "2026-07-30T12:00:00+08:00";

function validClassroomDocument() {
  return {
    schemaVersion: "1.0",
    classroomId: "classroom-1",
    classroomVersionId: "version-1",
    contentMode: "open_creation",
    openCreation: true,
    openmaic: {
      dslVersion: "0.1.0",
      stage: {
        id: "stage-1",
        name: "Portable classroom",
        createdAt: NOW,
        updatedAt: NOW,
      },
      scenes: [
        {
          id: "slide-1",
          stageId: "stage-1",
          title: "Slide",
          order: 0,
          type: "slide",
          content: {
            type: "slide",
            canvas: {
              elements: [
                {
                  id: "image-1",
                  type: "image",
                  left: 10,
                  top: 20,
                  width: 300,
                  height: 180,
                  rotate: 0,
                  fixedRatio: true,
                  src: "media/scene-1/image.png",
                },
              ],
            },
          },
          actions: [
            { type: "speech", payload: { text: "Welcome" } },
          ],
        },
        {
          id: "quiz-1",
          stageId: "stage-1",
          title: "Quiz",
          order: 1,
          type: "quiz",
          content: {
            type: "quiz",
            questions: [
              {
                id: "question-1",
                prompt: "Choose one",
                questionType: "single_choice",
                options: [
                  { id: "a", label: "A" },
                  { id: "b", label: "B" },
                ],
                correctOptionIds: ["a"],
                explanation: "A is correct.",
              },
            ],
          },
        },
        {
          id: "interactive-1",
          stageId: "stage-1",
          title: "Interactive",
          order: 2,
          type: "interactive",
          content: {
            type: "interactive",
            html: "<button id='run'>Run</button>",
            bridgeVersion: "1.0",
            sandbox: { allowScripts: true, allowSameOrigin: false },
          },
          actions: [],
        },
        {
          id: "pbl-1",
          stageId: "stage-1",
          title: "PBL",
          order: 3,
          type: "pbl",
          content: {
            type: "pbl",
            scenario: "Investigate a signal.",
            roles: [{ id: "analyst", name: "Analyst", brief: "Analyze it." }],
            milestones: [
              { id: "report", title: "Report", rubric: "Explain the result." },
            ],
          },
          actions: [],
        },
      ],
    },
    interactionIds: ["quiz-1", "interactive-1", "pbl-1"],
    sourceRefs: [],
    knowledgePointMappings: [
      {
        knowledgePointId: "kp-1",
        sceneIds: ["slide-1", "quiz-1", "interactive-1", "pbl-1"],
        sourceRefs: [],
      },
    ],
    mediaManifest: [
      {
        mediaId: "media-1",
        relativePath: "media/scene-1/image.png",
        mimeType: "image/png",
        sha256: SHA_A,
        sizeBytes: 128,
        temporaryDownloadPath: "downloads/media/scene-1/image.png",
        expiresAt: NOW,
      },
    ],
    fileSha256: SHA_A,
    exportManifest: [
      {
        format: "classroom_zip",
        relativePath: "exports/classroom.zip",
        sha256: SHA_B,
        sizeBytes: 256,
        mimeType: "application/zip",
        temporaryDownloadPath: "downloads/exports/classroom.zip",
        expiresAt: NOW,
      },
    ],
    generationMetadata: {
      generator: "openmaic",
      generatorVersion: "0.1.0",
      modelId: "model-1",
      generatedAt: NOW,
      teachingBriefId: "brief-1",
      teachingBriefSha256: SHA_A,
      templateId: "template-1",
      templateVersion: "1",
    },
    auditMetadata: {
      templateId: "template-1",
      templateVersion: "1",
      teachingBriefId: "brief-1",
      teachingBriefSha256: SHA_A,
      parentClassroomVersionId: null,
    },
    validationResult: {
      valid: true,
      issues: [],
      validatedAt: NOW,
    },
    migrationRecords: [
      {
        fromDslVersion: "0.0.0",
        toDslVersion: "0.1.0",
        migratedAt: NOW,
        migrationId: "migration-1",
      },
    ],
  };
}

function deepClone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

test("OpenMAIC packages and renderer peers are exact", () => {
  const packageJson = JSON.parse(readFileSync("package.json", "utf8")) as {
    dependencies: Record<string, string>;
  };
  assert.deepEqual(
    {
      dsl: packageJson.dependencies["@openmaic/dsl"],
      renderer: packageJson.dependencies["@openmaic/renderer"],
      importer: packageJson.dependencies["@openmaic/importer"],
      motion: packageJson.dependencies.motion,
      echarts: packageJson.dependencies.echarts,
      shiki: packageJson.dependencies.shiki,
    },
    {
      dsl: "0.4.0",
      renderer: "0.0.3",
      importer: "0.1.0",
      motion: "12.27.5",
      echarts: "6.0.0",
      shiki: "3.21.0",
    },
  );
});

function sourceFiles(root: string): string[] {
  if (!statSync(root).isDirectory()) return [root];
  return readdirSync(root)
    .flatMap(entry => sourceFiles(path.join(root, entry)))
    .filter(file => /\.(?:css|js|mjs|ts|tsx)$/.test(file));
}

test("only the adapter imports OpenMAIC packages", () => {
  const roots = ["app", "components", "context", "hooks", "lib"];
  const adapterRoot = `${path.join("lib", "openmaic-adapter")}${path.sep}`;
  const offenders = roots
    .flatMap(sourceFiles)
    .filter(file => !file.startsWith(adapterRoot))
    .filter(file =>
      /(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)["']@openmaic\/(?:dsl|renderer|importer)/.test(
        readFileSync(file, "utf8"),
      ),
    );
  assert.deepEqual(offenders, []);
});

test("the adapter calls the real migration and validation exports", () => {
  const source = readFileSync("lib/openmaic-adapter/dsl.ts", "utf8");
  const entrypoint = readFileSync("lib/openmaic-adapter/index.ts", "utf8");
  const contracts = readFileSync("lib/openmaic-adapter/contracts.ts", "utf8");
  assert.match(source, /\bDSL_VERSION\b/);
  assert.match(source, /\bmigrate\(input\)/);
  assert.match(source, /\bvalidateStage\(/);
  assert.match(source, /\bvalidateScene\(/);
  assert.match(source, /\bvalidateAction\(/);
  assert.match(source, /\bnormalizeScene\(/);
  assert.match(entrypoint, /from "@openmaic\/renderer\/editing"/);
  assert.doesNotMatch(
    `${source}\n${entrypoint}\n${contracts}`,
    /(?:from\s*|import\s*\(\s*)["']@openmaic\/importer/,
    "the browser-only PPTX importer is loaded lazily by the Task 6 boundary",
  );
  assert.doesNotMatch(source, /@openmaic\/renderer\/fonts\.css/);
});

test("installed DSL export surface matches the adapter assumptions", (t) => {
  const result = spawnSync(
    process.execPath,
    [
      "--input-type=module",
      "--eval",
      [
        'import * as dsl from "@openmaic/dsl";',
        'for (const name of ["DSL_VERSION","migrate","validateStage","validateScene","validateAction","normalizeScene"]) {',
        '  if (!(name in dsl)) throw new Error(`missing ${name}`);',
        "}",
        'if (dsl.DSL_VERSION !== "0.1.0") throw new Error(`unexpected DSL ${dsl.DSL_VERSION}`);',
      ].join("\n"),
    ],
    { cwd: process.cwd(), encoding: "utf8" },
  );
  if (result.error && (result.error as NodeJS.ErrnoException).code === "ENOENT") {
    t.skip("Node runtime unavailable");
    return;
  }
  assert.equal(result.status, 0, `${result.stderr}\n${result.stdout}`);
});

test("the yFeiSTAI classroom validator accepts the complete scene union without mutating input", () => {
  const input = validClassroomDocument();
  const before = deepClone(input);
  const parsed = parseYFeClassroomDocument(input);

  assert.deepEqual(input, before);
  assert.deepEqual(
    parsed.openmaic.scenes.map(scene => scene.type),
    ["slide", "quiz", "interactive", "pbl"],
  );
  assert.deepEqual(parsed.openmaic.scenes[1].actions, []);
});

test("the validator rejects extra fields, mismatched unions, bad lineage, and invalid metadata", () => {
  const cases = [
    (() => {
      const value = validClassroomDocument() as Record<string, unknown>;
      value.providerKey = "must-not-cross-boundary";
      return value;
    })(),
    (() => {
      const value = validClassroomDocument();
      value.openmaic.scenes[0].content.type = "quiz";
      return value;
    })(),
    (() => {
      const value = validClassroomDocument();
      value.knowledgePointMappings[0].sceneIds = ["missing-scene"];
      return value;
    })(),
    (() => {
      const value = validClassroomDocument();
      value.generationMetadata.teachingBriefSha256 = "not-a-hash";
      return value;
    })(),
  ];

  for (const value of cases) {
    assert.throws(
      () => parseYFeClassroomDocument(value),
      (error: unknown) =>
        error instanceof ClassroomCompatibilityError &&
        error.code === "INVALID_CLASSROOM_DOCUMENT" &&
        error.issues.length > 0,
    );
  }
});

test("media references resolve only through encoded yFeiSTAI routes", () => {
  const parsed = parseYFeClassroomDocument(validClassroomDocument());
  const resolved = resolveClassroomMediaReferences(parsed);
  const slide = resolved.openmaic.scenes[0];
  assert.equal(slide.type, "slide");
  const elements = slide.content.canvas.elements;
  assert.ok(Array.isArray(elements));
  assert.equal(
    (elements[0] as { src: string }).src,
    "/api/v1/classrooms/versions/version-1/media/media-1",
  );
  assert.equal(
    classroomMediaUrl("version / 1", "media?#1"),
    "/api/v1/classrooms/versions/version%20%2F%201/media/media%3F%231",
  );
  assert.deepEqual(resolveClassroomMediaReferences(resolved), resolved);
  assert.throws(() => classroomMediaUrl("..", "media-1"), /safe identifier/i);

  const missing = parseYFeClassroomDocument(validClassroomDocument());
  const missingSlide = missing.openmaic.scenes[0];
  assert.equal(missingSlide.type, "slide");
  (missingSlide.content.canvas.elements as Array<{ src: string }>)[0].src =
    "media/missing.png";
  assert.throws(
    () => resolveClassroomMediaReferences(missing),
    (error: unknown) =>
      error instanceof ClassroomCompatibilityError &&
      error.code === "UNSAFE_MEDIA_REFERENCE",
  );

  const external = parseYFeClassroomDocument(validClassroomDocument());
  const externalSlide = external.openmaic.scenes[0];
  assert.equal(externalSlide.type, "slide");
  (externalSlide.content.canvas.elements as Array<{ src: string }>)[0].src =
    "https://cdn.example/image.png";
  assert.throws(() => resolveClassroomMediaReferences(external), /classroom media route/i);

  const forgedRoute = parseYFeClassroomDocument(validClassroomDocument());
  const forgedSlide = forgedRoute.openmaic.scenes[0];
  assert.equal(forgedSlide.type, "slide");
  (forgedSlide.content.canvas.elements as Array<{ src: string }>)[0].src =
    "/api/v1/classrooms/versions/other/media/media-1";
  assert.throws(() => resolveClassroomMediaReferences(forgedRoute), /classroom media route/i);

  const interactiveResource = parseYFeClassroomDocument(validClassroomDocument());
  const interactive = interactiveResource.openmaic.scenes[2];
  assert.equal(interactive.type, "interactive");
  interactive.content.html = '<img src="media/scene-1/image.png">';
  assert.throws(
    () => resolveClassroomMediaReferences(interactiveResource),
    /self-contained/i,
  );
});

test("theme mapping uses app variables and adapter styles never load remote fonts", () => {
  for (const themeId of ["snow", "cream", "dark", "glass"] as const) {
    const theme = mapClassroomTheme(themeId);
    assert.match(theme.fontName, /^var\(--openmaic-font-(?:sans|serif)\)$/);
    assert.ok(theme.themeColors.length >= 4);
  }
  const styles = readFileSync("lib/openmaic-adapter/styles.css", "utf8");
  assert.match(styles, /--openmaic-font-sans:\s*[\s\S]*var\(--font-sans\)/);
  assert.match(styles, /--openmaic-font-serif:\s*[\s\S]*var\(--font-serif\)/);
  assert.doesNotMatch(styles, /@font-face|file\.maic\.chat|fonts\.googleapis\.com|https?:\/\//i);
});

test("third-party notices are backed by package and bundled-font licenses", () => {
  const notices = readFileSync("../THIRD_PARTY_NOTICES.md", "utf8");
  for (const name of ["@openmaic/dsl", "@openmaic/renderer", "@openmaic/importer"]) {
    assert.match(notices, new RegExp(name.replace("/", "\\/")));
    const license = readFileSync(path.join("node_modules", name, "LICENSE"), "utf8");
    assert.match(license, /MIT License/);
    assert.match(license, /Copyright \(c\) 2026 THU-MAIC/);
  }
  assert.match(notices, /Copyright \(c\) 2026 THU-MAIC/);
  assert.match(notices, /Geist[\s\S]*SIL Open Font License 1\.1/);
  assert.match(notices, /Lora[\s\S]*SIL Open Font License 1\.1/);
});
