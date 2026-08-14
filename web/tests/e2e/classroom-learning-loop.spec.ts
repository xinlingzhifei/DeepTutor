import type { Page, Request, Route } from '@playwright/test'

import { apiPayload } from './support/baseline-api-fixtures'
import { expect, test } from './support/teaching-flow-test'

const VERSION_ID = 'version-learning-loop'
const SESSION_ID = 'session-learning-loop'
const ASSIGNMENT_ID = 'assignment-learning-loop'

type LearningEvent = {
  event_id: string
  event_type: string
}

function requestJson(request: Request): Record<string, unknown> {
  const raw = request.postData()
  if (!raw) return {}
  const value = JSON.parse(raw) as unknown
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

async function json(route: Route, payload: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(payload),
  })
}

function classroomDocument() {
  const now = '2026-08-13T08:00:00.000Z'
  return {
    schemaVersion: '1.0',
    classroomId: 'classroom-learning-loop',
    classroomVersionId: VERSION_ID,
    contentMode: 'open_creation',
    openCreation: true,
    openmaic: {
      dslVersion: '0.1.0',
      stage: {
        id: 'stage-learning-loop',
        name: 'Learning loop',
        createdAt: now,
        updatedAt: now,
      },
      scenes: [
        {
          id: 'scene-slide',
          stageId: 'stage-learning-loop',
          title: 'Energy pathway',
          order: 0,
          type: 'slide',
          content: { type: 'slide', canvas: {} },
          actions: [],
        },
        {
          id: 'scene-quiz',
          stageId: 'stage-learning-loop',
          title: 'Energy check',
          order: 1,
          type: 'quiz',
          content: {
            type: 'quiz',
            questions: [
              {
                id: 'question-energy',
                prompt: 'Which answer conserves energy?',
                questionType: 'single_choice',
                options: [
                  { id: 'answer-a', label: 'Energy transfers between stores' },
                  { id: 'answer-b', label: 'Energy disappears' },
                ],
                correctOptionIds: ['answer-a'],
                explanation: 'Energy is conserved while it transfers.',
              },
            ],
          },
          actions: [],
        },
        {
          id: 'scene-pbl',
          stageId: 'stage-learning-loop',
          title: 'Energy investigation',
          order: 2,
          type: 'pbl',
          content: {
            type: 'pbl',
            scenario: 'Explain the energy pathway in a real system.',
            roles: [
              { id: 'analyst', name: 'Analyst', brief: 'Trace each transfer.' },
            ],
            milestones: [
              {
                id: 'milestone-explain',
                title: 'Explain the pathway',
                rubric: 'Name the stores and transfers.',
              },
            ],
          },
          actions: [],
        },
      ],
    },
    interactionIds: ['scene-quiz', 'scene-pbl'],
    sourceRefs: [],
    knowledgePointMappings: [
      {
        knowledgePointId: 'kp-energy',
        sceneIds: ['scene-quiz'],
        sourceRefs: [],
      },
    ],
    mediaManifest: [],
    fileSha256: 'a'.repeat(64),
    exportManifest: [],
    generationMetadata: {
      generator: 'e2e',
      generatorVersion: '1',
      modelId: 'fixture',
      generatedAt: now,
      teachingBriefId: 'brief-learning-loop',
      teachingBriefSha256: 'b'.repeat(64),
      templateId: 'template-learning-loop',
      templateVersion: '1',
    },
    auditMetadata: {
      templateId: 'template-learning-loop',
      templateVersion: '1',
      teachingBriefId: 'brief-learning-loop',
      teachingBriefSha256: 'b'.repeat(64),
      parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: now },
    migrationRecords: [],
  }
}

function session(status: 'active' | 'completed', cursor: Record<string, unknown> | null) {
  return {
    id: SESSION_ID,
    tenant_id: 'tenant-learning-loop',
    user_id: 'student-learning-loop',
    classroom_version_id: VERSION_ID,
    assignment_id: ASSIGNMENT_ID,
    student_asset_id: null,
    status,
    last_cursor: cursor,
    started_at: '2026-08-13T08:00:00Z',
    completed_at: status === 'completed' ? '2026-08-13T08:10:00Z' : null,
  }
}

async function installLearningBackend(page: Page) {
  const state = {
    cursor: null as Record<string, unknown> | null,
    status: 'active' as 'active' | 'completed',
    eventAttempts: 0,
    completionCalls: 0,
    accepted: new Set<string>(),
    batches: [] as LearningEvent[][],
    unexpected: [] as string[],
  }

  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const method = request.method()
    const pathname = new URL(request.url()).pathname

    if (method === 'POST' && pathname === '/api/v1/classroom-sessions') {
      expect(requestJson(request)).toEqual({ assignment_id: ASSIGNMENT_ID })
      await json(route, session(state.status, state.cursor))
      return
    }
    if (method === 'GET' && pathname === `/api/v1/classroom-sessions/${SESSION_ID}`) {
      await json(route, session(state.status, state.cursor))
      return
    }
    if (
      method === 'PUT' &&
      pathname === `/api/v1/classroom-sessions/${SESSION_ID}/cursor`
    ) {
      const body = requestJson(request)
      state.cursor = body.cursor as Record<string, unknown>
      await json(route, session(state.status, state.cursor))
      return
    }
    if (
      method === 'POST' &&
      pathname === `/api/v1/classroom-sessions/${SESSION_ID}/read-ticket`
    ) {
      await json(route, { ticket: 'read-ticket-learning-loop' })
      return
    }
    if (
      method === 'POST' &&
      pathname === `/api/v1/classroom-sessions/${SESSION_ID}/event-ticket`
    ) {
      await json(route, { ticket: `event-ticket-${state.eventAttempts + 1}` })
      return
    }
    if (
      method === 'POST' &&
      pathname === `/api/v1/classroom-sessions/${SESSION_ID}/events`
    ) {
      state.eventAttempts += 1
      const events = requestJson(request).events as LearningEvent[]
      state.batches.push(events)
      if (state.eventAttempts === 1) {
        await json(route, { detail: 'simulated network interruption' }, 503)
        return
      }
      const accepted = events.filter(event => !state.accepted.has(event.event_id))
      const duplicate = events.filter(event => state.accepted.has(event.event_id))
      accepted.forEach(event => state.accepted.add(event.event_id))
      await json(route, {
        accepted: accepted.map((event, index) => ({
          event_id: event.event_id,
          seq: state.accepted.size - accepted.length + index + 1,
        })),
        duplicate: duplicate.map((event, index) => ({
          event_id: event.event_id,
          seq: index + 1,
        })),
        quarantined: [],
      })
      return
    }
    if (
      method === 'POST' &&
      pathname === `/api/v1/classroom-sessions/${SESSION_ID}/complete`
    ) {
      state.completionCalls += 1
      state.status = 'completed'
      await json(route, session(state.status, state.cursor))
      return
    }
    if (method === 'GET' && pathname === `/api/v1/classroom-versions/${VERSION_ID}/document`) {
      await json(route, classroomDocument())
      return
    }
    if (method === 'GET') {
      const payload = apiPayload(pathname, 'snow')
      if (payload !== undefined) {
        await json(route, payload)
        return
      }
    }

    state.unexpected.push(`${method} ${pathname}`)
    await json(route, { detail: 'Unexpected E2E API request' }, 404)
  })
  return state
}

test.use({ locale: 'en-US', timezoneId: 'UTC' })
test.describe.configure({ mode: 'serial' })

test('learner events survive a network interruption and complete exactly once', async ({
  page,
}) => {
  test.setTimeout(180_000)
  await page.addInitScript(() => {
    localStorage.setItem('deeptutor-language', 'en')
  })
  const state = await installLearningBackend(page)

  await page.goto(`/learn/classrooms/${VERSION_ID}?assignmentId=${ASSIGNMENT_ID}`)
  await expect(page.getByText('Energy pathway')).toBeVisible({ timeout: 60_000 })

  await page.getByRole('button', { name: 'Next scene' }).click()
  await expect(page.getByText('Which answer conserves energy?')).toBeVisible()
  await page.getByRole('button', { name: 'Show hint' }).click()
  await page.getByLabel('Energy transfers between stores').check()
  await page.getByRole('button', { name: 'Submit' }).click()
  await expect(page.getByText('Graded')).toBeVisible()

  await page.getByRole('button', { name: 'Next scene' }).click()
  await page.getByRole('button', { name: 'Complete' }).click()
  await expect(page.getByRole('button', { name: 'Completed' })).toBeDisabled()

  await page.getByRole('button', { name: 'Previous scene' }).click()
  await page.getByRole('button', { name: 'Previous scene' }).click()
  await page.getByRole('button', { name: 'Play' }).click()
  await expect(page.getByText('Classroom learning request failed: 503')).toBeVisible()
  expect(state.eventAttempts).toBe(1)
  expect(state.completionCalls).toBe(0)

  await page.reload()
  await expect(page.getByText('Energy pathway')).toBeVisible({ timeout: 60_000 })
  const progress = page
    .getByRole('heading', { name: 'Learning progress' })
    .locator('xpath=ancestor::aside')
  await expect(progress.getByText('Completed', { exact: true })).toBeVisible({
    timeout: 60_000,
  })
  await expect(progress.getByText('100%', { exact: true })).toBeVisible()
  await expect(progress.getByText('3 / 3', { exact: true })).toBeVisible()
  await expect(progress.getByText('0', { exact: true })).toBeVisible()

  expect(state.eventAttempts).toBe(2)
  expect(state.completionCalls).toBe(1)
  expect(state.accepted.size).toBe(8)
  expect(state.batches[1]?.map(event => event.event_id)).toEqual(
    state.batches[0]?.map(event => event.event_id)
  )
  expect(new Set(state.batches[1]?.map(event => event.event_id)).size).toBe(
    state.batches[1]?.length
  )
  expect(state.unexpected).toEqual([])
})
