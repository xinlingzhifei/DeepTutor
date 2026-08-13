'use client'

import { useTranslation } from 'react-i18next'

import type { ClassroomLearningSession } from '@/lib/learning-api'

export interface LearningProgressPanelProps {
  session: ClassroomLearningSession
  totalScenes: number
  pendingEvents: number
  diagnostics: readonly string[]
}

function cursorSceneIndex(cursor: Record<string, unknown> | null): number {
  const value = cursor?.sceneIndex
  return Number.isInteger(value) && (value as number) >= 0 ? (value as number) : 0
}

export function LearningProgressPanel({
  session,
  totalScenes,
  pendingEvents,
  diagnostics,
}: LearningProgressPanelProps) {
  const { t } = useTranslation()
  const completedScenes = Math.min(totalScenes, cursorSceneIndex(session.lastCursor))
  const percentage = totalScenes > 0 ? Math.round((completedScenes / totalScenes) * 100) : 0

  return (
    <aside className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5 shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold text-[var(--foreground)]">
            {t('classroom.learning.progressTitle')}
          </h2>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            {t(`classroom.learning.status.${session.status}`)}
          </p>
        </div>
        <strong className="text-2xl text-[var(--primary)]">{percentage}%</strong>
      </div>
      <div className="mt-4 h-2 overflow-hidden rounded-full bg-[var(--muted)]">
        <div className="h-full rounded-full bg-[var(--primary)]" style={{ width: `${percentage}%` }} />
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-[var(--muted-foreground)]">{t('classroom.learning.scenes')}</dt>
          <dd className="font-medium text-[var(--foreground)]">
            {completedScenes} / {totalScenes}
          </dd>
        </div>
        <div>
          <dt className="text-[var(--muted-foreground)]">{t('classroom.learning.pending')}</dt>
          <dd className="font-medium text-[var(--foreground)]">{pendingEvents}</dd>
        </div>
      </dl>
      {diagnostics.length > 0 && (
        <div className="mt-4 rounded-xl border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800">
          <p className="font-semibold">{t('classroom.learning.diagnostics')}</p>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {diagnostics.map(item => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
    </aside>
  )
}
