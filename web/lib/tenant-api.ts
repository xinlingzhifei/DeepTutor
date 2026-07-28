import { apiFetch, apiUrl } from "@/lib/api";

export interface SwitchTenantResponse {
  active_tenant_id: string;
}

export async function switchTenant(
  tenantId: string,
): Promise<SwitchTenantResponse> {
  const normalizedId = tenantId.trim();
  if (!normalizedId) {
    throw new Error("Organization is required.");
  }

  const response = await apiFetch(apiUrl("/api/v1/tenants/active"), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tenant_id: normalizedId }),
  });

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    const detail =
      typeof payload?.detail === "string"
        ? payload.detail
        : `Could not switch organization (${response.status}).`;
    throw new Error(detail);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error("The server returned an invalid organization response.");
  }

  if (
    typeof payload !== "object" ||
    payload === null ||
    !("active_tenant_id" in payload) ||
    payload.active_tenant_id !== normalizedId
  ) {
    throw new Error("The server did not confirm the requested organization.");
  }

  return { active_tenant_id: normalizedId };
}
