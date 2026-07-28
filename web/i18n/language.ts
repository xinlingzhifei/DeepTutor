export type AppLanguage = "zh" | "en";

export const DEFAULT_APP_LANGUAGE: AppLanguage = "zh";

export function normalizeLanguage(value: unknown): AppLanguage {
  const normalized = String(value ?? "")
    .trim()
    .toLowerCase()
    .replaceAll("_", "-");

  if (
    normalized === "en" ||
    normalized === "english" ||
    normalized.startsWith("en-")
  ) {
    return "en";
  }
  if (
    normalized === "zh" ||
    normalized === "cn" ||
    normalized === "chinese" ||
    normalized.startsWith("zh-")
  ) {
    return "zh";
  }
  return DEFAULT_APP_LANGUAGE;
}
