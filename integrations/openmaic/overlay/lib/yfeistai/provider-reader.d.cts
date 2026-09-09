export function readConfiguration(): {
  scope: "shared" | "dedicated";
  tenantId: string | null;
  profileId: string;
  defaultModel: string;
  providers: Record<string, { apiKey: string; baseUrl: string }>;
} | null;
