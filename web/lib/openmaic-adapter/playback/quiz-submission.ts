export interface QuizSubmissionCoordinatorPorts<TGrade> {
  commitAnswered(submissionId: string): Promise<void>
  grade(submissionId: string): Promise<TGrade>
  commitGraded(grade: TGrade, submissionId: string): Promise<void>
}

export interface QuizSubmissionCoordinator<TGrade> {
  submit(submissionId: string): Promise<TGrade>
}

interface PendingQuizSubmission<TGrade> {
  answeredCommitted: boolean
  grade?: TGrade
}

/**
 * Keeps a final grade in memory until its durable consumed/event checkpoint succeeds. The caller's
 * submission id must also be used as the grading endpoint's idempotency key for process recovery.
 */
export function createQuizSubmissionCoordinator<TGrade>(
  ports: QuizSubmissionCoordinatorPorts<TGrade>
): QuizSubmissionCoordinator<TGrade> {
  const pending = new Map<string, PendingQuizSubmission<TGrade>>()
  const tails = new Map<string, Promise<void>>()

  const serialize = async <T>(submissionId: string, operation: () => Promise<T>): Promise<T> => {
    const previous = tails.get(submissionId) ?? Promise.resolve()
    let release!: () => void
    const current = new Promise<void>(resolve => {
      release = resolve
    })
    const queued = previous.then(() => current)
    tails.set(submissionId, queued)
    await previous
    try {
      return await operation()
    } finally {
      release()
      if (tails.get(submissionId) === queued) tails.delete(submissionId)
    }
  }

  return {
    submit(submissionId) {
      if (!submissionId || submissionId.includes('\0')) {
        return Promise.reject(new Error('quiz submission id is invalid'))
      }
      return serialize(submissionId, async () => {
        const submission = pending.get(submissionId) ?? { answeredCommitted: false }
        pending.set(submissionId, submission)
        if (!submission.answeredCommitted) {
          await ports.commitAnswered(submissionId)
          submission.answeredCommitted = true
        }
        if (!('grade' in submission)) {
          submission.grade = await ports.grade(submissionId)
        }
        const grade = submission.grade as TGrade
        await ports.commitGraded(grade, submissionId)
        pending.delete(submissionId)
        return grade
      })
    },
  }
}
