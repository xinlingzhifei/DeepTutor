'use client'

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { PblSceneContent } from '@/lib/openmaic-adapter'

export interface PblSceneProps {
  content: PblSceneContent
  disabled?: boolean
  completedMilestoneIds?: readonly string[]
  onCompleteMilestone?(milestoneId: string): Promise<void> | void
}

export function PblScene({
  content,
  completedMilestoneIds = [],
  disabled = false,
  onCompleteMilestone,
}: PblSceneProps) {
  const { t } = useTranslation()
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [completed, setCompleted] = useState(() => new Set(completedMilestoneIds))

  useEffect(() => {
    setCompleted(new Set(completedMilestoneIds))
  }, [completedMilestoneIds])

  const complete = async (milestoneId: string) => {
    if (disabled || completed.has(milestoneId) || pending) return
    setPending(milestoneId)
    setError(null)
    try {
      await onCompleteMilestone?.(milestoneId)
      setCompleted(current => new Set(current).add(milestoneId))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('classroom.pbl.unableSave'))
    } finally {
      setPending(null)
    }
  }

  return (
    <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <div className="space-y-4">
        <article className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--primary)]">
            {t('classroom.pbl.scenario')}
          </p>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-[var(--foreground)]">
            {content.scenario}
          </p>
        </article>

        <ol className="space-y-3" aria-label={t('classroom.pbl.milestones')}>
          {content.milestones.map((milestone, index) => {
            const done = completed.has(milestone.id)
            return (
              <li
                key={milestone.id}
                className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-4"
              >
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="text-xs text-[var(--muted-foreground)]">
                      {t('classroom.pbl.milestone', { number: index + 1 })}
                    </p>
                    <h3 className="mt-1 font-semibold text-[var(--foreground)]">
                      {milestone.title}
                    </h3>
                    <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">
                      {milestone.rubric}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={disabled || done || pending !== null}
                    onClick={() => void complete(milestone.id)}
                    className="shrink-0 rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-medium text-[var(--foreground)] disabled:opacity-50"
                  >
                    {done
                      ? t('classroom.pbl.completed')
                      : pending === milestone.id
                        ? t('classroom.pbl.saving')
                        : t('classroom.pbl.complete')}
                  </button>
                </div>
              </li>
            )
          })}
        </ol>
        {error && (
          <p className="text-sm text-red-600" role="alert">
            {error}
          </p>
        )}
      </div>

      <aside className="rounded-2xl border border-[var(--border)] bg-[var(--muted)]/35 p-4">
        <h3 className="text-sm font-semibold text-[var(--foreground)]">
          {t('classroom.pbl.roles')}
        </h3>
        <ul className="mt-3 space-y-3">
          {content.roles.map(role => (
            <li key={role.id}>
              <p className="text-sm font-medium text-[var(--foreground)]">{role.name}</p>
              <p className="mt-1 text-xs leading-5 text-[var(--muted-foreground)]">{role.brief}</p>
            </li>
          ))}
        </ul>
      </aside>
    </section>
  )
}
