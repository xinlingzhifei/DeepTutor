"use client";

import { useTranslation } from "react-i18next";

import { BatchWorkbench } from "@/components/teaching/BatchWorkbench";

export default function TeachingBatchesPage() {
  const { t } = useTranslation();
  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">{t("teaching.batches.title")}</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          {t("teaching.batches.description")}
        </p>
      </header>
      <BatchWorkbench />
    </div>
  );
}
