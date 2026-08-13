import { ClassroomLearningClient } from '@/components/classroom/ClassroomLearningClient'

interface ClassroomLearningPageProps {
  params: Promise<{ versionId: string }>
  searchParams: Promise<{ assignmentId?: string | string[] }>
}

export default async function ClassroomLearningPage({
  params,
  searchParams,
}: ClassroomLearningPageProps) {
  const { versionId } = await params
  const query = await searchParams
  const assignmentId = Array.isArray(query.assignmentId)
    ? query.assignmentId[0]
    : query.assignmentId
  return <ClassroomLearningClient versionId={versionId} assignmentId={assignmentId} />
}
