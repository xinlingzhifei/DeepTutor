import {
  contentJobStore,
  createClassroomGetHandler,
} from "../../../../../../lib/yfeistai/content-generation";
import { readServiceSecret } from "../../../../../../lib/yfeistai/service-auth";

const getClassroom = createClassroomGetHandler({
  readSecret: readServiceSecret,
  store: contentJobStore,
});

export async function GET(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  return getClassroom(request, context);
}
