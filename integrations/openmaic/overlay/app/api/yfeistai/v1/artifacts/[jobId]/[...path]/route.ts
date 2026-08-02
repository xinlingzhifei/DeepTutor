import {
  artifactStore,
  createArtifactGetHandler,
} from "../../../../../../../lib/yfeistai/artifact-manifest";
import { readServiceSecret } from "../../../../../../../lib/yfeistai/service-auth";

const getArtifact = createArtifactGetHandler({
  readSecret: readServiceSecret,
  store: artifactStore,
});

export async function GET(
  request: Request,
  context: {
    params: Promise<{ jobId: string; path: string[] }>;
  },
): Promise<Response> {
  return getArtifact(request, context);
}
