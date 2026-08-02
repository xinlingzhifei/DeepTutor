export type StableProviderErrorCode =
  | "provider_429"
  | "provider_5xx"
  | "connect_timeout"
  | "read_timeout";

export interface StableProviderFailure {
  code: StableProviderErrorCode;
  message: string;
}

const MAX_ERROR_RECORDS = 16;
const MAX_ERROR_DEPTH = 4;

function errorRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function errorChain(error: unknown): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = [];
  const queued: Array<{ record: Record<string, unknown>; depth: number }> = [];
  const seen = new Set<Record<string, unknown>>();
  const enqueue = (value: unknown, depth: number) => {
    if (
      depth > MAX_ERROR_DEPTH ||
      records.length + queued.length >= MAX_ERROR_RECORDS
    ) {
      return;
    }
    const record = errorRecord(value);
    if (!record || seen.has(record)) {
      return;
    }
    seen.add(record);
    queued.push({ record, depth });
  };

  enqueue(error, 0);
  while (queued.length > 0 && records.length < MAX_ERROR_RECORDS) {
    const current = queued.shift()!;
    records.push(current.record);
    if (current.depth >= MAX_ERROR_DEPTH) {
      continue;
    }
    for (const key of ["cause", "lastError"] as const) {
      try {
        enqueue(current.record[key], current.depth + 1);
      } catch {
        // A hostile error getter must not break stable error classification.
      }
    }
    let errors: unknown;
    try {
      errors = current.record.errors;
    } catch {
      errors = undefined;
    }
    if (Array.isArray(errors)) {
      for (const child of errors) {
        enqueue(child, current.depth + 1);
        if (records.length + queued.length >= MAX_ERROR_RECORDS) {
          break;
        }
      }
    }
  }
  return records;
}

function recordValue(
  record: Record<string, unknown>,
  key: string,
): unknown {
  try {
    return record[key];
  } catch {
    return undefined;
  }
}

function providerStatuses(records: Record<string, unknown>[]): number[] {
  const statuses: number[] = [];
  for (const record of records) {
    const response = errorRecord(recordValue(record, "response"));
    for (const value of [
      recordValue(record, "status"),
      recordValue(record, "statusCode"),
      response ? recordValue(response, "status") : undefined,
    ]) {
      if (typeof value === "number" && Number.isInteger(value)) {
        statuses.push(value);
      }
    }
  }
  return statuses;
}

export function classifyProviderFailure(
  error: unknown,
): StableProviderFailure | null {
  const records = errorChain(error);
  const statuses = providerStatuses(records);
  if (statuses.includes(429)) {
    return {
      code: "provider_429",
      message: "Provider rate limit exceeded.",
    };
  }
  if (statuses.includes(408)) {
    return {
      code: "read_timeout",
      message: "Provider response timed out.",
    };
  }
  if (statuses.some((status) => status >= 500 && status <= 599)) {
    return {
      code: "provider_5xx",
      message: "Provider service failed.",
    };
  }

  const markers = records.flatMap((record) =>
    [recordValue(record, "code"), recordValue(record, "name")]
      .filter((value): value is string => typeof value === "string")
      .map((value) => value.toUpperCase()),
  );
  if (
    markers.some((marker) =>
      [
        "CONNECT_TIMEOUT",
        "UND_ERR_CONNECT_TIMEOUT",
        "ECONNREFUSED",
        "ENETUNREACH",
        "EHOSTUNREACH",
      ].includes(marker),
    )
  ) {
    return {
      code: "connect_timeout",
      message: "Provider connection timed out.",
    };
  }
  if (
    markers.some((marker) =>
      [
        "READ_TIMEOUT",
        "ABORTERROR",
        "TIMEOUTERROR",
        "UND_ERR_HEADERS_TIMEOUT",
        "UND_ERR_BODY_TIMEOUT",
        "ERR_HTTP_REQUEST_TIMEOUT",
        "ETIMEDOUT",
        "ECONNRESET",
        "UND_ERR_SOCKET",
      ].includes(marker),
    )
  ) {
    return {
      code: "read_timeout",
      message: "Provider response timed out.",
    };
  }
  return null;
}
