/** Client presentation state never grants knowledge-management privileges. */

export function operationPhase(state: string): "running" | "success" | "failed" | "attention" {
  if (["INTENT_RECORDED", "SEND_STARTED", "ACKNOWLEDGED", "RECONCILING"].includes(state))
    return "running";
  if (state === "RECONCILED") return "success";
  if (state === "FAILED") return "failed";
  return "attention";
}

export function shouldPollOperations(operations: { state: string }[]): boolean {
  return operations.some(
    ({ state }) => operationPhase(state) === "running" || state === "RESULT_UNKNOWN",
  );
}

export function managementAccess(
  local: { id?: string; assigned?: boolean; read_only?: boolean },
  binding: { id: string; can_manage: boolean; can_write: boolean } | null,
) {
  const manage = Boolean(
    local.id &&
      !local.assigned &&
      !local.read_only &&
      binding?.id === local.id &&
      binding.can_manage === true,
  );
  return { manage, write: manage && binding?.can_write === true };
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value)
      .filter(([, item]) => item !== undefined)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

export class IntentKeys {
  private readonly entries = new Map<string, { key: string; operations: string[] }>();
  private readonly uncertain = new Set<string>();
  constructor(private readonly createKey: () => string) {}

  get(action: string, payload: unknown) {
    const signature = `${action}:${canonical(payload)}`;
    let entry = this.entries.get(signature);
    if (!entry) {
      entry = { key: this.createKey(), operations: [] };
      this.entries.set(signature, entry);
    }
    return entry.key;
  }

  accept(action: string, payload: unknown, operations: string[]) {
    const signature = `${action}:${canonical(payload)}`;
    const entry = this.entries.get(signature);
    if (entry) entry.operations = operations;
    if (operations.length) this.uncertain.delete(signature);
  }

  markUncertain(action: string, payload: unknown) {
    this.uncertain.add(`${action}:${canonical(payload)}`);
  }

  canSubmit(action: string, payload: unknown): boolean {
    return !this.uncertain.size || this.uncertain.has(`${action}:${canonical(payload)}`);
  }

  observe(operations: { operation_id: string; state: string }[]) {
    const states = new Map(
      operations.map((operation) => [operation.operation_id, operation.state]),
    );
    for (const [signature, entry] of this.entries) {
      if (
        entry.operations.length &&
        entry.operations.every((id) => ["RECONCILED", "FAILED"].includes(states.get(id) ?? ""))
      )
        this.entries.delete(signature);
    }
  }
}

export function parseObjectJson(input: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(input.trim() || "{}");
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    throw new Error("Expected a JSON object");
  return parsed as Record<string, unknown>;
}
