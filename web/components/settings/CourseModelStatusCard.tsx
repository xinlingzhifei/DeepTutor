"use client";

import Link from "next/link";
import { useTranslation } from "react-i18next";
import { useCourseModelStatus } from "@/hooks/useCourseModelStatus";

const REASONS: Record<string, string> = {
  unsupported_provider: "This provider is not supported for course generation. Choose an OpenAI-compatible or Anthropic API model.",
  unsupported_provider_options: "Course generation does not yet support extra headers or API version options.",
  provider_endpoint_required: "Enter the model API address and save again.",
  invalid_provider_configuration: "Check the saved model name, API address and credential, then save again.",
  platform_not_initialized: "Course services have not finished initialization. Contact the administrator.",
  data_plane_unavailable: "The course service is unavailable. Contact the administrator.",
};

export function CourseModelStatusNotice({
  model, settings = false,
}: {
  model: ReturnType<typeof useCourseModelStatus>;
  settings?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const { status, loading, error, refresh } = model;
  const description = loading ? "Checking course model configuration…"
    : error ? "Could not check the course model. Refresh to try again."
    : status?.state === "synced" ? (settings
      ? "The saved default model is synchronized. Course generation is available."
      : "Course model configuration is ready.")
    : status?.state === "not_enabled" ? "Course generation is not enabled on this server."
    : status?.state === "not_configured" ? "Select a default model and save its API credential before generating courses."
    : REASONS[status?.reason || ""] || "The saved model is not synchronized. Apply the configuration again or contact the administrator.";

  return (
    <section aria-label={t("Course model status")} data-testid="course-model-status"
      className="mb-5 space-y-3 rounded-xl border border-[var(--border)] bg-[var(--card)] p-4 text-sm">
      <h2 className="font-semibold">{t("Course model status")}{settings ? " · OpenMAIC" : ""}</h2>
      <p role="status" className="text-[var(--muted-foreground)]">{t(description)}</p>
      {settings && status?.model ? <p className="break-all">{status.provider} · {status.model}</p> : null}
      {settings && status?.last_synced_at ? (
        <p className="text-[var(--muted-foreground)]">
          {t("Last synchronized")}: {new Date(status.last_synced_at).toLocaleString(i18n.language)}
        </p>
      ) : null}
      {settings ? <p className="text-[var(--muted-foreground)]">{t("This status uses saved settings. Synchronization does not test the provider connection or generate a course.")}</p> : null}
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" disabled={loading} onClick={() => void refresh()}
          className="rounded-lg border border-[var(--border)] px-3 py-1.5 disabled:opacity-50">{t("Refresh status")}</button>
        {settings && status?.can_configure && status.state !== "not_enabled" ? (
          <button type="button" disabled={loading} onClick={() => void refresh(true)}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 disabled:opacity-50">{t("Synchronize saved model")}</button>
        ) : null}
        {!settings && status?.can_configure && !status.ready ? (
          <Link href="/settings/llm" target="_blank" rel="noopener noreferrer" className="font-medium text-[var(--primary)]">{t("Open model settings")}</Link>
        ) : null}
        {!settings && status && !status.can_configure && !status.ready ? (
          <span>{t("Ask an administrator to configure the course model.")}</span>
        ) : null}
      </div>
    </section>
  );
}

export function CourseModelStatusCard() {
  const model = useCourseModelStatus();
  return <CourseModelStatusNotice model={model} settings />;
}
