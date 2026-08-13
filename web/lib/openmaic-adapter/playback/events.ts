export type ClassroomLearningEventType =
  | 'scene.entered'
  | 'scene.completed'
  | 'action.completed'
  | 'quiz.answered'
  | 'quiz.graded'
  | 'hint.used'
  | 'interactive.event'
  | 'pbl.milestone'
  | 'classroom.completed'

export interface ClassroomLearningEvent {
  /** Hosts deduplicate this key globally within the classroom version. */
  eventId: string
  type: ClassroomLearningEventType
  classroomVersionId: string
  sceneId?: string
  actionId?: string
  interactionId?: string
  occurredAt: string
  payload?: Readonly<Record<string, unknown>>
}

export function learningEvent(
  type: ClassroomLearningEventType,
  classroomVersionId: string,
  occurredAt: string,
  fields: Omit<ClassroomLearningEvent, 'type' | 'classroomVersionId' | 'occurredAt'>
): ClassroomLearningEvent {
  if (!classroomVersionId || !fields.eventId || !Number.isFinite(Date.parse(occurredAt))) {
    throw new Error('learning event binding is invalid')
  }
  return { type, classroomVersionId, occurredAt, ...fields }
}
