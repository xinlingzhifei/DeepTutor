"use client";

import { useTranslation } from "react-i18next";

import type { QuizScene } from "@/lib/openmaic-adapter/contracts";
import type { QuizUpdateOperation } from "@/lib/openmaic-adapter/scene-operations";

export interface QuizEditorProps {
  scene: QuizScene;
  knowledgePointIds: readonly string[];
  disabled?: boolean;
  onOperation(operation: QuizUpdateOperation): void;
}

function sameStrings(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

export function QuizEditor({
  scene,
  knowledgePointIds,
  disabled = false,
  onOperation,
}: QuizEditorProps) {
  const { t } = useTranslation();
  return (
    <div className="space-y-5">
      {scene.content.questions.map(question => {
        const optionLabels = question.options.map(option => option.label);
        return (
          <fieldset
            key={question.id}
            disabled={disabled}
            className="space-y-3 rounded-lg border border-[var(--border)] p-3 disabled:opacity-60"
          >
            <legend className="px-1 text-sm font-semibold">
              {t("classroom.editor.question", { id: question.id })}
            </legend>
            <label className="block text-xs font-medium text-[var(--muted-foreground)]">
              {t("classroom.editor.prompt")}
              <textarea
                key={`${question.id}:prompt:${question.prompt}`}
                defaultValue={question.prompt}
                onBlur={event => {
                  if (event.currentTarget.value !== question.prompt) {
                    onOperation({
                      type: "quiz.update",
                      sceneId: scene.id,
                      questionId: question.id,
                      prompt: event.currentTarget.value,
                    });
                  }
                }}
                className="mt-1 min-h-20 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-2 text-sm text-[var(--foreground)]"
              />
            </label>
            {question.questionType !== "short_answer" && (
              <label className="block text-xs font-medium text-[var(--muted-foreground)]">
                {t("classroom.editor.optionsOnePerLine")}
                <textarea
                  key={`${question.id}:options:${optionLabels.join("\u0000")}`}
                  defaultValue={optionLabels.join("\n")}
                  onBlur={event => {
                    const options = event.currentTarget.value
                      .split(/\r?\n/)
                      .map(value => value.trim())
                      .filter(Boolean);
                    if (!sameStrings(options, optionLabels)) {
                      onOperation({
                        type: "quiz.update",
                        sceneId: scene.id,
                        questionId: question.id,
                        options,
                      });
                    }
                  }}
                  className="mt-1 min-h-24 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-2 text-sm text-[var(--foreground)]"
                />
              </label>
            )}
            {question.questionType === "single_choice" && (
              <label className="block text-xs font-medium text-[var(--muted-foreground)]">
                {t("classroom.editor.correctAnswer")}
                <select
                  value={Math.max(
                    0,
                    question.options.findIndex(option =>
                      question.correctOptionIds.includes(option.id),
                    ),
                  )}
                  onChange={event =>
                    onOperation({
                      type: "quiz.update",
                      sceneId: scene.id,
                      questionId: question.id,
                      correctOption: Number(event.currentTarget.value),
                    })
                  }
                  className="mt-1 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-2 text-sm"
                >
                  {question.options.map((option, index) => (
                    <option key={option.id} value={index}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {question.questionType === "multiple_choice" && (
              <label className="block text-xs font-medium text-[var(--muted-foreground)]">
                {t("classroom.editor.correctOptionIds")}
                <input
                  key={`${question.id}:answers:${question.correctOptionIds.join(",")}`}
                  defaultValue={question.correctOptionIds.join(", ")}
                  onBlur={event => {
                    const ids = event.currentTarget.value
                      .split(",")
                      .map(value => value.trim())
                      .filter(Boolean);
                    if (!sameStrings(ids, question.correctOptionIds)) {
                      onOperation({
                        type: "quiz.update",
                        sceneId: scene.id,
                        questionId: question.id,
                        correctOptionIds: ids,
                      });
                    }
                  }}
                  className="mt-1 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-2 text-sm"
                />
              </label>
            )}
            <label className="block text-xs font-medium text-[var(--muted-foreground)]">
              {t("classroom.editor.explanation")}
              <textarea
                key={`${question.id}:explanation:${question.explanation}`}
                defaultValue={question.explanation}
                onBlur={event => {
                  if (event.currentTarget.value !== question.explanation) {
                    onOperation({
                      type: "quiz.update",
                      sceneId: scene.id,
                      questionId: question.id,
                      explanation: event.currentTarget.value,
                    });
                  }
                }}
                className="mt-1 min-h-20 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-2 text-sm"
              />
            </label>
          </fieldset>
        );
      })}
      <label className="block text-xs font-medium text-[var(--muted-foreground)]">
        {t("classroom.editor.knowledgePointIds")}
        <input
          key={`${scene.id}:knowledge:${knowledgePointIds.join(",")}`}
          defaultValue={knowledgePointIds.join(", ")}
          disabled={disabled}
          onBlur={event => {
            const ids = event.currentTarget.value
              .split(",")
              .map(value => value.trim())
              .filter(Boolean);
            if (!sameStrings(ids, knowledgePointIds)) {
              onOperation({
                type: "quiz.update",
                sceneId: scene.id,
                questionId: scene.content.questions[0]?.id,
                knowledgePointIds: ids,
              });
            }
          }}
          className="mt-1 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-2 text-sm disabled:opacity-60"
        />
      </label>
    </div>
  );
}
