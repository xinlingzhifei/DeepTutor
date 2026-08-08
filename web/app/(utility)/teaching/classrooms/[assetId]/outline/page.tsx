"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { OutlineReview } from "@/components/teaching/OutlineReview";
import {
  getTeachingClassroom,
  type TeachingClassroom,
} from "@/lib/teaching-api";

export default function TeachingOutlinePage() {
  const { t } = useTranslation();
  const params = useParams<{ assetId: string }>();
  const assetId = params?.assetId;
  const [classroom, setClassroom] = useState<TeachingClassroom | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!assetId) return;
    let active = true;
    getTeachingClassroom(assetId)
      .then(next => {
        if (active) setClassroom(next);
      })
      .catch(reason => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      active = false;
    };
  }, [assetId]);

  if (error) return <p role="alert" className="text-sm text-[var(--destructive)]">{error}</p>;
  if (!classroom) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-[var(--muted-foreground)]">
        <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
        {t("teaching.common.loading")}
      </div>
    );
  }
  return <OutlineReview classroom={classroom} />;
}
