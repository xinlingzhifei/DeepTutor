"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ReviewQueue } from "@/components/teaching/ReviewQueue";
import { fetchAuthStatus } from "@/lib/auth";
import {
  listTeachingReviews,
  type TeachingReview,
} from "@/lib/teaching-api";

export default function TeachingReviewsPage() {
  const { t } = useTranslation();
  const [reviews, setReviews] = useState<TeachingReview[]>([]);
  const [currentUserId, setCurrentUserId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextReviews, auth] = await Promise.all([
        listTeachingReviews(),
        fetchAuthStatus(),
      ]);
      setReviews(nextReviews);
      setCurrentUserId(auth?.user_id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">{t("teaching.reviews.title")}</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          {t("teaching.reviews.description")}
        </p>
      </header>
      {loading && !reviews.length ? (
        <div className="flex items-center gap-2 p-6 text-sm text-[var(--muted-foreground)]">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          {t("teaching.common.loading")}
        </div>
      ) : (
        <ReviewQueue
          reviews={reviews}
          currentUserId={currentUserId}
          onRefresh={refresh}
        />
      )}
      {error ? <p role="alert" className="text-sm text-[var(--destructive)]">{error}</p> : null}
    </div>
  );
}
