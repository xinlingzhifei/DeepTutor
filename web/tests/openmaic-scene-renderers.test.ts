import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'

import {
  createInteractiveDocumentSource,
  createInteractiveEventQueue,
  readInteractiveMessage,
  sanitizeInteractivePayload,
} from '../lib/openmaic-adapter/playback/interactive-bridge'
import { ClassroomCompatibilityError, type SlideScene } from '../lib/openmaic-adapter/contracts'
import { toRenderableClassroomScene } from '../lib/openmaic-adapter/dsl'
import { sanitizeRendererHtml, sanitizeRendererSlide } from '../lib/openmaic-adapter/sanitize'

const component = (name: string) =>
  readFileSync(path.resolve('components', 'classroom', name), 'utf8')

test('classroom renderers depend only on the yFe adapter boundary', () => {
  for (const name of [
    'ClassroomPlayer.tsx',
    'QuizScene.tsx',
    'InteractiveScene.tsx',
    'PblScene.tsx',
    'WhiteboardLayer.tsx',
  ]) {
    assert.doesNotMatch(component(name), /from\s+["']@openmaic\//)
  }
})

test('interactive scenes use an opaque sandbox and nonce-bound message bridge', () => {
  const source = component('InteractiveScene.tsx')
  assert.match(source, /sandbox="allow-scripts"/)
  assert.doesNotMatch(source, /allow-same-origin/)
  assert.match(source, /event\.source/)
  assert.match(source, /sessionNonce/)
  assert.match(source, /readInteractiveMessage/)
})

test('quiz submission awaits authoritative grading', () => {
  const source = component('QuizScene.tsx')
  assert.match(source, /await onSubmit/)
  assert.match(source, /setSubmitting\(true\)/)
  assert.doesNotMatch(source, /correctOptionIds\.includes/)
})

test('interactive message parsing rejects spoofed, cyclic, and oversized payloads', () => {
  assert.equal(
    readInteractiveMessage(
      { sessionNonce: 'wrong', type: 'interactive.completed' },
      'nonce',
      'frame-a'
    ),
    null
  )
  assert.equal(
    readInteractiveMessage(
      { sessionNonce: 'nonce', type: 'untrusted.event' },
      'nonce',
      'frame-a'
    ),
    null
  )
  assert.deepEqual(
    readInteractiveMessage(
      {
        sessionNonce: 'nonce',
        eventId: 'nonce:frame-a:1',
        type: 'interactive.answer',
        payload: { answer: [1, true, null] },
      },
      'nonce',
      'frame-a'
    ),
    {
      eventId: 'nonce:frame-a:1',
      type: 'interactive.answer',
      payload: { answer: [1, true, null] },
    }
  )

  const cyclic: Record<string, unknown> = {}
  cyclic.self = cyclic
  assert.throws(() => sanitizeInteractivePayload(cyclic), /complex/i)
  assert.throws(() => sanitizeInteractivePayload({ text: 'x'.repeat(65_537) }), /large/i)
  assert.throws(() => sanitizeInteractivePayload(JSON.parse('{"constructor":{}}')), /unsafe/i)
})

test('interactive event queue is bounded, ordered, and continues after errors', async () => {
  const sequence: string[] = []
  const errors: string[] = []
  let releaseFirst!: () => void
  const firstGate = new Promise<void>(resolve => {
    releaseFirst = resolve
  })
  const queue = createInteractiveEventQueue(2, error => {
    errors.push(error.message)
  })

  assert.equal(
    queue.enqueue(async () => {
      sequence.push('first:start')
      await firstGate
      sequence.push('first:end')
    }),
    true
  )
  assert.equal(
    queue.enqueue(() => {
      sequence.push('second')
      throw new Error('consumer failed')
    }),
    true
  )
  assert.equal(
    queue.enqueue(() => {
      sequence.push('overflow')
    }),
    false
  )
  assert.equal(queue.pending, 2)

  await Promise.resolve()
  assert.deepEqual(sequence, ['first:start'])
  releaseFirst()
  await new Promise(resolve => setImmediate(resolve))

  assert.deepEqual(sequence, ['first:start', 'first:end', 'second'])
  assert.deepEqual(errors, ['consumer failed'])
  assert.equal(queue.pending, 0)
})

test('interactive event queue drops work that has not started after cancellation', async () => {
  const sequence: string[] = []
  let releaseFirst!: () => void
  const firstGate = new Promise<void>(resolve => {
    releaseFirst = resolve
  })
  const queue = createInteractiveEventQueue(2, () => undefined)
  queue.enqueue(async () => {
    sequence.push('first')
    await firstGate
  })
  queue.enqueue(() => {
    sequence.push('stale')
  })
  await Promise.resolve()

  queue.cancel()
  releaseFirst()
  await new Promise(resolve => setImmediate(resolve))

  assert.deepEqual(sequence, ['first'])
  assert.equal(queue.enqueue(() => undefined), false)
})

test('interactive iframe source binds the nonce and denies network and forms', () => {
  const source = createInteractiveDocumentSource(
    '<script>parent.postMessage({type:"interactive.ready"}, "*")</script>',
    'nonce</script><script>alert(1)</script>',
    'frame-a'
  )

  assert.match(source, /default-src 'none'/)
  assert.match(source, /connect-src 'none'/)
  assert.match(source, /form-action 'none'/)
  assert.doesNotMatch(
    source.match(/Object\.defineProperty[\s\S]*?<\/script>/)?.[0] ?? '',
    /<script>alert/
  )
  assert.match(source, /\\u003c\/script>/)
  assert.throws(
    () => createInteractiveDocumentSource('<p>safe</p>', 'bad\nnonce', 'frame-a'),
    /nonce/i
  )
})

test('interactive iframe executes only the controlled bridge runtime', () => {
  const source = createInteractiveDocumentSource(
    `<button onclick="location.href='https://tracker.example/nav'" data-yfeistai-event="interactive.completed">Done</button>
     <script>location.href = 'https://tracker.example/script'</script>`,
    'session-1',
    'frame-a'
  )

  assert.doesNotMatch(source, /tracker\.example|onclick|location\.href/i)
  assert.doesNotMatch(source, /script-src 'unsafe-inline'/i)
  assert.match(source, /data-yfeistai-event="interactive\.completed"/)
  assert.equal(source.match(/<script\b/g)?.length, 1)
})

test('interactive bridge assigns and validates a stable event id', () => {
  const parsed = readInteractiveMessage(
    {
      sessionNonce: 'nonce',
      eventId: 'nonce:frame-a:7',
      type: 'interactive.progress',
      payload: { progress: 0.5 },
    },
    'nonce',
    'frame-a'
  ) as (ReturnType<typeof readInteractiveMessage> & { eventId?: string }) | null

  assert.equal(parsed?.eventId, 'nonce:frame-a:7')
  assert.equal(
    readInteractiveMessage(
      { sessionNonce: 'nonce', eventId: '', type: 'interactive.progress' },
      'nonce',
      'frame-a'
    ),
    null
  )
})

test('interactive bridge scopes ids to a parent frame and retries until acknowledged', () => {
  const first = createInteractiveDocumentSource(
    '<button data-yfeistai-event="interactive.completed">Done</button>',
    'session-1',
    'frame-a'
  )
  const second = createInteractiveDocumentSource(
    '<button data-yfeistai-event="interactive.completed">Done</button>',
    'session-1',
    'frame-b'
  )

  assert.match(first, /nonce\+":"\+frameId\+":"\+sequence/)
  assert.match(first, /interactive\.ack/)
  assert.match(first, /pending/)
  assert.match(first, /frame-a/)
  assert.match(second, /frame-b/)
  assert.equal(
    readInteractiveMessage(
      {
        sessionNonce: 'session-1',
        eventId: 'session-1:frame-a:1',
        type: 'interactive.completed',
      },
      'session-1',
      'frame-b'
    ),
    null
  )
})

test('classroom teardown guards direct host work and preserves uncertain projections', () => {
  const player = component('ClassroomPlayer.tsx')
  const interactive = component('InteractiveScene.tsx')
  const quiz = component('QuizScene.tsx')

  assert.match(player, /ensureCurrentBinding/)
  assert.match(player, /portBindingRef\.current\?\.document !== document/)
  assert.match(player, /quizSubmissionsRef/)
  assert.match(
    player,
    /await portBinding\.handleInteractiveEvent[\s\S]*?ensureCurrentBinding\(\)[\s\S]*?controller\.(?:markConsumed|commitEvents)/
  )
  assert.doesNotMatch(player, /reconstructWhiteboardState\(document, created\.snapshot\(\)\)/)
  assert.match(interactive, /active = false/)
  assert.match(interactive, /eventQueue\.cancel\(\)/)
  assert.match(interactive, /interactive\.ack/)
  assert.match(quiz, /retryAnswer/)
})

test('interactive ingress can be rejected before payload parsing', async () => {
  const bridgeModule = (await import('../lib/openmaic-adapter/playback/interactive-bridge')) as {
    createInteractiveIngressLimiter?: (
      maximum: number,
      windowMs: number,
      now?: () => number
    ) => { accept(): boolean }
  }
  assert.ok(bridgeModule.createInteractiveIngressLimiter)
  let now = 0
  const limiter = bridgeModule.createInteractiveIngressLimiter(2, 1_000, () => now)
  assert.equal(limiter.accept(), true)
  assert.equal(limiter.accept(), true)
  assert.equal(limiter.accept(), false)
  now = 1_001
  assert.equal(limiter.accept(), true)

  const source = component('InteractiveScene.tsx')
  assert.ok(source.indexOf('.accept()') < source.indexOf('readInteractiveMessage('))
})

test('renderer HTML sanitizer strips executable markup and preserves formatting', () => {
  const sanitized = sanitizeRendererHtml(`
    <img src="javascript:alert(1)" onerror="alert(1)">
    <script>alert(1)</script>
    <p style="color:#123456;background-image:url(javascript:alert(1))">
      Safe <strong>formatting</strong>
    </p>
    <svg onload="alert(1)" viewBox="0 0 10 10">
      <path d="M0 0 L10 10" fill="#ffffff"></path>
    </svg>
  `)

  assert.doesNotMatch(sanitized, /script|onerror|onload|javascript|<img/i)
  assert.doesNotMatch(sanitized, /background-image/i)
  assert.match(sanitized, /<p style="color:#123456">/)
  assert.match(sanitized, /<strong>formatting<\/strong>/)
  assert.match(sanitized, /<svg viewBox="0 0 10 10">/)
  assert.throws(() => sanitizeRendererHtml('x'.repeat(1_000_001)), /supported size/i)
})

test('renderer slide sanitizer rejects CSS URLs in persisted backgrounds', () => {
  const slide = sanitizeRendererSlide({
    id: 'slide-1',
    viewportSize: 1_000,
    viewportRatio: 9 / 16,
    theme: {
      backgroundColor: '#ffffff',
      themeColors: ['#2563eb'],
      fontColor: '#111111',
      fontName: 'sans-serif',
    },
    background: {
      type: 'gradient',
      color: 'url(https://tracker.example/pixel)',
      gradient: {
        type: 'linear',
        rotate: 0,
        colors: [
          { pos: 0, color: '#ffffff' },
          { pos: 1, color: 'url(https://tracker.example/pixel)' },
        ],
      },
    },
    elements: [],
  })

  assert.equal(slide.background?.color, undefined)
  assert.equal(slide.background?.gradient?.colors[1]?.color, '#ffffff')
})

test('renderer slide sanitizer removes URL-bearing image filters and paint fields', () => {
  const slide = sanitizeRendererSlide({
    id: 'slide-filter',
    viewportSize: 1_000,
    viewportRatio: 9 / 16,
    theme: {
      backgroundColor: '#ffffff',
      themeColors: ['#2563eb'],
      fontColor: '#111111',
      fontName: 'sans-serif',
    },
    elements: [
      {
        id: 'image-1',
        type: 'image',
        left: 0,
        top: 0,
        width: 100,
        height: 100,
        rotate: 0,
        fixedRatio: true,
        src: 'data:image/png;base64,AA==',
        filters: {
          blur: '0) url(https://tracker.example/filter.svg#x) blur(0',
          brightness: '80%',
        },
        colorMask: 'url(https://tracker.example/mask.svg#x)',
        outline: { color: 'url(https://tracker.example/stroke.svg#x)' },
        shadow: { h: 1, v: 1, blur: 2, color: 'url(https://tracker.example/shadow.svg#x)' },
      },
      {
        id: 'shape-1',
        type: 'shape',
        left: 0,
        top: 0,
        width: 100,
        height: 100,
        rotate: 0,
        viewBox: [100, 100],
        path: 'M0 0 L100 0 L100 100 Z',
        fixedRatio: false,
        fill: 'url(https://tracker.example/fill.svg#x)',
        outline: { color: 'url(https://tracker.example/outline.svg#x)' },
      },
    ],
  })
  const image = slide.elements[0]
  const shape = slide.elements[1]

  assert.equal(image?.type, 'image')
  if (image?.type === 'image') {
    assert.equal(image.filters?.blur, undefined)
    assert.equal(image.filters?.brightness, '80%')
    assert.equal(image.colorMask, undefined)
    assert.equal(image.outline?.color, undefined)
    assert.equal(image.shadow?.color, undefined)
  }
  assert.equal(shape?.type, 'shape')
  if (shape?.type === 'shape') {
    assert.equal(shape.fill, '#5b9bd5')
    assert.equal(shape.outline?.color, undefined)
  }
})

test('renderer slide defaults use height divided by width', () => {
  const scene: SlideScene = {
    id: 'scene-default-ratio',
    stageId: 'stage-1',
    title: 'Default ratio',
    order: 0,
    type: 'slide',
    content: { type: 'slide', canvas: { elements: [] } },
    actions: [],
  }

  const renderable = toRenderableClassroomScene(scene)
  assert.equal(renderable.type, 'slide')
  if (renderable.type === 'slide') {
    assert.equal(renderable.content.canvas.viewportSize, 1_000)
    assert.equal(renderable.content.canvas.viewportRatio, 9 / 16)
  }
})

test('malformed renderer slides fail as compatibility errors', () => {
  const scene: SlideScene = {
    id: 'scene-malformed',
    stageId: 'stage-1',
    title: 'Malformed',
    order: 0,
    type: 'slide',
    content: {
      type: 'slide',
      canvas: {
        elements: [
          {
            id: 'chart-1',
            type: 'chart',
            left: 0,
            top: 0,
            width: 100,
            height: 100,
            rotate: 0,
            chartType: 'bar',
            data: { labels: [], legends: [], series: [] },
            themeColors: null,
          },
        ],
      },
    },
    actions: [],
  } as unknown as SlideScene

  assert.throws(
    () => toRenderableClassroomScene(scene),
    error =>
      error instanceof ClassroomCompatibilityError &&
      error.code === 'OPENMAIC_VALIDATION_FAILED'
  )
})

test('classroom error reporting remains stable across language switches', () => {
  const source = component('ClassroomPlayer.tsx')
  assert.match(source, /translationRef\.current/)
  assert.doesNotMatch(source, /const reportError[\s\S]*?\n\s*\[t\]\s*\n\s*\)/)
})

test('classroom UI copy is routed through the shared translation hook', () => {
  for (const name of [
    'ClassroomPlayer.tsx',
    'QuizScene.tsx',
    'InteractiveScene.tsx',
    'PblScene.tsx',
    'WhiteboardLayer.tsx',
  ]) {
    assert.match(component(name), /useTranslation/)
  }
})
