'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  ClassroomSlideCanvas,
  readClassroomDocument,
  toRenderableClassroomScene,
  type ClassroomDocument,
  type ClassroomScene,
  type ClassroomSlideCanvasProps,
  type ClassroomThemeId,
} from '@/lib/openmaic-adapter'
import {
  createWhiteboardState,
  applyWhiteboardAction,
  reconstructWhiteboardState,
} from '@/lib/openmaic-adapter/playback/action-reducer'
import {
  createPlaybackController,
  type PlaybackController,
  type PlaybackPorts,
} from '@/lib/openmaic-adapter/playback/controller'
import { learningEvent } from '@/lib/openmaic-adapter/playback/events'
import { createQuizSubmissionCoordinator } from '@/lib/openmaic-adapter/playback/quiz-submission'
import {
  playbackInteractionKey,
  playbackStableId,
  type EffectAction,
  type PlaybackCursor,
  type PlaybackState,
} from '@/lib/openmaic-adapter/playback/types'

import { InteractiveScene, type InteractiveEvent } from './InteractiveScene'
import { PblScene } from './PblScene'
import { QuizScene, type QuizGradeResult } from './QuizScene'
import { WhiteboardLayer } from './WhiteboardLayer'

export type ClassroomPlaybackHostPorts = Pick<
  PlaybackPorts,
  'speak' | 'playVideo' | 'openDiscussion' | 'postWidgetAction' | 'commitCheckpoint'
>

export interface ClassroomPlayerProps {
  document: unknown
  ports: ClassroomPlaybackHostPorts
  sessionNonce: string
  initialCursor?: PlaybackCursor
  theme?: ClassroomThemeId
  autoStart?: boolean
  className?: string
  gradeQuiz(
    sceneId: string,
    questionId: string,
    answer: { optionIds?: string[]; text?: string },
    submissionId: string
  ): Promise<QuizGradeResult>
  handleInteractiveEvent?(
    sceneId: string,
    event: InteractiveEvent,
    operationId: string
  ): Promise<void> | void
  completePblMilestone?(
    sceneId: string,
    milestoneId: string,
    operationId: string
  ): Promise<void> | void
  onStateChange?(state: PlaybackState): void
  onError?(error: Error): void
}

function effectState(action: EffectAction): ClassroomSlideCanvasProps['effects'] {
  if (action.type === 'spotlight') {
    return {
      spotlight: {
        elementId: action.elementId,
        dimness: action.dimOpacity,
      },
    }
  }
  return { laser: { elementId: action.elementId, color: action.color } }
}

function sceneAtCursor(
  document: ClassroomDocument,
  cursor: PlaybackCursor | undefined
): ClassroomScene {
  const scenes = [...document.openmaic.scenes].sort((left, right) => left.order - right.order)
  const index = cursor ? Math.min(cursor.sceneIndex, Math.max(0, scenes.length - 1)) : 0
  const scene = scenes[index]
  if (!scene) throw new Error('classroom player requires at least one scene')
  return scene
}

function canonicalQuizAnswer(answer: { optionIds?: string[]; text?: string }): string {
  return JSON.stringify({
    optionIds: answer.optionIds ? [...answer.optionIds].sort() : undefined,
    text: answer.text,
  })
}

interface VersionPortBinding {
  key: string
  document: ClassroomDocument
  active: boolean
  ports: ClassroomPlaybackHostPorts
  gradeQuiz: ClassroomPlayerProps['gradeQuiz']
  handleInteractiveEvent: ClassroomPlayerProps['handleInteractiveEvent']
  completePblMilestone: ClassroomPlayerProps['completePblMilestone']
}

interface PendingQuizInput {
  sceneId: string
  questionId: string
  answer: { optionIds?: string[]; text?: string }
  answeredAt: string
  gradedAt?: string
}

export function ClassroomPlayer({
  document: input,
  ports,
  sessionNonce,
  initialCursor,
  theme = 'snow',
  autoStart = false,
  className = '',
  gradeQuiz,
  handleInteractiveEvent,
  completePblMilestone,
  onStateChange,
  onError,
}: ClassroomPlayerProps) {
  const { t } = useTranslation()
  const translationRef = useRef(t)
  translationRef.current = t
  const document = useMemo(() => readClassroomDocument(input), [input])
  const bindingKey = `${document.classroomVersionId}:${document.fileSha256}`
  const portBindingRef = useRef<VersionPortBinding | null>(null)
  if (portBindingRef.current?.document !== document) {
    portBindingRef.current = {
      key: bindingKey,
      document,
      active: true,
      ports,
      gradeQuiz,
      handleInteractiveEvent,
      completePblMilestone,
    }
  } else {
    portBindingRef.current.ports = ports
    portBindingRef.current.gradeQuiz = gradeQuiz
    portBindingRef.current.handleInteractiveEvent = handleInteractiveEvent
    portBindingRef.current.completePblMilestone = completePblMilestone
  }
  const portBinding = portBindingRef.current as VersionPortBinding
  const orderedScenes = useMemo(
    () => [...document.openmaic.scenes].sort((left, right) => left.order - right.order),
    [document]
  )
  const initialScene = sceneAtCursor(document, initialCursor)
  const [sceneId, setSceneId] = useState(initialScene.id)
  const [playbackState, setPlaybackState] = useState<PlaybackState>('idle')
  const [controllerReady, setControllerReady] = useState(false)
  const [playbackError, setPlaybackError] = useState<string | null>(null)
  const [effects, setEffects] = useState<ClassroomSlideCanvasProps['effects']>()
  const [whiteboard, setWhiteboard] = useState(() =>
    initialCursor
      ? reconstructWhiteboardState(document, initialCursor)
      : createWhiteboardState(`${document.classroomVersionId}-whiteboard`)
  )
  const whiteboardRef = useRef(whiteboard)
  const errorRef = useRef(onError)
  const initialCursorRef = useRef(initialCursor)
  const controllerRef = useRef<PlaybackController | null>(null)
  const controllerBindingRef = useRef<string | null>(null)
  const activePortBindingRef = useRef<VersionPortBinding | null>(null)
  const disposalTicketRef = useRef<{
    controller: PlaybackController
    cancelled: boolean
  } | null>(null)
  const quizInputsRef = useRef(new Map<string, PendingQuizInput>())
  const quizSubmissionsRef = useRef(new Map<string, string>())
  const eventTimesRef = useRef(new Map<string, string>())

  errorRef.current = onError
  initialCursorRef.current = initialCursor

  const reportError = useCallback(
    (reason: unknown) => {
      const error =
        reason instanceof Error
          ? reason
          : new Error(translationRef.current('classroom.player.failed'))
      setPlaybackError(error.message)
      errorRef.current?.(error)
    },
    []
  )

  const controller = useMemo(() => {
    const binding = portBinding
    const ensureBound = () => {
      if (!binding.active) {
        throw new Error(translationRef.current('classroom.player.failed'))
      }
    }
    let created: PlaybackController
    created = createPlaybackController(document, {
      renderScene: nextSceneId => {
        if (!binding.active) return
        setSceneId(nextSceneId)
        setEffects(undefined)
      },
      speak: (action, signal, execution) => {
        ensureBound()
        return binding.ports.speak(action, signal, execution)
      },
      playVideo: (action, signal, execution) => {
        ensureBound()
        return binding.ports.playVideo(action, signal, execution)
      },
      applyWhiteboard: async action => {
        ensureBound()
        const next = applyWhiteboardAction(whiteboardRef.current, action)
        whiteboardRef.current = next
        setWhiteboard(next)
      },
      applyEffect: action => {
        ensureBound()
        setEffects(effectState(action))
      },
      openDiscussion: (action, execution) => {
        ensureBound()
        return binding.ports.openDiscussion(action, execution)
      },
      postWidgetAction: (action, execution) => {
        ensureBound()
        return binding.ports.postWidgetAction(action, execution)
      },
      commitCheckpoint: async checkpoint => {
        ensureBound()
        await binding.ports.commitCheckpoint(checkpoint)
      },
    })
    return created
  }, [document, portBinding])

  useEffect(() => {
    let cancelled = false
    const previousPortBinding = activePortBindingRef.current
    if (previousPortBinding && previousPortBinding !== portBinding) {
      previousPortBinding.active = false
    }
    if (disposalTicketRef.current?.controller === controller) {
      disposalTicketRef.current.cancelled = true
      disposalTicketRef.current = null
    }
    const previousController = controllerRef.current
    const previousCursor =
      previousController && controllerBindingRef.current === bindingKey
        ? previousController.snapshot()
        : initialCursorRef.current
    if (controllerBindingRef.current !== bindingKey) {
      quizInputsRef.current.clear()
      quizSubmissionsRef.current.clear()
      eventTimesRef.current.clear()
    }
    setControllerReady(false)
    setPlaybackState('switching')

    const activate = async () => {
      if (previousController && previousController !== controller) {
        await previousController.dispose()
      } else if (previousController === controller) {
        await controller.drain()
      }
      if (cancelled) {
        portBinding.active = false
        await controller.dispose()
        return
      }
      if (previousCursor) controller.restore(previousCursor)
      const durableCursor = controller.snapshot()
      const restoredWhiteboard = reconstructWhiteboardState(document, durableCursor)
      whiteboardRef.current = restoredWhiteboard
      setWhiteboard(restoredWhiteboard)
      controllerRef.current = controller
      controllerBindingRef.current = bindingKey
      activePortBindingRef.current = portBinding
      const current = sceneAtCursor(document, durableCursor)
      setSceneId(current.id)
      setPlaybackState(controller.state)
      setPlaybackError(null)
      setControllerReady(true)
    }
    void activate().catch(reason => {
      if (!cancelled) reportError(reason)
    })
    return () => {
      cancelled = true
      void controller.stop().catch(() => undefined)
      const ticket = { controller, cancelled: false }
      disposalTicketRef.current = ticket
      window.setTimeout(() => {
        if (!ticket.cancelled) {
          if (activePortBindingRef.current === portBinding) {
            portBinding.active = false
          }
          void controller.dispose().catch(() => undefined)
        }
      }, 0)
    }
  }, [bindingKey, controller, document, portBinding, reportError])

  const ensureCurrentBinding = useCallback(() => {
    if (
      !portBinding.active ||
      activePortBindingRef.current !== portBinding ||
      controllerRef.current !== controller
    ) {
      throw new Error(translationRef.current('classroom.player.failed'))
    }
  }, [controller, portBinding])

  useEffect(() => {
    onStateChange?.(playbackState)
  }, [onStateChange, playbackState])

  useEffect(() => {
    if (
      !autoStart ||
      !controllerReady ||
      controllerRef.current !== controller ||
      controller.state === 'running' ||
      controller.state === 'completed'
    ) {
      return
    }
    let mounted = true
    setPlaybackState('running')
    void controller
      .start()
      .catch(reason => {
        if (mounted) reportError(reason)
      })
      .finally(() => {
        if (mounted) setPlaybackState(controller.state)
      })
    return () => {
      mounted = false
      void controller.stop().catch(() => undefined)
    }
  }, [autoStart, controller, controllerReady, reportError])

  const sceneIndex = Math.max(
    0,
    orderedScenes.findIndex(scene => scene.id === sceneId)
  )
  const scene = orderedScenes[sceneIndex] ?? initialScene
  const controlsDisabled = !controllerReady || playbackState === 'switching'
  const consumedInteractionKeys = new Set(controller.snapshot().consumed)
  const isConsumed = (interactionId: string) =>
    consumedInteractionKeys.has(playbackInteractionKey(scene.id, interactionId))

  const startOrPause = async () => {
    if (!controllerReady || playbackState === 'switching') return
    setPlaybackError(null)
    if (controller.state === 'running') {
      await controller.pause()
      setPlaybackState(controller.state)
      return
    }
    setPlaybackState('running')
    try {
      if (controller.state === 'paused') await controller.resume()
      else await controller.start()
    } catch (reason) {
      reportError(reason)
    } finally {
      setPlaybackState(controller.state)
    }
  }

  const stop = async () => {
    if (!controllerReady || playbackState === 'switching') return
    try {
      await controller.stop()
    } catch (reason) {
      reportError(reason)
    } finally {
      setPlaybackState(controller.state)
    }
  }

  const switchScene = async (nextIndex: number) => {
    if (!controllerReady || playbackState === 'switching') return
    setPlaybackError(null)
    setPlaybackState('switching')
    try {
      await controller.switchScene(nextIndex)
      const restored = reconstructWhiteboardState(document, controller.snapshot())
      whiteboardRef.current = restored
      setWhiteboard(restored)
    } catch (reason) {
      reportError(reason)
    } finally {
      setPlaybackState(controller.state)
    }
  }

  const quizCoordinator = useMemo(
    () =>
      createQuizSubmissionCoordinator<QuizGradeResult>({
        commitAnswered: async submissionId => {
          ensureCurrentBinding()
          const pending = quizInputsRef.current.get(submissionId)
          if (!pending) throw new Error('quiz submission input is unavailable')
          await controller.commitEvents(`quiz-answered:${submissionId}`, [
            learningEvent(
              'quiz.answered',
              document.classroomVersionId,
              pending.answeredAt,
              {
                eventId: playbackStableId('event', submissionId, 'quiz.answered'),
                sceneId: pending.sceneId,
                interactionId: pending.questionId,
                payload: pending.answer,
              }
            ),
          ])
        },
        grade: async submissionId => {
          ensureCurrentBinding()
          const pending = quizInputsRef.current.get(submissionId)
          if (!pending) throw new Error('quiz submission input is unavailable')
          const grade = await portBinding.gradeQuiz(
            pending.sceneId,
            pending.questionId,
            pending.answer,
            submissionId
          )
          ensureCurrentBinding()
          if ((grade as { status: string }).status !== 'graded') {
            throw new Error(translationRef.current('classroom.player.quizFinalMissing'))
          }
          return grade
        },
        commitGraded: async (grade, submissionId) => {
          ensureCurrentBinding()
          const pending = quizInputsRef.current.get(submissionId)
          if (!pending) throw new Error('quiz submission input is unavailable')
          pending.gradedAt ??= new Date().toISOString()
          await controller.markConsumed(pending.sceneId, pending.questionId, [
            learningEvent(
              'quiz.graded',
              document.classroomVersionId,
              pending.gradedAt,
              {
                eventId: playbackStableId('event', submissionId, 'quiz.graded'),
                sceneId: pending.sceneId,
                interactionId: pending.questionId,
                payload: {
                  attemptId: grade.attemptId,
                  status: grade.status,
                  score: grade.score,
                },
              }
            ),
          ])
        },
      }),
    [controller, document.classroomVersionId, ensureCurrentBinding, portBinding]
  )

  const submitQuiz = async (
    questionId: string,
    answer: { optionIds?: string[]; text?: string }
  ) => {
    if (!controllerReady || playbackState === 'switching') {
      throw new Error(translationRef.current('classroom.player.failed'))
    }
    ensureCurrentBinding()
    const questionKey = playbackInteractionKey(scene.id, questionId)
    const submissionId =
      quizSubmissionsRef.current.get(questionKey) ??
      playbackStableId(
        'quiz-submission',
        document.classroomVersionId,
        document.fileSha256,
        scene.id,
        questionId,
        canonicalQuizAnswer(answer)
      )
    if (!quizInputsRef.current.has(submissionId)) {
      quizSubmissionsRef.current.set(questionKey, submissionId)
      quizInputsRef.current.set(submissionId, {
        sceneId: scene.id,
        questionId,
        answer,
        answeredAt: new Date().toISOString(),
      })
    }
    const grade = await quizCoordinator.submit(submissionId)
    quizInputsRef.current.delete(submissionId)
    quizSubmissionsRef.current.delete(questionKey)
    return grade
  }

  const receiveInteractive = async (event: InteractiveEvent) => {
    if (!controllerReady || playbackState === 'switching') {
      throw new Error(translationRef.current('classroom.player.failed'))
    }
    ensureCurrentBinding()
    const operationId = playbackStableId(
      'interactive-event',
      document.classroomVersionId,
      document.fileSha256,
      scene.id,
      event.eventId
    )
    const learningEventId = playbackStableId('event', operationId, 'interactive.event')
    const eventTime = eventTimesRef.current.get(learningEventId) ?? new Date().toISOString()
    eventTimesRef.current.set(learningEventId, eventTime)
    await portBinding.handleInteractiveEvent?.(scene.id, event, operationId)
    ensureCurrentBinding()
    const completedEvent = learningEvent(
      'interactive.event',
      document.classroomVersionId,
      eventTime,
      {
        eventId: learningEventId,
        sceneId: scene.id,
        interactionId: scene.id,
        payload: { type: event.type, payload: event.payload },
      }
    )
    if (event.type === 'interactive.completed') {
      await controller.markConsumed(scene.id, scene.id, [completedEvent])
    } else {
      await controller.commitEvents(`interactive:${operationId}`, [completedEvent])
    }
    eventTimesRef.current.delete(learningEventId)
  }

  const completeMilestone = async (milestoneId: string) => {
    if (!controllerReady || playbackState === 'switching') {
      throw new Error(translationRef.current('classroom.player.failed'))
    }
    ensureCurrentBinding()
    const operationId = playbackStableId(
      'pbl-milestone',
      document.classroomVersionId,
      document.fileSha256,
      scene.id,
      milestoneId
    )
    const eventId = playbackStableId('event', operationId, 'pbl.milestone')
    const eventTime = eventTimesRef.current.get(eventId) ?? new Date().toISOString()
    eventTimesRef.current.set(eventId, eventTime)
    await portBinding.completePblMilestone?.(scene.id, milestoneId, operationId)
    ensureCurrentBinding()
    await controller.markConsumed(scene.id, milestoneId, [
      learningEvent('pbl.milestone', document.classroomVersionId, eventTime, {
        eventId,
        sceneId: scene.id,
        interactionId: milestoneId,
      }),
    ])
    eventTimesRef.current.delete(eventId)
  }

  const sceneBody = (() => {
    if (scene.type === 'slide') {
      const renderable = toRenderableClassroomScene(scene, { theme })
      if (renderable.type !== 'slide') {
        throw new Error('slide scene could not be rendered')
      }
      return (
        <ClassroomSlideCanvas
          slide={renderable.content.canvas}
          effects={effects}
          className="h-full min-h-[20rem] w-full"
        />
      )
    }
    if (scene.type === 'quiz') {
      return (
        <QuizScene
          key={`${bindingKey}:${scene.id}`}
          content={scene.content}
          disabled={controlsDisabled}
          submittedQuestionIds={scene.content.questions
            .filter(question => isConsumed(question.id))
            .map(question => question.id)}
          onSubmit={submitQuiz}
        />
      )
    }
    if (scene.type === 'interactive') {
      return (
        <InteractiveScene
          key={`${bindingKey}:${scene.id}`}
          content={scene.content}
          disabled={controlsDisabled}
          frameInstanceId={`frame-${document.fileSha256}-${controller.snapshot().revision}`}
          sessionNonce={sessionNonce}
          title={scene.title}
          onEvent={receiveInteractive}
          onError={reportError}
        />
      )
    }
    return (
      <PblScene
        key={`${bindingKey}:${scene.id}`}
        content={scene.content}
        disabled={controlsDisabled}
        completedMilestoneIds={scene.content.milestones
          .filter(milestone => isConsumed(milestone.id))
          .map(milestone => milestone.id)}
        onCompleteMilestone={completeMilestone}
      />
    )
  })()

  return (
    <section
      className={`overflow-hidden rounded-3xl border border-[var(--border)] bg-[var(--background)] shadow-sm ${className}`}
      data-playback-state={playbackState}
    >
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-[var(--border)] px-4 py-3">
        <div className="min-w-0">
          <p className="text-xs text-[var(--muted-foreground)]">
            {t('classroom.player.sceneProgress', {
              current: sceneIndex + 1,
              total: orderedScenes.length,
            })}
          </p>
          <h2 className="truncate text-sm font-semibold text-[var(--foreground)]">{scene.title}</h2>
        </div>
        <div className="flex max-w-full items-center gap-2 overflow-x-auto pb-1">
          <button
            type="button"
            aria-label={t('classroom.player.previousScene')}
            disabled={controlsDisabled || sceneIndex === 0}
            onClick={() => void switchScene(sceneIndex - 1)}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-40"
          >
            {t('classroom.player.previous')}
          </button>
          <button
            type="button"
            onClick={() => void startOrPause()}
            disabled={controlsDisabled || playbackState === 'completed'}
            className="rounded-lg bg-[var(--primary)] px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {playbackState === 'running' ? t('classroom.player.pause') : t('classroom.player.play')}
          </button>
          <button
            type="button"
            onClick={() => void stop()}
            disabled={
              controlsDisabled ||
              playbackState === 'idle' ||
              playbackState === 'stopped' ||
              playbackState === 'completed'
            }
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-40"
          >
            {t('classroom.player.stop')}
          </button>
          <button
            type="button"
            aria-label={t('classroom.player.nextScene')}
            disabled={controlsDisabled || sceneIndex === orderedScenes.length - 1}
            onClick={() => void switchScene(sceneIndex + 1)}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-40"
          >
            {t('classroom.player.next')}
          </button>
        </div>
      </header>

      <div className="relative min-h-[24rem] p-4">
        {playbackError && (
          <p
            className="mb-3 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700"
            role="alert"
          >
            {playbackError}
          </p>
        )}
        {sceneBody}
        <WhiteboardLayer state={whiteboard} />
      </div>
    </section>
  )
}
