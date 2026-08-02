function isAllowedNextDevelopmentSocket(
  webSocketUrl: string,
  pageUrl: URL,
): boolean {
  let candidate: URL;
  try {
    candidate = new URL(webSocketUrl);
  } catch {
    return false;
  }
  const expectedProtocol = pageUrl.protocol === "https:" ? "wss:" : "ws:";
  return (
    candidate.protocol === expectedProtocol &&
    candidate.host === pageUrl.host &&
    candidate.username === "" &&
    candidate.password === "" &&
    candidate.pathname === "/_next/webpack-hmr" &&
    candidate.hash === ""
  );
}

export function assertOnlyClassroomInfrastructureWebSockets(
  webSocketUrls: readonly string[],
  classroomBaseUrl: string,
): void {
  const pageUrl = new URL(classroomBaseUrl);
  if (pageUrl.protocol !== "http:" && pageUrl.protocol !== "https:") {
    throw new Error("Classroom visual fixture base URL must use HTTP or HTTPS");
  }
  const unexpected = webSocketUrls.filter(
    webSocketUrl => !isAllowedNextDevelopmentSocket(webSocketUrl, pageUrl),
  );
  if (unexpected.length === 0) return;
  throw new Error(
    `Classroom visual fixture opened unexpected WebSocket connections: ${unexpected.join(", ")}`,
  );
}
