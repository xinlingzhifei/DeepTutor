'use client'

import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { QuizQuestion, QuizSceneContent } from '@/lib/openmaic-adapter'

export interface QuizGradeResult {
  attemptId: string
  status: 'graded'
  score: number | null
  feedback?: string
}

type QuizAnswer = { optionIds?: string[]; text?: string }

export interface QuizSceneProps {
  content: QuizSceneContent
  disabled?: boolean
  submittedQuestionIds?: readonly string[]
  onSubmit(
    questionId: string,
    answer: { optionIds?: string[]; text?: string }
  ): Promise<QuizGradeResult>
  onGraded?(questionId: string, result: QuizGradeResult): Promise<void> | void
  onHint?(questionId: string): Promise<void> | void
}

function Question({
  question,
  onSubmit,
  onGraded,
  onHint,
  alreadySubmitted,
  disabled,
}: {
  question: QuizQuestion
  onSubmit: QuizSceneProps['onSubmit']
  onGraded: QuizSceneProps['onGraded']
  onHint: QuizSceneProps['onHint']
  alreadySubmitted: boolean
  disabled: boolean
}) {
  const { t } = useTranslation()
  const groupName = useId()
  const [selected, setSelected] = useState<string[]>([])
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<QuizGradeResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retryAnswer, setRetryAnswer] = useState<QuizAnswer | null>(null)
  const [hintVisible, setHintVisible] = useState(false)

  const selectOption = (optionId: string) => {
    if (question.questionType === 'single_choice') {
      setSelected([optionId])
      return
    }
    setSelected(current =>
      current.includes(optionId)
        ? current.filter(value => value !== optionId)
        : [...current, optionId]
    )
  }

  const canSubmit =
    question.questionType === 'short_answer' ? text.trim().length > 0 : selected.length > 0

  const submit = async () => {
    if ((!canSubmit && !retryAnswer) || submitting || result || alreadySubmitted || disabled) return
    setSubmitting(true)
    setError(null)
    const answer =
      retryAnswer ??
      (question.questionType === 'short_answer'
        ? { text: text.trim() }
        : { optionIds: [...selected] })
    setRetryAnswer(answer)
    try {
      const grade = await onSubmit(question.id, answer)
      setResult(grade)
      await onGraded?.(question.id, grade)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('classroom.quiz.unableSubmit'))
    } finally {
      setSubmitting(false)
    }
  }

  const showHint = async () => {
    if (disabled || submitting || hintVisible || alreadySubmitted) return
    setError(null)
    try {
      await onHint?.(question.id)
      setHintVisible(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('classroom.quiz.unableHint'))
    }
  }

  return (
    <article className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5 shadow-sm">
      <h3 className="text-base font-semibold text-[var(--foreground)]">{question.prompt}</h3>

      {question.questionType === 'short_answer' ? (
        <label className="mt-4 block text-sm text-[var(--muted-foreground)]">
          <span className="sr-only">{t('classroom.quiz.answer')}</span>
          <textarea
            value={text}
            onChange={event => setText(event.target.value)}
            disabled={
              disabled || submitting || Boolean(result) || alreadySubmitted || Boolean(retryAnswer)
            }
            rows={4}
            className="w-full resize-y rounded-xl border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-[var(--foreground)] outline-none focus:border-[var(--primary)] disabled:opacity-60"
          />
        </label>
      ) : (
        <fieldset
          className="mt-4 space-y-2"
          disabled={
            disabled || submitting || Boolean(result) || alreadySubmitted || Boolean(retryAnswer)
          }
        >
          <legend className="sr-only">{t('classroom.quiz.choices')}</legend>
          {question.options.map(option => {
            const checked = selected.includes(option.id)
            return (
              <label
                key={option.id}
                className="flex cursor-pointer items-center gap-3 rounded-xl border border-[var(--border)] px-3 py-2 text-sm text-[var(--foreground)] transition-colors has-[:checked]:border-[var(--primary)] has-[:checked]:bg-[var(--primary)]/10"
              >
                <input
                  type={question.questionType === 'single_choice' ? 'radio' : 'checkbox'}
                  name={groupName}
                  checked={checked}
                  onChange={() => selectOption(option.id)}
                  className="accent-[var(--primary)]"
                />
                <span>{option.label}</span>
              </label>
            )
          })}
        </fieldset>
      )}

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={() => void submit()}
          disabled={
            disabled ||
            (!canSubmit && !retryAnswer) ||
            submitting ||
            Boolean(result) ||
            alreadySubmitted
          }
          className="rounded-lg bg-[var(--primary)] px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting
            ? t('classroom.quiz.grading')
            : result || alreadySubmitted
              ? t('classroom.quiz.submitted')
              : t('classroom.quiz.submit')}
        </button>
        <button
          type="button"
          onClick={() => void showHint()}
          disabled={disabled || submitting || hintVisible || alreadySubmitted}
          className="rounded-lg border border-[var(--border)] px-4 py-2 text-sm font-medium text-[var(--foreground)] disabled:opacity-50"
        >
          {hintVisible ? t('classroom.quiz.hintShown') : t('classroom.quiz.hint')}
        </button>
        {result?.status === 'graded' && (
          <output className="text-sm text-[var(--muted-foreground)]">
            {result.score === null
              ? t('classroom.quiz.graded')
              : t('classroom.quiz.score', { score: result.score })}
          </output>
        )}
      </div>

      {hintVisible && (
        <p className="mt-3 rounded-lg bg-[var(--muted)]/50 px-3 py-2 text-sm text-[var(--foreground)]">
          {question.explanation}
        </p>
      )}

      {result?.feedback && (
        <p className="mt-3 text-sm text-[var(--muted-foreground)]" role="status">
          {result.feedback}
        </p>
      )}
      {error && (
        <p className="mt-3 text-sm text-red-600" role="alert">
          {error}
        </p>
      )}
    </article>
  )
}

export function QuizScene({
  content,
  submittedQuestionIds = [],
  disabled = false,
  onSubmit,
  onGraded,
  onHint,
}: QuizSceneProps) {
  const { t } = useTranslation()
  return (
    <section className="space-y-4" aria-label={t('classroom.quiz.label')}>
      {content.questions.map(question => (
        <Question
          key={question.id}
          question={question}
          onSubmit={onSubmit}
          onGraded={onGraded}
          onHint={onHint}
          alreadySubmitted={submittedQuestionIds.includes(question.id)}
          disabled={disabled}
        />
      ))}
    </section>
  )
}
