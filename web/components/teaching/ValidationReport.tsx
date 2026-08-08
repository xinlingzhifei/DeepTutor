"use client";

import { AlertTriangle, CheckCircle2, CircleX } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { TeachingValidationReport } from "@/lib/teaching-api";

const SECTION_NAMES = [
  "dsl_integrity",
  "media_integrity",
  "knowledge_point_coverage",
  "source_traceability",
  "unsupported_claims",
  "quiz_answerability",
  "interactive_security",
  "accessibility",
  "export_readiness",
] as const;

interface SectionIssue {
  severity: string;
  code: string;
  message: string;
  path: string;
}

function reportSection(
  report: TeachingValidationReport,
  name: string,
): { status: string; issues: SectionIssue[] } {
  const sections = report.sections;
  if (!sections || typeof sections !== "object" || Array.isArray(sections)) {
    return { status: "unknown", issues: [] };
  }
  const raw = (sections as Record<string, unknown>)[name];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { status: "unknown", issues: [] };
  }
  const section = raw as Record<string, unknown>;
  const status = typeof section.status === "string" ? section.status : "unknown";
  const issues = Array.isArray(section.issues)
    ? section.issues.flatMap(issue => {
        if (!issue || typeof issue !== "object" || Array.isArray(issue)) return [];
        const value = issue as Record<string, unknown>;
        return [
          {
            severity: typeof value.severity === "string" ? value.severity : "warning",
            code: typeof value.code === "string" ? value.code : "unknown",
            message: typeof value.message === "string" ? value.message : "",
            path: typeof value.path === "string" ? value.path : "$",
          },
        ];
      })
    : [];
  return { status, issues };
}

export function ValidationReport({
  report,
  compact = false,
}: {
  report: TeachingValidationReport | null;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  if (!report) {
    return (
      <p className="rounded-xl border border-dashed border-[var(--border)] p-4 text-sm text-[var(--muted-foreground)]">
        {t("teaching.validation.notRun")}
      </p>
    );
  }

  return (
    <section className="space-y-3" aria-label={t("teaching.validation.title")}>
      {!compact ? (
        <div className="flex items-center justify-between gap-3">
          <h2 className="font-semibold">{t("teaching.validation.title")}</h2>
          <span
            className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
              report.valid
                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                : "bg-[var(--destructive)]/10 text-[var(--destructive)]"
            }`}
          >
            {report.valid
              ? t("teaching.validation.ready")
              : t("teaching.validation.blocked")}
          </span>
        </div>
      ) : null}
      <div className="grid gap-2 sm:grid-cols-2">
        {SECTION_NAMES.map(name => {
          const section = reportSection(report, name);
          const failing = section.status === "error";
          const warning = section.status === "warning" || section.issues.some(issue => issue.severity === "warning");
          const Icon = failing ? CircleX : warning ? AlertTriangle : CheckCircle2;
          return (
            <details
              key={name}
              open={failing}
              className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-3"
            >
              <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium">
                <Icon
                  className={`h-4 w-4 ${
                    failing
                      ? "text-[var(--destructive)]"
                      : warning
                        ? "text-amber-600"
                        : "text-emerald-600"
                  }`}
                  aria-hidden
                />
                {t(`teaching.validation.section.${name}`)}
              </summary>
              {section.issues.length ? (
                <ul className="mt-2 space-y-2">
                  {section.issues.map((issue, index) => (
                    <li key={`${issue.code}-${issue.path}-${index}`} className="text-xs leading-5">
                      <span className="font-semibold">{issue.code}</span>: {issue.message}
                      <code className="ml-1 text-[var(--muted-foreground)]">{issue.path}</code>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-xs text-[var(--muted-foreground)]">
                  {t("teaching.validation.noIssues")}
                </p>
              )}
            </details>
          );
        })}
      </div>
    </section>
  );
}
