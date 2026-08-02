import assert from 'node:assert/strict'
import test from 'node:test'

import type { ClassroomDocument, ClassroomScene } from '../lib/openmaic-adapter/contracts'
import {
  createPlaybackController,
  type PlaybackPorts,
} from '../lib/openmaic-adapter/playback/controller'
import {
  applyWhiteboardAction,
  createWhiteboardState,
  reconstructWhiteboardState,
} from '../lib/openmaic-adapter/playback/action-reducer'
import { learningEvent } from '../lib/openmaic-adapter/playback/events'
import { createQuizSubmissionCoordinator } from '../lib/openmaic-adapter/playback/quiz-submission'
import {
  playbackInteractionKey,
  type PlaybackCursor,
} from '../lib/openmaic-adapter/playback/types'

const NOW = '2026-07-30T00:00:00.000Z'

function documentFixture(): ClassroomDocument {
  return {
    schemaVersion: '1.0',
    classroomId: 'classroom-1',
    classroomVersionId: 'version-1',
    contentMode: 'open_creation',
    openCreation: true,
    openmaic: {
      dslVersion: '0.1.0',
      stage: { id: 'stage-1', name: 'Stage', createdAt: NOW, updatedAt: NOW },
      scenes: [
        {
          id: 'scene-1',
          stageId: 'stage-1',
          title: 'Slide',
          order: 0,
          type: 'slide',
          content: { type: 'slide', canvas: {} },
          actions: [{ id: 'speech-1', type: 'speech', text: 'Welcome' }],
        },
        {
          id: 'scene-2',
          stageId: 'stage-1',
          title: 'Quiz',
          order: 1,
          type: 'quiz',
          content: {
            type: 'quiz',
            questions: [
              {
                id: 'quiz-1',
                prompt: 'Ready?',
                questionType: 'single_choice',
                options: [
                  { id: 'yes', label: 'Yes' },
                  { id: 'no', label: 'No' },
                ],
                correctOptionIds: ['yes'],
                explanation: 'Ready.',
              },
            ],
          },
          actions: [
            { id: 'scene-2-action-1', type: 'speech', text: 'One' },
            { id: 'scene-2-action-2', type: 'speech', text: 'Two' },
            { id: 'scene-2-action-3', type: 'speech', text: 'Three' },
          ],
        },
      ],
    },
    interactionIds: ['scene-2'],
    sourceRefs: [],
    knowledgePointMappings: [{ knowledgePointId: 'kp-1', sceneIds: ['scene-1'], sourceRefs: [] }],
    mediaManifest: [],
    fileSha256: 'a'.repeat(64),
    exportManifest: [],
    generationMetadata: {
      generator: 'test',
      generatorVersion: '1',
      modelId: 'model',
      generatedAt: NOW,
      teachingBriefId: 'brief',
      teachingBriefSha256: 'b'.repeat(64),
      templateId: 'template',
      templateVersion: '1',
    },
    auditMetadata: {
      templateId: 'template',
      templateVersion: '1',
      teachingBriefId: 'brief',
      teachingBriefSha256: 'b'.repeat(64),
      parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: NOW },
    migrationRecords: [],
  }
}

function fakePlaybackPorts() {
  const executed: string[] = []
  const rendered: string[] = []
  const events: Array<{ type: string }> = []
  const cursors: unknown[] = []
  const committedCheckpoints = new Set<string>()
  const ports: PlaybackPorts = {
    renderScene: sceneId => rendered.push(sceneId),
    speak: async action => {
      executed.push(action.id)
    },
    playVideo: async action => {
      executed.push(action.id)
    },
    applyWhiteboard: async action => {
      executed.push(action.id)
    },
    applyEffect: action => {
      executed.push(action.id)
    },
    openDiscussion: async action => {
      executed.push(action.id)
    },
    postWidgetAction: async action => {
      executed.push(action.id)
    },
    commitCheckpoint: async checkpoint => {
      if (committedCheckpoints.has(checkpoint.checkpointId)) return
      committedCheckpoints.add(checkpoint.checkpointId)
      cursors.push(checkpoint.cursor)
      events.push(...checkpoint.events)
    },
  }
  return { ports, executed, rendered, events, cursors, committedCheckpoints }
}

function cursorFixture(overrides: Partial<PlaybackCursor> = {}): PlaybackCursor {
  return {
    classroomVersionId: 'version-1',
    documentFingerprint: 'a'.repeat(64),
    revision: 0,
    sceneIndex: 0,
    actionIndex: 0,
    consumed: [],
    enteredSceneId: null,
    pendingAction: null,
    ...overrides,
  }
}

function documentWithFirstSceneActions(actions: ClassroomScene['actions']): ClassroomDocument {
  const document = documentFixture()
  const first = document.openmaic.scenes[0]!
  return {
    ...document,
    openmaic: {
      ...document.openmaic,
      scenes: [{ ...first, actions }, ...document.openmaic.scenes.slice(1)],
    },
  }
}

test('playback resumes from the persisted cursor without replaying quiz grading', async () => {
  const fake = fakePlaybackPorts()
  const controller = createPlaybackController(documentFixture(), fake.ports)
  controller.restore(cursorFixture({
    sceneIndex: 1,
    actionIndex: 2,
    consumed: [playbackInteractionKey('scene-2', 'quiz-1')],
  }))

  await controller.start()

  assert.equal(fake.executed[0], 'scene-2-action-3')
  assert.equal(fake.events.filter(event => event.type === 'quiz.graded').length, 0)
  assert.equal(
    controller.snapshot().consumed.includes(playbackInteractionKey('scene-2', 'quiz-1')),
    true
  )
})

test('completion is emitted once and a completed controller is idempotent', async () => {
  const fake = fakePlaybackPorts()
  const controller = createPlaybackController(documentFixture(), fake.ports)

  await controller.start()
  await controller.start()

  assert.equal(fake.events.filter(event => event.type === 'classroom.completed').length, 1)
  assert.equal(controller.state, 'completed')
})

test('playback resumes an interrupted action instead of reusing the paused run', async () => {
  const fake = fakePlaybackPorts()
  let firstAttempt = true
  fake.ports.speak = async (action, signal) => {
    fake.executed.push(action.id)
    if (!firstAttempt) return
    firstAttempt = false
    await new Promise<void>((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('paused', 'AbortError')), {
        once: true,
      })
    })
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)

  const firstRun = controller.start()
  await new Promise(resolve => setImmediate(resolve))
  await controller.pause()
  await controller.resume()
  await firstRun

  assert.deepEqual(fake.executed.slice(0, 2), ['speech-1', 'speech-1'])
  assert.equal(controller.state, 'completed')
})

test('playback can restart immediately after stop without losing the cursor', async () => {
  const fake = fakePlaybackPorts()
  let firstAttempt = true
  fake.ports.speak = async (action, signal) => {
    fake.executed.push(action.id)
    if (!firstAttempt) return
    firstAttempt = false
    await new Promise<void>((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('stopped', 'AbortError')), {
        once: true,
      })
    })
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)

  const interrupted = controller.start()
  await new Promise(resolve => setImmediate(resolve))
  await controller.stop()
  await controller.start()
  await interrupted

  assert.deepEqual(fake.executed.slice(0, 2), ['speech-1', 'speech-1'])
  assert.equal(controller.state, 'completed')
})

test('invalid persisted cursors fail closed before any action runs', () => {
  const fake = fakePlaybackPorts()
  const controller = createPlaybackController(documentFixture(), fake.ports)

  assert.throws(
    () => controller.restore(cursorFixture({ sceneIndex: 99 })),
    /cursor/i
  )
  assert.deepEqual(fake.executed, [])
})

test('whiteboard actions update immutable slide state in order', () => {
  const initial = createWhiteboardState('whiteboard-1')
  const opened = applyWhiteboardAction(initial, { id: 'open', type: 'wb_open' })
  const drawn = applyWhiteboardAction(opened, {
    id: 'draw',
    type: 'wb_draw_text',
    elementId: 'equation',
    content: 'x = 2',
    x: 10,
    y: 20,
  })
  const cleared = applyWhiteboardAction(drawn, { id: 'clear', type: 'wb_clear' })

  assert.equal(initial.open, false)
  assert.equal(initial.slide.viewportRatio, 9 / 16)
  assert.equal(opened.open, true)
  assert.equal(drawn.slide.elements.length, 1)
  assert.equal(drawn.slide.elements[0]?.id, 'equation')
  assert.equal(cleared.slide.elements.length, 0)
  assert.equal(drawn.slide.elements.length, 1)
})

test('cursor persistence failure leaves the in-memory cursor retryable', async () => {
  const fake = fakePlaybackPorts()
  let shouldFail = true
  fake.ports.commitCheckpoint = async checkpoint => {
    if (shouldFail) {
      shouldFail = false
      throw new Error('storage unavailable')
    }
    fake.cursors.push(checkpoint.cursor)
    fake.events.push(...checkpoint.events)
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)

  await assert.rejects(
    controller.markConsumed('scene-1', 'shared-interaction'),
    /storage unavailable/
  )
  assert.deepEqual(controller.snapshot().consumed, [])

  await controller.markConsumed('scene-1', 'shared-interaction')
  assert.deepEqual(controller.snapshot().consumed, [
    playbackInteractionKey('scene-1', 'shared-interaction'),
  ])
})

test('concurrent cursor mutations are serialized without losing progress', async () => {
  const fake = fakePlaybackPorts()
  let activeWrites = 0
  let maximumActiveWrites = 0
  fake.ports.commitCheckpoint = async checkpoint => {
    activeWrites += 1
    maximumActiveWrites = Math.max(maximumActiveWrites, activeWrites)
    await new Promise(resolve => setImmediate(resolve))
    fake.cursors.push(checkpoint.cursor)
    fake.events.push(...checkpoint.events)
    activeWrites -= 1
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)

  await Promise.all([
    controller.markConsumed('scene-1', 'shared-interaction'),
    controller.markConsumed('scene-2', 'shared-interaction'),
  ])

  assert.equal(maximumActiveWrites, 1)
  assert.deepEqual(controller.snapshot().consumed, [
    playbackInteractionKey('scene-1', 'shared-interaction'),
    playbackInteractionKey('scene-2', 'shared-interaction'),
  ])
})

test('interaction checkpoints can commit while an action intent is pending', async () => {
  const fake = fakePlaybackPorts()
  let releaseDiscussion!: () => void
  let discussionStarted!: () => void
  const started = new Promise<void>(resolve => {
    discussionStarted = resolve
  })
  fake.ports.openDiscussion = async () => {
    discussionStarted()
    await new Promise<void>(resolve => {
      releaseDiscussion = resolve
    })
  }
  const controller = createPlaybackController(
    documentWithFirstSceneActions([{ id: 'discussion-pending', type: 'discussion', topic: 'Why?' }]),
    fake.ports
  )

  const running = controller.start()
  await started
  await controller.markConsumed('scene-1', 'parallel-interaction')
  releaseDiscussion()
  await running

  assert.equal(controller.state, 'completed')
  assert.equal(
    controller.snapshot().consumed.includes(
      playbackInteractionKey('scene-1', 'parallel-interaction')
    ),
    true
  )
})

test('interaction consumption is scoped by scene', async () => {
  const fake = fakePlaybackPorts()
  const controller = createPlaybackController(documentFixture(), fake.ports)

  await controller.markConsumed('scene-1', 'duplicate-id')
  await controller.markConsumed('scene-2', 'duplicate-id')

  assert.equal(new Set(controller.snapshot().consumed).size, 2)
  assert.notEqual(
    playbackInteractionKey('scene-1', 'duplicate-id'),
    playbackInteractionKey('scene-2', 'duplicate-id')
  )
})

test('a failed completion event remains retryable', async () => {
  const fake = fakePlaybackPorts()
  let completionAttempts = 0
  fake.ports.commitCheckpoint = async checkpoint => {
    if (checkpoint.events.some(event => event.type === 'classroom.completed')) {
      completionAttempts += 1
      if (completionAttempts === 1) throw new Error('event sink unavailable')
    }
    fake.cursors.push(checkpoint.cursor)
    fake.events.push(...checkpoint.events)
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)

  await assert.rejects(controller.start(), /event sink unavailable/)
  assert.equal(controller.state, 'stopped')
  assert.equal(controller.snapshot().sceneIndex, 1)

  await controller.start()
  assert.equal(completionAttempts, 2)
  assert.equal(fake.events.filter(event => event.type === 'classroom.completed').length, 1)
  assert.equal(controller.state, 'completed')
})

test('pausing an atomic discussion does not replay it on resume', async () => {
  const fake = fakePlaybackPorts()
  let release!: () => void
  let started!: () => void
  const actionStarted = new Promise<void>(resolve => {
    started = resolve
  })
  fake.ports.openDiscussion = async action => {
    fake.executed.push(action.id)
    started()
    await new Promise<void>(resolve => {
      release = resolve
    })
  }
  const controller = createPlaybackController(
    documentWithFirstSceneActions([{ id: 'discussion-1', type: 'discussion', topic: 'Why?' }]),
    fake.ports
  )

  const firstRun = controller.start()
  await actionStarted
  await controller.pause()
  release()
  await firstRun

  assert.equal(controller.state, 'paused')
  assert.equal(controller.snapshot().actionIndex, 1)
  await controller.resume()
  assert.equal(fake.executed.filter(id => id === 'discussion-1').length, 1)
  assert.equal(controller.state, 'completed')
})

test('non-abort action failures stop playback at the retryable cursor', async () => {
  const fake = fakePlaybackPorts()
  fake.ports.openDiscussion = async () => {
    throw new Error('discussion failed')
  }
  const controller = createPlaybackController(
    documentWithFirstSceneActions([{ id: 'discussion-1', type: 'discussion', topic: 'Why?' }]),
    fake.ports
  )

  await assert.rejects(controller.start(), /discussion failed/)
  assert.equal(controller.state, 'stopped')
  assert.equal(controller.snapshot().actionIndex, 0)
})

test('whiteboard reducer emits renderer-native structures and stable code lines', () => {
  let state = createWhiteboardState('whiteboard-structures')
  state = applyWhiteboardAction(state, {
    id: 'text',
    type: 'wb_draw_text',
    content: '<img onerror=alert(1)>',
    x: 10,
    y: 20,
  })
  state = applyWhiteboardAction(state, {
    id: 'shape',
    type: 'wb_draw_shape',
    shape: 'triangle',
    x: 10,
    y: 20,
    width: 100,
    height: 80,
  })
  state = applyWhiteboardAction(state, {
    id: 'line',
    type: 'wb_draw_line',
    startX: 30,
    startY: 40,
    endX: 10,
    endY: 20,
    width: 3,
  })
  state = applyWhiteboardAction(state, {
    id: 'table',
    type: 'wb_draw_table',
    x: 0,
    y: 0,
    width: 400,
    height: 120,
    data: [
      ['A', '<B>'],
      ['C', 'D'],
    ],
  })
  state = applyWhiteboardAction(state, {
    id: 'code',
    elementId: 'code-block',
    type: 'wb_draw_code',
    language: 'ts',
    code: 'const one = 1;\nconst two = 2;',
    x: 0,
    y: 0,
  })
  state = applyWhiteboardAction(state, {
    id: 'edit-code',
    elementId: 'code-block',
    type: 'wb_edit_code',
    operation: 'replace_lines',
    lineId: 'L1',
    content: 'const one = 3;',
  })

  const text = state.slide.elements.find(element => element.id === 'text')
  const shape = state.slide.elements.find(element => element.id === 'shape')
  const line = state.slide.elements.find(element => element.id === 'line')
  const table = state.slide.elements.find(element => element.id === 'table')
  const code = state.slide.elements.find(element => element.id === 'code-block')
  assert.equal(text?.type, 'text')
  if (text?.type === 'text') assert.match(text.content, /&lt;img/)
  assert.equal(shape?.type, 'shape')
  if (shape?.type === 'shape') assert.match(shape.path, /^M100 0/)
  assert.equal(line?.type, 'line')
  if (line?.type === 'line') {
    assert.deepEqual(line.start, [20, 20])
    assert.deepEqual(line.end, [0, 0])
    assert.equal(line.width, 3)
  }
  assert.equal(table?.type, 'table')
  if (table?.type === 'table') {
    assert.deepEqual(table.colWidths, [0.5, 0.5])
    assert.equal(table.data[0]?.[1]?.text, '&lt;B&gt;')
  }
  assert.equal(code?.type, 'code')
  if (code?.type === 'code') {
    assert.equal(code.lines[0]?.id, 'edit-code-L1')
    assert.equal(code.lines[1]?.id, 'L2')
  }
})

test('whiteboard state is reconstructed exactly to the durable action cursor', () => {
  const document = documentWithFirstSceneActions([
    { id: 'open', type: 'wb_open' },
    {
      id: 'draw',
      type: 'wb_draw_text',
      elementId: 'durable-text',
      content: 'durable',
      x: 10,
      y: 10,
    },
    { id: 'clear', type: 'wb_clear' },
  ])

  const beforeClear = reconstructWhiteboardState(document, {
    sceneIndex: 0,
    actionIndex: 2,
  })
  const afterClear = reconstructWhiteboardState(document, {
    sceneIndex: 0,
    actionIndex: 3,
  })

  assert.equal(beforeClear.open, true)
  assert.equal(beforeClear.slide.elements[0]?.id, 'durable-text')
  assert.equal(afterClear.slide.elements.length, 0)
})

test('a completed non-abort action is not repeated when its cursor commit is retried', async () => {
  const fake = fakePlaybackPorts()
  let finalCommitAttempts = 0
  fake.ports.commitCheckpoint = async checkpoint => {
    if (
      checkpoint.events.some(
        event => event.type === 'action.completed' && event.actionId === 'discussion-once'
      )
    ) {
      finalCommitAttempts += 1
      if (finalCommitAttempts === 1) {
        fake.committedCheckpoints.add(checkpoint.checkpointId)
        fake.cursors.push(checkpoint.cursor)
        fake.events.push(...checkpoint.events)
        throw new Error('ambiguous checkpoint failure')
      }
    }
    if (fake.committedCheckpoints.has(checkpoint.checkpointId)) return
    fake.committedCheckpoints.add(checkpoint.checkpointId)
    fake.cursors.push(checkpoint.cursor)
    fake.events.push(...checkpoint.events)
  }
  const controller = createPlaybackController(
    documentWithFirstSceneActions([{ id: 'discussion-once', type: 'discussion', topic: 'Why?' }]),
    fake.ports
  )

  await assert.rejects(controller.start(), /ambiguous checkpoint failure/)
  await controller.start()

  assert.equal(fake.executed.filter(id => id === 'discussion-once').length, 1)
  assert.equal(controller.state, 'completed')
})

test('completion events carry stable idempotency keys across an ambiguous retry', async () => {
  const fake = fakePlaybackPorts()
  const attemptedIds: Array<string | undefined> = []
  let shouldFail = true
  fake.ports.commitCheckpoint = async checkpoint => {
    const completed = checkpoint.events.find(
      event => event.type === 'action.completed' && event.actionId === 'speech-1'
    )
    if (completed) {
      attemptedIds.push(completed.eventId)
      if (shouldFail) {
        shouldFail = false
        throw new Error('ambiguous event commit')
      }
    }
    if (fake.committedCheckpoints.has(checkpoint.checkpointId)) return
    fake.committedCheckpoints.add(checkpoint.checkpointId)
    fake.cursors.push(checkpoint.cursor)
    fake.events.push(...checkpoint.events)
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)

  await assert.rejects(controller.start(), /ambiguous event commit/)
  await controller.start()

  assert.equal(attemptedIds.length, 2)
  assert.ok(attemptedIds[0])
  assert.equal(attemptedIds[0], attemptedIds[1])
  assert.equal(
    fake.events.filter(event => event.type === 'action.completed').length,
    documentFixture().openmaic.scenes.reduce((total, scene) => total + scene.actions.length, 0)
  )
})

test('a recovered pending action reuses its host side-effect idempotency key', async () => {
  const document = documentWithFirstSceneActions([
    { id: 'discussion-recovered', type: 'discussion', topic: 'Why?' },
  ])
  const fake = fakePlaybackPorts()
  const executedIds = new Set<string>()
  let durableCursor = cursorFixture()
  let failFinalCheckpoint = true
  fake.ports.openDiscussion = async (_action, execution) => {
    if (executedIds.has(execution.executionId)) return
    executedIds.add(execution.executionId)
    fake.executed.push('discussion-recovered')
  }
  fake.ports.commitCheckpoint = async checkpoint => {
    if (
      failFinalCheckpoint &&
      checkpoint.events.some(event => event.type === 'action.completed')
    ) {
      failFinalCheckpoint = false
      throw new Error('process lost final checkpoint response')
    }
    durableCursor = checkpoint.cursor
    fake.events.push(...checkpoint.events)
  }

  const firstController = createPlaybackController(document, fake.ports)
  await assert.rejects(firstController.start(), /lost final checkpoint response/)
  assert.equal(durableCursor.pendingAction?.actionId, 'discussion-recovered')

  const tamperedController = createPlaybackController(document, fake.ports)
  assert.throws(
    () =>
      tamperedController.restore({
        ...durableCursor,
        pendingAction: durableCursor.pendingAction
          ? { ...durableCursor.pendingAction, executionId: 'attacker-selected-id' }
          : null,
      }),
    /action binding/i
  )

  const recoveredController = createPlaybackController(document, fake.ports)
  recoveredController.restore(durableCursor)
  await recoveredController.start()

  assert.equal(fake.executed.filter(id => id === 'discussion-recovered').length, 1)
  assert.equal(executedIds.size, 1)
  assert.equal(recoveredController.state, 'completed')
})

test('scene completion is not duplicated when cursor persistence is retried', async () => {
  const fake = fakePlaybackPorts()
  let sceneAdvanceAttempts = 0
  fake.ports.commitCheckpoint = async checkpoint => {
    if (
      checkpoint.events.some(
        event => event.type === 'scene.completed' && event.sceneId === 'scene-1'
      )
    ) {
      sceneAdvanceAttempts += 1
      if (sceneAdvanceAttempts === 1) throw new Error('scene checkpoint unavailable')
    }
    if (fake.committedCheckpoints.has(checkpoint.checkpointId)) return
    fake.committedCheckpoints.add(checkpoint.checkpointId)
    fake.cursors.push(checkpoint.cursor)
    fake.events.push(...checkpoint.events)
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)

  await assert.rejects(controller.start(), /scene checkpoint unavailable/)
  await controller.start()

  assert.equal(
    fake.events.filter(event => event.type === 'scene.completed').length,
    documentFixture().openmaic.scenes.length
  )
})

test('scene switching serializes an immediately requested restart', async () => {
  const fake = fakePlaybackPorts()
  let releaseFirstSpeech!: () => void
  let firstSpeechStarted!: () => void
  const firstSpeech = new Promise<void>(resolve => {
    firstSpeechStarted = resolve
  })
  let firstSpeechAttempt = true
  fake.ports.speak = async (action, signal) => {
    fake.executed.push(action.id)
    if (action.id !== 'speech-1' || !firstSpeechAttempt) return
    firstSpeechAttempt = false
    firstSpeechStarted()
    await new Promise<void>((resolve, reject) => {
      releaseFirstSpeech = resolve
      signal.addEventListener(
        'abort',
        () => reject(new DOMException('switched', 'AbortError')),
        { once: true }
      )
    })
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)
  const initialRun = controller.start()
  await firstSpeech

  const switching = controller.switchScene(1)
  const restarted = controller.start()
  releaseFirstSpeech()
  await Promise.all([initialRun, switching, restarted])

  assert.equal(controller.state, 'completed')
  assert.equal(fake.executed.filter(id => id === 'speech-1').length, 1)
  assert.deepEqual(fake.executed.slice(-3), [
    'scene-2-action-1',
    'scene-2-action-2',
    'scene-2-action-3',
  ])
})

test('an ambiguous checkpoint is replayed before a different public mutation', async () => {
  const fake = fakePlaybackPorts()
  const committed = new Set<string>()
  let durableRevision = 0
  let loseFirstResponse = true
  const attemptedRevisions: number[] = []
  fake.ports.commitCheckpoint = async checkpoint => {
    attemptedRevisions.push(checkpoint.expectedRevision)
    if (committed.has(checkpoint.checkpointId)) return
    assert.equal(checkpoint.expectedRevision, durableRevision)
    committed.add(checkpoint.checkpointId)
    durableRevision = checkpoint.cursor.revision
    if (loseFirstResponse) {
      loseFirstResponse = false
      throw new Error('checkpoint response was lost')
    }
  }
  const document = documentFixture()
  const controller = createPlaybackController(document, fake.ports)
  const event = learningEvent('interactive.event', document.classroomVersionId, NOW, {
    eventId: 'event-before-consume',
    sceneId: 'scene-2',
    interactionId: 'quiz-1',
  })

  await assert.rejects(controller.commitEvents('first-operation', [event]), /response was lost/)
  await controller.markConsumed('scene-2', 'quiz-1')

  assert.deepEqual(attemptedRevisions, [0, 0, 1])
  assert.equal(durableRevision, 2)
  assert.equal(
    controller.snapshot().consumed.includes(playbackInteractionKey('scene-2', 'quiz-1')),
    true
  )
})

test('a failed scene switch settles in a retryable non-switching state', async () => {
  const fake = fakePlaybackPorts()
  fake.ports.commitCheckpoint = async checkpoint => {
    if (checkpoint.checkpointId.includes('switch-scene')) {
      throw new Error('switch checkpoint unavailable')
    }
  }
  const controller = createPlaybackController(documentFixture(), fake.ports)

  await assert.rejects(controller.switchScene(1), /switch checkpoint unavailable/)

  assert.equal(controller.state, 'stopped')
})

test('persisted cursors are bound to the exact classroom version and document fingerprint', () => {
  const fake = fakePlaybackPorts()
  const controller = createPlaybackController(documentFixture(), fake.ports)

  assert.throws(
    () =>
      controller.restore({
        classroomVersionId: 'version-from-another-document',
        documentFingerprint: 'f'.repeat(64),
        revision: 3,
        sceneIndex: 0,
        actionIndex: 0,
        consumed: [],
      } as never),
    /version|fingerprint|cursor/i
  )
})

test('quiz submission retains a final grade while its consumed checkpoint is retried', async () => {
  let gradeCalls = 0
  let gradedCommitCalls = 0
  const coordinator = createQuizSubmissionCoordinator({
    commitAnswered: async () => undefined,
    grade: async () => {
      gradeCalls += 1
      return { status: 'graded', attemptId: 'attempt-1', score: 1 }
    },
    commitGraded: async () => {
      gradedCommitCalls += 1
      if (gradedCommitCalls === 1) throw new Error('checkpoint unavailable')
    },
  })

  await assert.rejects(coordinator.submit('submission-1'), /checkpoint unavailable/)
  await coordinator.submit('submission-1')

  assert.equal(gradeCalls, 1)
  assert.equal(gradedCommitCalls, 2)
})

test('whiteboard actions sanitize SVG paint fields before renderer state', () => {
  let state = createWhiteboardState('whiteboard-safe-paints')
  state = applyWhiteboardAction(state, {
    id: 'unsafe-shape',
    type: 'wb_draw_shape',
    shape: 'rectangle',
    x: 0,
    y: 0,
    width: 100,
    height: 100,
    fillColor: 'url(https://tracker.example/fill.svg#x)',
  })
  state = applyWhiteboardAction(state, {
    id: 'unsafe-line',
    type: 'wb_draw_line',
    startX: 0,
    startY: 0,
    endX: 100,
    endY: 100,
    color: 'url(https://tracker.example/stroke.svg#x)',
  })

  const shape = state.slide.elements.find(element => element.id === 'unsafe-shape')
  const line = state.slide.elements.find(element => element.id === 'unsafe-line')
  assert.equal(shape?.type, 'shape')
  if (shape?.type === 'shape') assert.equal(shape.fill, '#5b9bd5')
  assert.equal(line?.type, 'line')
  if (line?.type === 'line') assert.equal(line.color, '#333333')
})

test('whiteboard latex actions include sanitized KaTeX renderer HTML', () => {
  const state = applyWhiteboardAction(createWhiteboardState('whiteboard-latex'), {
    id: 'latex-1',
    type: 'wb_draw_latex',
    latex: '\\frac{x}{2}',
    x: 0,
    y: 0,
  })
  const latex = state.slide.elements[0]

  assert.equal(latex?.type, 'latex')
  if (latex?.type === 'latex') {
    assert.match(latex.html ?? '', /katex/)
    assert.doesNotMatch(latex.html ?? '', /<script|onerror|javascript:/i)
  }
})
