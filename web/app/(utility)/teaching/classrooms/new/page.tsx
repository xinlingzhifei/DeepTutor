"use client";

import { useTranslation } from "react-i18next";

import { TeachingBriefForm } from "@/components/teaching/TeachingBriefForm";

export default function NewTeachingClassroomPage() {
  const { t } = useTranslation();
  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">{t("teaching.new.title")}</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          {t("teaching.new.description")}
        </p>
      </header>
      <TeachingBriefForm />
    </div>
  );
}
