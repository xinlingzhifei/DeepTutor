const LIVE_EVIDENCE = new Set([
  "teacher_flow",
  "student_micro_flow",
  "student_full_flow",
  "content_operations_flow",
  "tailwind4_visual_matrix",
]);

const REQUIRED_BASE_URL_ERROR =
  "WEB_BASE_URL is required for live release evidence";
const INVALID_BASE_URL_ERROR =
  "WEB_BASE_URL must identify a non-loopback HTTP(S) host";

export function isLivePlaywrightSelected(
  argv: readonly string[],
  evidence: string | undefined,
): boolean {
  const separatorIndex = argv.indexOf("--");
  const optionArgv = separatorIndex < 0 ? argv : argv.slice(0, separatorIndex);
  return (
    optionArgv.some(
      (argument, index) =>
        argument === "--project=first-release-live" ||
        (argument === "--project" &&
          optionArgv[index + 1] === "first-release-live"),
    ) || LIVE_EVIDENCE.has(evidence ?? "")
  );
}

function isLoopbackHostname(hostname: string): boolean {
  const normalizedHostname = hostname.toLowerCase().replace(/\.$/, "");
  return (
    normalizedHostname === "localhost" ||
    normalizedHostname.endsWith(".localhost") ||
    /^127(?:\.\d{1,3}){3}$/.test(normalizedHostname) ||
    normalizedHostname === "::1" ||
    normalizedHostname === "[::1]"
  );
}

export function resolveLiveBaseUrl(
  liveProjectSelected: boolean,
  rawBaseUrl: string | undefined,
): string | undefined {
  if (!liveProjectSelected) {
    return undefined;
  }
  const trimmedBaseUrl = rawBaseUrl?.trim();
  if (!trimmedBaseUrl) {
    throw new Error(REQUIRED_BASE_URL_ERROR);
  }
  let baseUrl: URL;
  try {
    baseUrl = new URL(trimmedBaseUrl);
  } catch {
    throw new Error(INVALID_BASE_URL_ERROR);
  }
  if (
    (baseUrl.protocol !== "http:" && baseUrl.protocol !== "https:") ||
    !baseUrl.hostname ||
    isLoopbackHostname(baseUrl.hostname)
  ) {
    throw new Error(INVALID_BASE_URL_ERROR);
  }
  return baseUrl.toString();
}
