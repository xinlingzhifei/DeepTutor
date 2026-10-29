import { apiFetch, apiUrl } from "@/lib/api";

export const COURSE_MODEL_CHANGED = "course-model-changed";

export interface CourseModelStatus {
  state: "not_enabled" | "not_configured" | "synced" | "error";
  ready: boolean;
  can_configure: boolean;
  reason?: string;
  provider?: string;
  model?: string;
  last_synced_at?: string;
}

export async function fetchCourseModelStatus(
  teaching = false, resync = false,
): Promise<CourseModelStatus> {
  const response = await apiFetch(apiUrl(resync
    ? "/api/v1/settings/openmaic-sync"
    : teaching ? "/api/v1/teaching/model-readiness" : "/api/v1/settings/openmaic-status"), {
    method: resync ? "POST" : "GET",
    cache: "no-store",
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error("Course model status unavailable");
  const value = await response.json() as CourseModelStatus;
  if (!value || !["not_enabled", "not_configured", "synced", "error"].includes(value.state)
    || typeof value.ready !== "boolean" || typeof value.can_configure !== "boolean"
    || value.ready !== (value.state === "synced")) {
    throw new Error("Invalid course model status");
  }
  return value;
}
