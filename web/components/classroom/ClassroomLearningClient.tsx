'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  ClassroomPlayer,
  type ClassroomPlaybackHostPorts,
} from '@/components/classroom/ClassroomPlayer'
import { LearningProgressPanel } from '@/components/classroom/LearningProgressPanel'
import {
  createClassroomEventDispatcher,
  createClassroomEventTranslator,
  type EventQuarantinedResult,
  type LearningEvent,
  type LearningEventQueueStorage,
} from '@/lib/classroom-events'
import {
  appendClassroomEvents,
  classroomSessionNeedsCompletionRecovery,
  completeClassroomLearningSession,
  fetchClassroomLearningDocument,
  fetchClassroomLearningMedia,
  resolveStudentClassroomAuthority,
  restoreOrCreateClassroomLearningSession,
  updateClassroomLearningCursor,
  type ClassroomLearningSession,
  type ClassroomSessionAuthority,
} from '@/lib/learning-api'
import {
  readClassroomDocument,
  type ClassroomDocument,
  type PlaybackCheckpoint,
  type PlaybackCursor,
} from '@/lib/openmaic-adapter'

interface ClassroomLearningClientProps {
  versionId: string
  assignmentId?: string
}

type EventDispatcher = ReturnType<typeof createClassroomEventDispatcher>
const LOAD_FAILED_KEY = 'classroom.learning.loadFailed'

function sessionStorageKey(versionId: string, authority: ClassroomSessionAuthority): string {
  const reference = authority.assignmentId ?? authority.studentAssetId
  return `yfeistai.classroom.session:${encodeURIComponent(versionId)}:${encodeURIComponent(reference)}`
}

function eventStorage(sessionId: string): LearningEventQueueStorage {
  const key = `yfeistai.classroom.events:${encodeURIComponent(sessionId)}`
  return {
    load: () => {
      try {
        const value = JSON.parse(localStorage.getItem(key) ?? '[]')
        return Array.isArray(value) ? (value as LearningEvent[]) : []
      } catch {
        return []
      }
    },
    save: events => localStorage.setItem(key, JSON.stringify(events)),
  }
}

async function resolveAuthority(
  versionId: string,
  assignmentId?: string
): Promise<ClassroomSessionAuthority> {
  if (assignmentId?.trim()) return { assignmentId: assignmentId.trim() }
  const authority = await resolveStudentClassroomAuthority(versionId)
  if (!authority) throw new Error('classroom learning authority is unavailable')
  return authority
}

export function ClassroomLearningClient({
  versionId,
  assignmentId,
}: ClassroomLearningClientProps) {
  const { t } = useTranslation()
  const [session, setSession] = useState<ClassroomLearningSession | null>(null)
  const [document, setDocument] = useState<unknown>(null)
  const [parsedDocument, setParsedDocument] = useState<ClassroomDocument | null>(null)
  const [mediaUrls, setMediaUrls] = useState<ReadonlyMap<string, string>>(new Map())
  const [dispatcher, setDispatcher] = useState<EventDispatcher | null>(null)
  const [pendingEvents, setPendingEvents] = useState(0)
  const [diagnostics, setDiagnostics] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const translatorRef = useRef<ReturnType<typeof createClassroomEventTranslator> | null>(null)
  const sessionRef = useRef<ClassroomLearningSession | null>(null)
  const loadGenerationRef = useRef(0)
  sessionRef.current = session

  const load = useCallback(async () => {
    const generation = ++loadGenerationRef.current
    const candidateUrls: string[] = []
    let candidateDispatcher: EventDispatcher | null = null
    const discardCandidate = () => {
      candidateDispatcher?.dispose()
      for (const url of candidateUrls) URL.revokeObjectURL(url)
    }
    setLoading(true)
    setError(null)
    setDiagnostics([])
    try {
      const authority = await resolveAuthority(versionId, assignmentId)
      const storageKey = sessionStorageKey(versionId, authority)
      let restored = await restoreOrCreateClassroomLearningSession(
        versionId,
        authority,
        localStorage.getItem(storageKey)
      )
      localStorage.setItem(storageKey, restored.id)
      const rawDocument = await fetchClassroomLearningDocument(restored.id, versionId)
      const verifiedDocument = readClassroomDocument(rawDocument)
      const blobs: Array<readonly [string, string]> = []
      for (const item of verifiedDocument.mediaManifest) {
        const url = URL.createObjectURL(
          await fetchClassroomLearningMedia(restored.id, versionId, item.mediaId)
        )
        candidateUrls.push(url)
        blobs.push([item.mediaId, url])
      }
      const urls = new Map(blobs)
      candidateDispatcher = createClassroomEventDispatcher({
        storage: eventStorage(restored.id),
        send: events => appendClassroomEvents(restored.id, events),
        onQuarantined: (item: EventQuarantinedResult) => {
          if (generation === loadGenerationRef.current) {
            setDiagnostics(current => [
              ...new Set([...current, `${item.event_id}: ${item.reason}`]),
            ])
          }
        },
        onChange: size => {
          if (generation === loadGenerationRef.current) setPendingEvents(size)
        },
      })
      if (generation !== loadGenerationRef.current) {
        discardCandidate()
        return
      }
      const queue = candidateDispatcher
      if (
        classroomSessionNeedsCompletionRecovery(
          restored,
          verifiedDocument.openmaic.scenes.length
        )
      ) {
        await queue.flush()
        restored = await completeClassroomLearningSession(restored.id)
      }
      if (generation !== loadGenerationRef.current) {
        discardCandidate()
        return
      }
      translatorRef.current = createClassroomEventTranslator(
        verifiedDocument.knowledgePointMappings,
        restored.lastCursor !== null,
        restored.id
      )
      setSession(restored)
      setDocument(rawDocument)
      setParsedDocument(verifiedDocument)
      setMediaUrls(urls)
      setDispatcher(queue)
      setPendingEvents(queue.size)
      candidateDispatcher = null
      candidateUrls.length = 0
    } catch (reason) {
      discardCandidate()
      if (generation === loadGenerationRef.current) {
        setError(reason instanceof Error ? reason.message : LOAD_FAILED_KEY)
      }
    } finally {
      if (generation === loadGenerationRef.current) setLoading(false)
    }
  }, [assignmentId, versionId])

  useEffect(() => {
    void load()
    return () => {
      loadGenerationRef.current += 1
    }
  }, [load])

  useEffect(
    () => () => {
      dispatcher?.dispose()
      for (const url of mediaUrls.values()) URL.revokeObjectURL(url)
    },
    [dispatcher, mediaUrls]
  )

  const mediaUrl = useCallback(
    (mediaId: string) => {
      const url = mediaUrls.get(mediaId)
      if (!url) throw new Error('classroom media is unavailable')
      return url
    },
    [mediaUrls]
  )

  const commitCheckpoint = useCallback(
    async (checkpoint: PlaybackCheckpoint) => {
      const activeSession = sessionRef.current
      const activeTranslator = translatorRef.current
      if (!dispatcher || !activeTranslator || !activeSession) {
        throw new Error('classroom learning session is not ready')
      }
      const events = (await Promise.all(
        checkpoint.events.map(event => activeTranslator.translate(event))
      )).filter((event): event is LearningEvent => event !== null)
      await dispatcher.commit(events, async () => {
        const updated = await updateClassroomLearningCursor(
          activeSession.id,
          checkpoint.cursor as unknown as Record<string, unknown>
        )
        if (sessionRef.current?.id === activeSession.id) {
          setSession(updated)
          setPendingEvents(dispatcher.size)
        }
      })
      if (events.some(event => event.event_type === 'classroom.completed')) {
        await dispatcher.flush()
        const completed = await completeClassroomLearningSession(activeSession.id)
        if (sessionRef.current?.id === activeSession.id) setSession(completed)
      }
      if (sessionRef.current?.id === activeSession.id) setPendingEvents(dispatcher.size)
    },
    [dispatcher]
  )

  const ports = useMemo<ClassroomPlaybackHostPorts>(
    () => ({
      speak: async () => undefined,
      playVideo: async () => undefined,
      openDiscussion: async () => undefined,
      postWidgetAction: async () => undefined,
      commitCheckpoint,
    }),
    [commitCheckpoint]
  )

  if (loading) {
    return <p className="p-8 text-sm text-[var(--muted-foreground)]">{t('classroom.learning.loading')}</p>
  }
  if (error || !session || !document || !parsedDocument || !dispatcher) {
    return (
      <section className="m-6 rounded-2xl border border-red-300 bg-red-50 p-5 text-red-800">
        <p role="alert">{error === LOAD_FAILED_KEY ? t(error) : error ?? t(LOAD_FAILED_KEY)}</p>
        <button
          type="button"
          onClick={() => void load()}
          className="mt-3 rounded-lg border border-current px-3 py-1.5 text-sm font-medium"
        >
          {t('classroom.learning.retry')}
        </button>
      </section>
    )
  }

  return (
    <div className="h-full overflow-y-auto p-4 sm:p-6">
      <div className="mx-auto grid max-w-7xl gap-5 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <ClassroomPlayer
          document={document}
          ports={ports}
          sessionNonce={session.id}
          initialCursor={session.lastCursor as unknown as PlaybackCursor | undefined}
          readOnly={session.status !== 'active'}
          mediaUrl={mediaUrl}
          autoStart={false}
          gradeQuiz={async (_sceneId, _questionId, _answer, submissionId) => ({
            attemptId: submissionId,
            status: 'graded',
            score: null,
          })}
          onError={reason => setError(reason.message)}
        />
        <LearningProgressPanel
          session={session}
          totalScenes={parsedDocument.openmaic.scenes.length}
          pendingEvents={pendingEvents}
          diagnostics={diagnostics}
        />
      </div>
    </div>
  )
}
