export const OPENMAIC_UPSTREAM_COMMIT =
  "0cf2a330411681190e89f48e20f305345ff99f87" as const;
export const OPENMAIC_APP_VERSION = "0.3.1" as const;
export const CLASSROOM_CONTRACT_VERSIONS = ["1.0"] as const;
export const OPENMAIC_CAPABILITIES = [
  "outline",
  "content",
  "micro",
  "export",
  "cancel",
  "artifact-manifest",
] as const;
export const OPENMAIC_EXPORT_FORMATS = [
  "classroom_zip",
  "pptx",
  "offline_html",
  "mp4",
] as const;

export interface OpenMaicHealthResponse {
  service: "openmaic";
  upstreamCommit: typeof OPENMAIC_UPSTREAM_COMMIT;
  appVersion: typeof OPENMAIC_APP_VERSION;
  contractVersions: typeof CLASSROOM_CONTRACT_VERSIONS;
  capabilities: typeof OPENMAIC_CAPABILITIES;
  exportFormats: typeof OPENMAIC_EXPORT_FORMATS;
}

export const OPENMAIC_HEALTH_RESPONSE = {
  service: "openmaic",
  upstreamCommit: OPENMAIC_UPSTREAM_COMMIT,
  appVersion: OPENMAIC_APP_VERSION,
  contractVersions: CLASSROOM_CONTRACT_VERSIONS,
  capabilities: OPENMAIC_CAPABILITIES,
  exportFormats: OPENMAIC_EXPORT_FORMATS,
} as const satisfies OpenMaicHealthResponse;

export interface SourceReference {
  citationId: string;
  sourceId: string;
  fragmentId: string;
}

export interface SourceFragment {
  fragmentId: string;
  sourceId: string;
  text: string;
  contentSha256: string;
}

export interface SourceCitation extends SourceReference {
  label: string;
}

export interface TeachingBrief {
  schemaVersion: "1.0";
  briefId: string;
  briefVersion: number;
  tenantId: string;
  courseId: string;
  targetClassId: string;
  gradeBand: string;
  audienceLevel: string;
  classroomMode: "micro" | "full";
  objectives: Array<{
    objectiveId: string;
    description: string;
    knowledgePointIds: string[];
  }>;
  durationMinutes: number;
  knowledgePoints: Array<{
    knowledgePointId: string;
    title: string;
    description: string;
  }>;
  prerequisites: Array<{
    knowledgePointId: string;
    prerequisiteKnowledgePointIds: string[];
  }>;
  assessment: {
    methods: Array<
      "quiz" | "discussion" | "project" | "observation" | "self_assessment"
    >;
    successCriteria: string[];
  };
  sourceSnapshot: {
    snapshotId: string;
    createdAt: string;
    contentSha256: string;
  } | null;
  sourceFragments: SourceFragment[];
  citations: SourceCitation[];
  sourceRefs: SourceReference[];
  permissionSummary: {
    allowedSourceIds: string[];
    allowedFragmentIds: string[];
    usageScope: string;
    attributionRequired: boolean;
  };
  contentMode: "source_grounded" | "open_creation";
  networkPolicy: {
    allowWebAccess: boolean;
    allowedDomains: string[];
  };
  mediaPolicy: {
    allowGeneration: boolean;
    allowedMimeTypes: string[];
  };
  templatePolicy: {
    templateId: string;
    templateVersion: string;
  };
  safetyPolicy: {
    policyId: string;
    blockedCategories: string[];
  };
  contentSha256: string;
}

export interface GenerationMetadata {
  generator: string;
  generatorVersion: string;
  modelId: string;
  generatedAt: string;
  teachingBriefId: string;
  teachingBriefSha256: string;
  templateId: string;
  templateVersion: string;
}

export interface OutlineScene {
  sceneId: string;
  title: string;
  summary: string;
  knowledgePointIds: string[];
  sourceRefs: SourceReference[];
}

export interface OutlineBundle {
  schemaVersion: "1.0";
  outlineId: string;
  outlineVersion: number;
  confirmationMetadata: {
    status: "draft" | "confirmed";
    confirmedAt?: string | null;
    confirmedBy?: string | null;
  };
  title: string;
  language: string;
  scenes: OutlineScene[];
  knowledgeCoverage: Array<{
    knowledgePointId: string;
    sceneIds: string[];
  }>;
  sourceRefs: SourceReference[];
  estimatedSceneCount: number;
  generationMetadata: GenerationMetadata;
  contractSha256: string;
}

export interface GenerationRequest {
  schemaVersion: "1.0";
  tenantId: string;
  requestId: string;
  jobId: string;
  idempotencyKey: string;
  phase: "outline" | "content" | "micro";
  classroomMode: "micro" | "full";
  teachingBriefId: string;
  teachingBriefSha256: string;
  teachingBrief: TeachingBrief;
  confirmedOutline?: OutlineBundle | null;
  confirmedOutlineSha256?: string | null;
  templateId: string;
  templateVersion: string;
  sceneBudget: number;
  durationMinutes: number;
  requestedExports: Array<"classroom_zip" | "pptx" | "offline_html" | "mp4">;
  callbackContext: string;
  dataPlaneRouteId: string;
  priority: "student_micro" | "interaction" | "teacher" | "full" | "batch";
}

export type OutlineJobStatus = "running" | "succeeded" | "failed" | "canceled";

export interface OutlineJob {
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  phase: "outline";
  status: OutlineJobStatus;
  createdAt: string;
  updatedAt: string;
  result?: {
    outline: OutlineBundle;
  };
  error?: {
    code: string;
    message: string;
  };
}
