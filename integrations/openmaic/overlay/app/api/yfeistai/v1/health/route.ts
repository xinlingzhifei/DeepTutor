import { OPENMAIC_HEALTH_RESPONSE } from "../../../../../lib/yfeistai/contracts";

export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json(OPENMAIC_HEALTH_RESPONSE);
}
