'use client'

import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  fetchClassLearningReport,
  type ClassLearningReport,
} from '@/lib/learning-api'

export default function TeachingReportsPage() {
  const { t } = useTranslation()
  const [classId, setClassId] = useState('')
  const [report, setReport] = useState<ClassLearningReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    const normalized = classId.trim()
    if (!normalized || loading) return
    setLoading(true)
    setError(null)
    try {
      setReport(await fetchClassLearningReport(normalized))
    } catch (reason) {
      setReport(null)
      setError(reason instanceof Error ? reason.message : t('teaching.reports.loadFailed'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-6xl space-y-6">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight">{t('teaching.reports.title')}</h1>
          <p className="mt-1 text-sm text-[var(--muted-foreground)]">
            {t('teaching.reports.description')}
          </p>
        </header>
        <form
          className="flex flex-wrap gap-3"
          onSubmit={event => {
            event.preventDefault()
            void load()
          }}
        >
          <label className="min-w-64 flex-1 text-sm font-medium">
            {t('teaching.reports.classId')}
            <input
              value={classId}
              onChange={event => setClassId(event.target.value)}
              className="mt-1 block w-full rounded-xl border border-[var(--border)] bg-[var(--background)] px-3 py-2"
            />
          </label>
          <button
            type="submit"
            disabled={!classId.trim() || loading}
            className="self-end rounded-xl bg-[var(--primary)] px-5 py-2 text-sm font-semibold text-[var(--primary-foreground)] disabled:opacity-50"
          >
            {loading ? t('teaching.reports.loading') : t('teaching.reports.load')}
          </button>
        </form>
        {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        {report && (
          <div className="space-y-5">
            <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {[
                ['completionRate', `${Math.round(report.completionRate * 100)}%`],
                ['completedSceneCount', report.completedSceneCount],
                ['validQuizCount', report.validQuizCount],
                ['correctQuizCount', report.correctQuizCount],
                ['hintCount', report.hintCount],
                ['pblMilestoneCount', report.pblMilestoneCount],
                ['sessionCount', report.sessionCount],
                ['projectionLagSeconds', report.projectionLagSeconds.toFixed(1)],
              ].map(([key, value]) => (
                <div key={String(key)} className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-4">
                  <dt className="text-xs text-[var(--muted-foreground)]">{t(`teaching.reports.${key}`)}</dt>
                  <dd className="mt-2 text-2xl font-semibold text-[var(--foreground)]">{value}</dd>
                </div>
              ))}
            </dl>
            <section className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
              <h2 className="font-semibold">{t('teaching.reports.mastery')}</h2>
              <div className="mt-4 space-y-3">
                {report.mastery.map(item => (
                  <div key={item.knowledgePointId}>
                    <div className="flex justify-between gap-3 text-sm">
                      <span>{item.knowledgePointId}</span>
                      <span>
                        {Math.round(item.level * 100)}% ·{' '}
                        {t('teaching.reports.evidenceCount', { count: item.evidenceCount })}
                      </span>
                    </div>
                    <div className="mt-1 h-2 overflow-hidden rounded-full bg-[var(--muted)]">
                      <div className="h-full rounded-full bg-[var(--primary)]" style={{ width: `${item.level * 100}%` }} />
                    </div>
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  )
}
