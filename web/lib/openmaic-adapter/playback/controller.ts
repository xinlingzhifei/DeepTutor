import type { ClassroomDocument, ClassroomScene } from '../contracts'
import { learningEvent, type ClassroomLearningEvent } from './events'
import {
  playbackInteractionKey,
  playbackStableId,
  readPlaybackAction,
  readPlaybackInteractionKey,
  type PlaybackActionExecution,
  type PlaybackCheckpoint,
  type PlaybackCursor,
  type PlaybackPendingAction,
  type PlaybackPorts,
  type PlaybackState,
} from './types'

export type { PlaybackPorts } from './types'

export interface PlaybackController {
  readonly state: PlaybackState
  snapshot(): PlaybackCursor
  restore(cursor: PlaybackCursor): void
  start(): Promise<void>
  pause(): Promise<void>
  resume(): Promise<void>
  stop(): Promise<void>
  switchScene(sceneIndex: number): Promise<void>
  commitEvents(operationId: string, events: readonly ClassroomLearningEvent[]): Promise<void>
  markConsumed(
    sceneId: string,
    interactionId: string,
    events?: readonly ClassroomLearningEvent[]
  ): Promise<void>
  drain(): Promise<void>
  dispose(): Promise<void>
}

export interface PlaybackControllerOptions {
  now?: () => Date
}

interface CursorMutation {
  cursor: PlaybackCursor
  events?: readonly ClassroomLearningEvent[]
}

function copyCursor(cursor: PlaybackCursor): PlaybackCursor {
  return {
    classroomVersionId: cursor.classroomVersionId,
    documentFingerprint: cursor.documentFingerprint,
    revision: cursor.revision,
    sceneIndex: cursor.sceneIndex,
    actionIndex: cursor.actionIndex,
    consumed: [...cursor.consumed],
    enteredSceneId: cursor.enteredSceneId,
    pendingAction: cursor.pendingAction ? { ...cursor.pendingAction } : null,
  }
}

function initialCursor(document: ClassroomDocument): PlaybackCursor {
  return {
    classroomVersionId: document.classroomVersionId,
    documentFingerprint: document.fileSha256,
    revision: 0,
    sceneIndex: 0,
    actionIndex: 0,
    consumed: [],
    enteredSceneId: null,
    pendingAction: null,
  }
}

function validateCursor(
  cursor: PlaybackCursor,
  document: ClassroomDocument,
  scenes: readonly ClassroomScene[]
): PlaybackCursor {
  const sceneIds = new Set(scenes.map(scene => scene.id))
  if (
    cursor.classroomVersionId !== document.classroomVersionId ||
    cursor.documentFingerprint !== document.fileSha256 ||
    !Number.isSafeInteger(cursor.revision) ||
    cursor.revision < 0 ||
    !Number.isSafeInteger(cursor.sceneIndex) ||
    !Number.isSafeInteger(cursor.actionIndex) ||
    cursor.sceneIndex < 0 ||
    cursor.actionIndex < 0 ||
    !Array.isArray(cursor.consumed) ||
    cursor.consumed.some(value => typeof value !== 'string' || value.length === 0) ||
    new Set(cursor.consumed).size !== cursor.consumed.length ||
    (cursor.enteredSceneId !== null && typeof cursor.enteredSceneId !== 'string') ||
    (cursor.pendingAction !== null &&
      (typeof cursor.pendingAction !== 'object' ||
        typeof cursor.pendingAction.sceneId !== 'string' ||
        typeof cursor.pendingAction.actionId !== 'string' ||
        !Number.isSafeInteger(cursor.pendingAction.actionIndex) ||
        !Number.isSafeInteger(cursor.pendingAction.preparedAtRevision) ||
        typeof cursor.pendingAction.executionId !== 'string' ||
        !cursor.pendingAction.executionId))
  ) {
    throw new Error('playback cursor version, fingerprint, or shape is invalid')
  }
  cursor.consumed.forEach(value => {
    const [sceneId] = readPlaybackInteractionKey(value)
    if (!sceneIds.has(sceneId)) throw new Error('playback cursor is invalid')
  })
  if (cursor.sceneIndex === scenes.length) {
    if (
      cursor.actionIndex !== 0 ||
      cursor.enteredSceneId !== null ||
      cursor.pendingAction !== null
    ) {
      throw new Error('playback cursor is invalid')
    }
    return copyCursor(cursor)
  }
  const scene = scenes[cursor.sceneIndex]
  if (!scene || cursor.actionIndex > scene.actions.length) {
    throw new Error('playback cursor is outside the classroom')
  }
  if (cursor.enteredSceneId !== null && cursor.enteredSceneId !== scene.id) {
    throw new Error('playback cursor scene binding is invalid')
  }
  if (cursor.pendingAction) {
    const actionInput = scene.actions[cursor.actionIndex]
    const action = actionInput ? readPlaybackAction(actionInput) : null
    const expectedExecutionId =
      action && cursor.pendingAction.preparedAtRevision >= 1
        ? playbackStableId(
            'execution',
            playbackStableId(
              'checkpoint',
              document.classroomVersionId,
              document.fileSha256,
              cursor.pendingAction.preparedAtRevision - 1,
              `prepare-action:${scene.id}:${action.id}`
            ),
            scene.id,
            action.id
          )
        : ''
    if (
      !action ||
      cursor.pendingAction.preparedAtRevision < 1 ||
      cursor.pendingAction.preparedAtRevision > cursor.revision ||
      cursor.enteredSceneId !== scene.id ||
      cursor.pendingAction.sceneId !== scene.id ||
      cursor.pendingAction.actionIndex !== cursor.actionIndex ||
      cursor.pendingAction.actionId !== action.id ||
      cursor.pendingAction.executionId !== expectedExecutionId
    ) {
      throw new Error('playback cursor action binding is invalid')
    }
  }
  return copyCursor(cursor)
}

function isAbort(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

export function createPlaybackController(
  document: ClassroomDocument,
  ports: PlaybackPorts,
  options: PlaybackControllerOptions = {}
): PlaybackController {
  const scenes = [...document.openmaic.scenes].sort((left, right) => left.order - right.order)
  if (scenes.length === 0 || new Set(scenes.map(scene => scene.id)).size !== scenes.length) {
    throw new Error('classroom playback requires unique scenes')
  }
  const actionIds = new Set<string>()
  scenes.forEach(scene =>
    scene.actions.forEach(input => {
      const action = readPlaybackAction(input)
      if (actionIds.has(action.id)) {
        throw new Error('classroom playback requires unique actions')
      }
      actionIds.add(action.id)
    })
  )

  let cursor = initialCursor(document)
  let playbackState: PlaybackState = 'idle'
  let activeAbort: AbortController | null = null
  let activeRun: Promise<void> | null = null
  let runGeneration = 0
  let mutationTail: Promise<void> = Promise.resolve()
  let transitionTail: Promise<void> = Promise.resolve()
  let pendingMutations = 0
  let pendingTransitions = 0
  let disposed = false
  let unresolvedCheckpoint: PlaybackCheckpoint | null = null
  const completedExecutions = new Set<string>()
  const eventTimes = new Map<string, string>()
  const now = options.now ?? (() => new Date())

  const occurredAt = (eventId: string): string => {
    const existing = eventTimes.get(eventId)
    if (existing) return existing
    const value = now().toISOString()
    eventTimes.set(eventId, value)
    return value
  }

  const ensureActive = () => {
    if (disposed) throw new Error('playback controller is disposed')
  }

  const enqueueTransition = <T>(operation: () => Promise<T> | T): Promise<T> => {
    pendingTransitions += 1
    const result = transitionTail.then(operation)
    transitionTail = result.then(
      () => undefined,
      () => undefined
    )
    return result.finally(() => {
      pendingTransitions -= 1
    })
  }

  const commitCursor = (
    operationId: string,
    mutation: (current: PlaybackCursor, checkpointId: string) => CursorMutation | null
  ): Promise<boolean> => {
    pendingMutations += 1
    const operation = mutationTail.then(async () => {
      ensureActive()
      let replayedCheckpoint = false
      if (unresolvedCheckpoint) {
        const pending = unresolvedCheckpoint
        await ports.commitCheckpoint(pending)
        cursor = validateCursor(pending.cursor, document, scenes)
        unresolvedCheckpoint = null
        replayedCheckpoint = true
      }
      const current = copyCursor(cursor)
      const checkpointId = playbackStableId(
        'checkpoint',
        document.classroomVersionId,
        document.fileSha256,
        current.revision,
        operationId
      )
      const planned = mutation(current, checkpointId)
      if (!planned) return replayedCheckpoint
      const next = validateCursor(
        { ...planned.cursor, revision: current.revision + 1 },
        document,
        scenes
      )
      const events = [...(planned.events ?? [])]
      if (
        events.some(
          event => !event.eventId || event.classroomVersionId !== document.classroomVersionId
        ) ||
        new Set(events.map(event => event.eventId)).size !== events.length
      ) {
        throw new Error('playback checkpoint events are invalid')
      }
      const checkpoint: PlaybackCheckpoint = {
        checkpointId,
        expectedRevision: current.revision,
        cursor: copyCursor(next),
        events,
      }
      unresolvedCheckpoint = checkpoint
      await ports.commitCheckpoint(checkpoint)
      cursor = next
      unresolvedCheckpoint = null
      return true
    })
    mutationTail = operation.then(
      () => undefined,
      () => undefined
    )
    return operation.finally(() => {
      pendingMutations -= 1
    })
  }

  const eventFor = (
    checkpointId: string,
    suffix: string,
    type: Parameters<typeof learningEvent>[0],
    fields: Omit<
      ClassroomLearningEvent,
      'eventId' | 'type' | 'classroomVersionId' | 'occurredAt'
    > = {}
  ): ClassroomLearningEvent => {
    const eventId = playbackStableId('event', checkpointId, suffix)
    return learningEvent(type, document.classroomVersionId, occurredAt(eventId), {
      eventId,
      ...fields,
    })
  }

  const executionContext = (pending: PlaybackPendingAction): PlaybackActionExecution => ({
    executionId: pending.executionId,
    classroomVersionId: document.classroomVersionId,
    documentFingerprint: document.fileSha256,
    sceneId: pending.sceneId,
    actionId: pending.actionId,
  })

  const executeAction = async (
    actionInput: unknown,
    pending: PlaybackPendingAction,
    signal: AbortSignal
  ): Promise<void> => {
    const action = readPlaybackAction(actionInput)
    const execution = executionContext(pending)
    switch (action.type) {
      case 'speech':
        await ports.speak(action, signal, execution)
        return
      case 'play_video':
        await ports.playVideo(action, signal, execution)
        return
      case 'spotlight':
      case 'laser':
        await ports.applyEffect(action, execution)
        return
      case 'discussion':
        await ports.openDiscussion(action, execution)
        return
      case 'widget_highlight':
      case 'widget_setState':
      case 'widget_annotation':
      case 'widget_reveal':
        await ports.postWidgetAction(action, execution)
        return
      default:
        await ports.applyWhiteboard(action, execution)
    }
  }

  const finalizeAction = async (
    scene: ClassroomScene,
    actionInput: unknown,
    pending: PlaybackPendingAction
  ): Promise<boolean> => {
    const action = readPlaybackAction(actionInput)
    return commitCursor(`complete-action:${pending.executionId}`, (current, checkpointId) => {
      if (current.pendingAction?.executionId !== pending.executionId) return null
      return {
        cursor: {
          ...current,
          actionIndex: current.actionIndex + 1,
          pendingAction: null,
        },
        events: [
          eventFor(checkpointId, 'action.completed', 'action.completed', {
            sceneId: scene.id,
            actionId: action.id,
          }),
        ],
      }
    })
  }

  const run = async (generation: number): Promise<void> => {
    while (playbackState === 'running' && cursor.sceneIndex < scenes.length) {
      const sceneIndex = cursor.sceneIndex
      const scene = scenes[sceneIndex]
      if (!scene) throw new Error('playback cursor is outside the classroom')
      ports.renderScene(scene.id)

      if (cursor.enteredSceneId !== scene.id) {
        await commitCursor(`enter-scene:${scene.id}`, (current, checkpointId) => {
          if (current.sceneIndex !== sceneIndex || current.enteredSceneId === scene.id) return null
          return {
            cursor: { ...current, enteredSceneId: scene.id },
            events: [
              eventFor(checkpointId, 'scene.entered', 'scene.entered', { sceneId: scene.id }),
            ],
          }
        })
      }
      if (playbackState !== 'running' || generation !== runGeneration) return

      while (
        playbackState === 'running' &&
        generation === runGeneration &&
        cursor.sceneIndex === sceneIndex &&
        cursor.actionIndex < scene.actions.length
      ) {
        const actionIndex = cursor.actionIndex
        const action = readPlaybackAction(scene.actions[actionIndex])
        if (!cursor.pendingAction) {
          await commitCursor(`prepare-action:${scene.id}:${action.id}`, (current, checkpointId) => {
            if (
              current.sceneIndex !== sceneIndex ||
              current.actionIndex !== actionIndex ||
              current.pendingAction
            ) {
              return null
            }
            return {
              cursor: {
                ...current,
                pendingAction: {
                  sceneId: scene.id,
                  actionId: action.id,
                  actionIndex,
                  preparedAtRevision: current.revision + 1,
                  executionId: playbackStableId('execution', checkpointId, scene.id, action.id),
                },
              },
            }
          })
        }
        const pending = cursor.pendingAction
        if (!pending || pending.actionId !== action.id) {
          throw new Error('playback action intent is invalid')
        }

        const abort = new AbortController()
        const abortable = action.type === 'speech' || action.type === 'play_video'
        if (abortable) activeAbort = abort
        try {
          if (!completedExecutions.has(pending.executionId)) {
            await executeAction(action, pending, abort.signal)
            completedExecutions.add(pending.executionId)
          }
        } catch (error) {
          if (abortable && abort.signal.aborted && isAbort(error)) return
          throw error
        } finally {
          if (activeAbort === abort) activeAbort = null
        }
        if (abortable && abort.signal.aborted) return

        const advanced = await finalizeAction(scene, action, pending)
        if (!advanced) return
        if (playbackState !== 'running' || generation !== runGeneration) return
      }
      if (playbackState !== 'running' || generation !== runGeneration) return
      const advanced = await commitCursor(
        `complete-scene:${scene.id}`,
        (current, checkpointId) => {
          if (
            current.sceneIndex !== sceneIndex ||
            current.actionIndex !== scene.actions.length ||
            current.pendingAction
          ) {
            return null
          }
          const terminal = sceneIndex + 1 === scenes.length
          const events: ClassroomLearningEvent[] = [
            eventFor(checkpointId, 'scene.completed', 'scene.completed', { sceneId: scene.id }),
          ]
          if (terminal) {
            events.push(eventFor(checkpointId, 'classroom.completed', 'classroom.completed'))
          }
          return {
            cursor: {
              ...current,
              sceneIndex: sceneIndex + 1,
              actionIndex: 0,
              enteredSceneId: null,
            },
            events,
          }
        }
      )
      if (!advanced) return
    }
    if (playbackState === 'running' && generation === runGeneration) {
      playbackState = 'completed'
    }
  }

  const launchRun = (): Promise<void> => {
    playbackState = 'running'
    const generation = ++runGeneration
    const launched = run(generation)
      .catch(error => {
        if (generation === runGeneration && playbackState === 'running') {
          playbackState = 'stopped'
        }
        throw error
      })
      .finally(() => {
        if (activeRun === launched) activeRun = null
      })
    activeRun = launched
    return launched
  }

  const startTransition = async (resumeOnly: boolean): Promise<void> => {
    const result = await enqueueTransition(async () => {
      ensureActive()
      if (playbackState === 'completed' || (resumeOnly && playbackState !== 'paused')) {
        return { run: Promise.resolve() }
      }
      if (activeRun) {
        if (playbackState === 'running') return { run: activeRun }
        await activeRun
      }
      return { run: launchRun() }
    })
    await result.run
  }

  const controller: PlaybackController = {
    get state() {
      return playbackState
    },
    snapshot: () => copyCursor(cursor),
    restore(restored) {
      ensureActive()
      if (
        playbackState === 'running' ||
        playbackState === 'switching' ||
        pendingMutations > 0 ||
        pendingTransitions > 0 ||
        unresolvedCheckpoint !== null
      ) {
        throw new Error('cannot restore an active playback controller')
      }
      cursor = validateCursor(restored, document, scenes)
      playbackState = cursor.sceneIndex === scenes.length ? 'completed' : 'idle'
    },
    start: () => startTransition(false),
    async pause() {
      await enqueueTransition(async () => {
        ensureActive()
        if (playbackState !== 'running') return
        playbackState = 'paused'
        runGeneration += 1
        activeAbort?.abort()
      })
    },
    resume: () => startTransition(true),
    async stop() {
      await enqueueTransition(async () => {
        ensureActive()
        if (playbackState === 'completed' || playbackState === 'stopped') return
        playbackState = 'stopped'
        runGeneration += 1
        activeAbort?.abort()
      })
    },
    async switchScene(sceneIndex) {
      await enqueueTransition(async () => {
        ensureActive()
        if (!Number.isSafeInteger(sceneIndex) || sceneIndex < 0 || sceneIndex >= scenes.length) {
          throw new Error('scene index is invalid')
        }
        playbackState = 'switching'
        try {
          runGeneration += 1
          activeAbort?.abort()
          const interrupted = activeRun
          if (interrupted) await interrupted

          if (cursor.pendingAction) {
            if (!completedExecutions.has(cursor.pendingAction.executionId)) {
              throw new Error('retry the pending action before switching scenes')
            }
            const currentScene = scenes[cursor.sceneIndex]
            const currentAction = currentScene?.actions[cursor.actionIndex]
            if (!currentScene || !currentAction) {
              throw new Error('playback pending action is invalid')
            }
            await finalizeAction(currentScene, currentAction, cursor.pendingAction)
          }

          await commitCursor(`switch-scene:${sceneIndex}`, current => ({
            cursor: {
              ...current,
              sceneIndex,
              actionIndex: 0,
              enteredSceneId: null,
              pendingAction: null,
            },
          }))
          ports.renderScene(scenes[sceneIndex]!.id)
          playbackState = 'idle'
        } catch (error) {
          playbackState = 'stopped'
          throw error
        }
      })
    },
    async commitEvents(operationId, events) {
      await enqueueTransition(async () => {
        ensureActive()
        if (!operationId || events.length === 0) return
        await commitCursor(`events:${operationId}`, current => ({ cursor: current, events }))
      })
    },
    async markConsumed(sceneId, interactionId, events = []) {
      await enqueueTransition(async () => {
        ensureActive()
        if (!scenes.some(scene => scene.id === sceneId)) {
          throw new Error('interaction scene is invalid')
        }
        const key = playbackInteractionKey(sceneId, interactionId)
        await commitCursor(`consume:${key}`, current => {
          if (current.consumed.includes(key) && events.length === 0) return null
          return {
            cursor: current.consumed.includes(key)
              ? current
              : { ...current, consumed: [...current.consumed, key] },
            events,
          }
        })
      })
    },
    async drain() {
      await transitionTail
      await mutationTail
      const running = activeRun
      if (running) await running
    },
    async dispose() {
      await enqueueTransition(async () => {
        if (disposed) return
        disposed = true
        if (playbackState !== 'completed') playbackState = 'stopped'
        runGeneration += 1
        activeAbort?.abort()
        const interrupted = activeRun
        if (interrupted) await interrupted.catch(() => undefined)
        await mutationTail
      })
    },
  }
  return controller
}
