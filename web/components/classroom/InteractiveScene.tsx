'use client'

import { useEffect, useEffectEvent, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { InteractiveSceneContent } from '@/lib/openmaic-adapter'
import {
  createInteractiveDocumentSource,
  createInteractiveEventQueue,
  createInteractiveIngressLimiter,
  readInteractiveMessage,
  type InteractiveEvent,
} from '@/lib/openmaic-adapter/playback/interactive-bridge'

export type { InteractiveEvent } from '@/lib/openmaic-adapter/playback/interactive-bridge'

export interface InteractiveSceneProps {
  content: InteractiveSceneContent
  disabled?: boolean
  frameInstanceId: string
  sessionNonce: string
  title?: string
  onEvent(event: InteractiveEvent): Promise<void> | void
  onError?(error: Error): void
}

export function InteractiveScene({
  content,
  disabled = false,
  frameInstanceId,
  sessionNonce,
  title,
  onEvent,
  onError,
}: InteractiveSceneProps) {
  const { t } = useTranslation()
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [ownedFrameInstanceId] = useState(frameInstanceId)
  const source = useMemo(
    () => createInteractiveDocumentSource(content.html, sessionNonce, ownedFrameInstanceId),
    [content.html, ownedFrameInstanceId, sessionNonce]
  )
  const dispatchEvent = useEffectEvent((event: InteractiveEvent) => onEvent(event))
  const reportError = useEffectEvent((error: Error) => onError?.(error))

  useEffect(() => {
    if (disabled) return
    const eventQueue = createInteractiveEventQueue(32, reportError)
    const ingressLimiter = createInteractiveIngressLimiter(120, 1_000)
    let rateLimitReported = false
    let active = true
    const receive = (event: MessageEvent<unknown>) => {
      if (event.source !== frameRef.current?.contentWindow) return
      if (event.origin !== 'null') return
      if (!ingressLimiter.accept()) {
        if (!rateLimitReported) {
          rateLimitReported = true
          reportError(new Error('interactive event rate limit exceeded'))
        }
        return
      }
      rateLimitReported = false
      if (!eventQueue.canAccept()) {
        reportError(new Error('interactive event queue is full'))
        return
      }
      let message: InteractiveEvent | null
      try {
        message = readInteractiveMessage(event.data, sessionNonce, ownedFrameInstanceId)
      } catch (reason) {
        reportError(reason instanceof Error ? reason : new Error('interactive event failed'))
        return
      }
      if (!message) return
      const accepted = eventQueue.enqueue(async () => {
        if (!active) return
        await dispatchEvent(message)
        if (!active) return
        frameRef.current?.contentWindow?.postMessage(
          { sessionNonce, eventId: message.eventId, type: 'interactive.ack' },
          '*'
        )
      })
      if (!accepted) {
        reportError(new Error('interactive event queue is full'))
      }
    }
    window.addEventListener('message', receive)
    return () => {
      active = false
      eventQueue.cancel()
      window.removeEventListener('message', receive)
    }
  }, [disabled, ownedFrameInstanceId, sessionNonce])

  return (
    <iframe
      ref={frameRef}
      title={title ?? t('classroom.interactive.title')}
      srcDoc={source}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : undefined}
      className={`aspect-video w-full rounded-2xl border border-[var(--border)] bg-white shadow-sm ${disabled ? 'pointer-events-none opacity-60' : ''}`}
    />
  )
}
