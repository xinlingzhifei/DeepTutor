"use client";

import { useEffect, useState } from "react";
import { Check, LoaderCircle, ShieldAlert, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ValidationReport } from "@/components/teaching/ValidationReport";
import {
  decideTeachingReview,
  getTeachingReviewDetail,
  TeachingApiError,
  type TeachingReview,
  type TeachingReviewDetail,
} from "@/lib/teaching-api";
import {
  canDecideTeachingReview,
  reviewEvidenceMatches,
} from "@/lib/teaching-workflow";

export function ReviewQueue({
  reviews,
  currentUserId,
  onRefresh,
}: {
  reviews: TeachingReview[];
  currentUserId: string | null;
  onRefresh: () => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState(reviews[0]?.id ?? "");
  const [detail, setDetail] = useState<TeachingReviewDetail | null>(null);
  const [comment, setComment] = useState("");
  const [loading, setLoading] = useState(false);
  const [deciding, setDeciding] = useState<"approve" | "reject" | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const selected = reviews.find(review => review.id === selectedId) ?? reviews[0];
  const selectedReviewId = selected?.id ?? null;

  useEffect(() => {
    if (selected && selected.id !== selectedId) setSelectedId(selected.id);
  }, [selected, selectedId]);

  useEffect(() => {
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setComment("");
    if (!selected || !selectedReviewId) {
      setLoading(false);
      return;
    }
    let active = true;
    setLoading(true);
    getTeachingReviewDetail(selectedReviewId)
      .then(next => {
        if (!reviewEvidenceMatches(selected, next)) {
          throw new TeachingApiError(
            "Review evidence does not match the selected submission",
          );
        }
        if (active) setDetail(next);
      })
      .catch(reason => {
        if (active) {
          setDetailError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selected, selectedReviewId]);

  const chooseReview = (reviewId: string) => {
    if (reviewId === selectedReviewId) return;
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setComment("");
    setSelectedId(reviewId);
  };

  const mappings = detail?.document.knowledgePointMappings ?? [];
  const selfReview = Boolean(
    selected && currentUserId && selected.submittedBy === currentUserId,
  );
  const canDecide = canDecideTeachingReview({
    review: selected,
    detail,
    currentUserId,
    loading,
    error: detailError,
  });

  const act = async (decision: "approve" | "reject") => {
    if (!selected || !canDecide || !comment.trim()) return;
    setDeciding(decision);
    setActionError(null);
    try {
      await decideTeachingReview(selected.id, decision, comment.trim());
      setComment("");
      setDetail(null);
      await onRefresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDeciding(null);
    }
  };

  if (!reviews.length) {
    return (
      <div className="rounded-2xl border border-dashed border-[var(--border)] p-8 text-center text-sm text-[var(--muted-foreground)]">
        {t("teaching.reviews.empty")}
      </div>
    );
  }

  return (
    <div className="grid min-h-[36rem] gap-4 xl:grid-cols-[18rem_minmax(0,1fr)]">
      <aside className="space-y-2 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-3">
        {reviews.map(review => (
          <button
            key={review.id}
            type="button"
            onClick={() => chooseReview(review.id)}
            className={`w-full rounded-xl border p-3 text-left text-sm transition-colors ${
              selected?.id === review.id
                ? "border-[var(--primary)] bg-[var(--primary)]/5"
                : "border-transparent hover:bg-[var(--muted)]/50"
            }`}
          >
            <span className="block font-semibold">{review.assetId}</span>
            <span className="mt-1 block text-xs text-[var(--muted-foreground)]">
              {t("teaching.reviews.submittedBy", { user: review.submittedBy })}
            </span>
            {review.submittedBy === currentUserId ? (
              <span className="mt-2 inline-flex rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                {t("teaching.reviews.ownSubmission")}
              </span>
            ) : null}
          </button>
        ))}
      </aside>

      {selected ? (
        <main className="space-y-4">
          <header className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-xl font-semibold">
                  {detail?.title ?? selected.assetId}
                </h2>
                <p className="mt-1 text-sm text-[var(--muted-foreground)]">
                  {t("teaching.reviews.scope", { scope: selected.scope })}
                </p>
              </div>
              <span className="rounded-full bg-[var(--muted)] px-3 py-1 text-xs font-semibold">
                {selected.status}
              </span>
            </div>
            <div className="mt-4 grid gap-3 text-xs sm:grid-cols-3">
              <div>
                <p className="text-[var(--muted-foreground)]">
                  {t("teaching.reviews.submittedRevision")}
                </p>
                <p className="font-mono">{selected.draftRevision}</p>
              </div>
              <div>
                <p className="text-[var(--muted-foreground)]">
                  {t("teaching.reviews.course")}
                </p>
                <p className="font-mono">{detail?.courseId ?? "-"}</p>
              </div>
              <div>
                <p className="text-[var(--muted-foreground)]">
                  {t("teaching.reviews.targetClass")}
                </p>
                <p className="font-mono">{detail?.targetClassId ?? "-"}</p>
              </div>
            </div>
            <dl className="mt-4 grid gap-2 text-xs lg:grid-cols-2">
              <div>
                <dt className="text-[var(--muted-foreground)]">
                  {t("teaching.reviews.documentHash")}
                </dt>
                <dd className="break-all font-mono">{selected.documentSha256}</dd>
              </div>
              <div>
                <dt className="text-[var(--muted-foreground)]">
                  {t("teaching.reviews.validationHash")}
                </dt>
                <dd className="break-all font-mono">
                  {selected.validationReportSha256}
                </dd>
              </div>
            </dl>
          </header>

          {loading ? (
            <div className="flex items-center gap-2 p-5 text-sm text-[var(--muted-foreground)]">
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
              {t("teaching.common.loading")}
            </div>
          ) : detail ? (
            <>
              <div className="grid gap-4 lg:grid-cols-2">
                <section className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
                  <h3 className="font-semibold">
                    {t("teaching.reviews.sourceEvidence")}
                  </h3>
                  {detail.sourceFragments.length ? (
                    <ul className="mt-3 space-y-2 text-sm">
                      {detail.sourceFragments.map(fragment => (
                        <li
                          key={`${fragment.sourceId}:${fragment.fragmentId}`}
                          className="rounded-lg bg-[var(--muted)]/35 p-3"
                        >
                          <p className="whitespace-pre-wrap leading-6">{fragment.text}</p>
                          <span className="mt-2 block break-all font-mono text-xs text-[var(--muted-foreground)]">
                            {fragment.sourceId} / {fragment.fragmentId}
                          </span>
                          <span className="mt-1 block break-all font-mono text-[11px] text-[var(--muted-foreground)]">
                            {fragment.contentSha256}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-3 text-sm text-[var(--muted-foreground)]">
                      {t("teaching.reviews.noSourceEvidence")}
                    </p>
                  )}
                </section>
                <section className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
                  <h3 className="font-semibold">
                    {t("teaching.reviews.knowledgeCoverage")}
                  </h3>
                  <ul className="mt-3 space-y-2 text-sm">
                    {mappings.map(mapping => (
                      <li
                        key={mapping.knowledgePointId}
                        className="rounded-lg bg-[var(--muted)]/35 p-3"
                      >
                        <span className="font-medium">{mapping.knowledgePointId}</span>
                        <span className="mt-1 block text-xs text-[var(--muted-foreground)]">
                          {t("teaching.reviews.sceneCount", {
                            count: mapping.sceneIds.length,
                          })}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              </div>

              <section className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
                <h3 className="font-semibold">{t("teaching.reviews.versionDiff")}</h3>
                {detail?.baseline ? (
                  <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2">
                    <div>
                      <dt className="text-[var(--muted-foreground)]">
                        {t("teaching.reviews.baselineVersion")}
                      </dt>
                      <dd className="font-mono">
                        {detail.baseline.versionNumber} / {detail.baseline.versionId}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[var(--muted-foreground)]">
                        {t("teaching.reviews.baselineHash")}
                      </dt>
                      <dd className="break-all font-mono">
                        {detail.baseline.documentSha256}
                      </dd>
                    </div>
                  </dl>
                ) : (
                  <p className="mt-3 text-sm text-[var(--muted-foreground)]">
                    {t("teaching.reviews.noBaseline")}
                  </p>
                )}
                <ul className="mt-3 space-y-1 text-xs">
                  {detail?.changedPaths.map(path => (
                    <li key={path} className="break-all font-mono">
                      {path}
                    </li>
                  ))}
                </ul>
              </section>

              <section className="rounded-2xl border border-[var(--border)] bg-[var(--background)] p-5">
                <ValidationReport report={detail.validationReport} />
              </section>
            </>
          ) : null}

          {selected.warnings.length ? (
            <section className="rounded-2xl border border-amber-500/30 bg-amber-500/5 p-5">
              <h3 className="font-semibold">
                {t("teaching.reviews.submissionWarnings")}
              </h3>
              <pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">
                {JSON.stringify(selected.warnings, null, 2)}
              </pre>
            </section>
          ) : null}

          {selfReview || !currentUserId ? (
            <div className="flex items-start gap-3 rounded-2xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
              <ShieldAlert
                className="mt-0.5 h-4 w-4 shrink-0 text-amber-600"
                aria-hidden
              />
              <p>
                {selfReview
                  ? t("teaching.reviews.selfReviewBlocked")
                  : t("teaching.reviews.identityRequired")}
              </p>
            </div>
          ) : null}

          {canDecide ? (
            <section className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
              <label className="space-y-2 text-sm font-medium">
                {t("teaching.reviews.comment")}
                <textarea
                  value={comment}
                  onChange={event => setComment(event.target.value)}
                  rows={3}
                  maxLength={4000}
                  disabled={deciding !== null}
                  className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 disabled:opacity-50"
                />
              </label>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void act("approve")}
                  disabled={!comment.trim() || deciding !== null}
                  className="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                >
                  {deciding === "approve" ? (
                    <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
                  ) : (
                    <Check className="h-4 w-4" aria-hidden />
                  )}
                  {t("teaching.reviews.approve")}
                </button>
                <button
                  type="button"
                  onClick={() => void act("reject")}
                  disabled={!comment.trim() || deciding !== null}
                  className="inline-flex items-center gap-2 rounded-lg bg-[var(--destructive)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                >
                  {deciding === "reject" ? (
                    <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
                  ) : (
                    <X className="h-4 w-4" aria-hidden />
                  )}
                  {t("teaching.reviews.reject")}
                </button>
              </div>
            </section>
          ) : null}

          {detailError || actionError ? (
            <p role="alert" className="text-sm text-[var(--destructive)]">
              {detailError ?? actionError}
            </p>
          ) : null}
        </main>
      ) : null}
    </div>
  );
}
