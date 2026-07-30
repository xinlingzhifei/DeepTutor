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
