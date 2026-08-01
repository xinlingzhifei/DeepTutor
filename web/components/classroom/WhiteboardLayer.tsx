'use client'

import { useTranslation } from 'react-i18next'
import 'katex/dist/katex.min.css'

import { ClassroomSlideCanvas } from '@/lib/openmaic-adapter'
import type { WhiteboardState } from '@/lib/openmaic-adapter/playback/action-reducer'

export interface WhiteboardLayerProps {
  state: WhiteboardState
  onClose?(): void
}

export function WhiteboardLayer({ state, onClose }: WhiteboardLayerProps) {
  const { t } = useTranslation()
  if (!state.open) return null

  return (
    <section
      className="absolute inset-3 z-20 overflow-hidden rounded-2xl border border-[var(--border)] bg-white shadow-2xl"
      aria-label={t('classroom.whiteboard.label')}
    >
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          className="absolute right-3 top-3 z-10 rounded-lg bg-black/65 px-3 py-1.5 text-xs font-medium text-white"
        >
          {t('classroom.whiteboard.close')}
        </button>
      )}
      <ClassroomSlideCanvas
        slide={state.slide}
        className="h-full w-full"
        canvasPercentage={100}
        chrome={false}
      />
    </section>
  )
}
