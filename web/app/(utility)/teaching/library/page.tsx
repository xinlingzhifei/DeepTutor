"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, Send } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ClassroomExportMenu } from "@/components/classroom/ClassroomExportMenu";
import {
  listTeachingPublications,
  publishTeachingClassroom,
  type TeachingPublicationList,
} from "@/lib/teaching-api";
import { createTeachingPublicationAttemptRegistry } from "@/lib/teaching-workflow";

const EMPTY_LIBRARY: TeachingPublicationList = { items: [], candidates: [] };

export default function TeachingLibraryPage() {
  const { t } = useTranslation();
  const [library, setLibrary] = useState<TeachingPublicationList>(EMPTY_LIBRARY);
  const [loading, setLoading] = useState(true);
  const [publishingAssetId, setPublishingAssetId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [publicationAttempts] = useState(() =>
    createTeachingPublicationAttemptRegistry(),
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setLibrary(await listTeachingPublications());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const publish = async (assetId: string) => {
    if (publishingAssetId !== null) return;
    setPublishingAssetId(assetId);
    setError(null);
    try {
      await publishTeachingClassroom(
        assetId,
        publicationAttempts.keyFor(assetId),
      );
      setLibrary(current => ({
        ...current,
        candidates: current.candidates.filter(
          candidate => candidate.assetId !== assetId,
        ),
      }));
      publicationAttempts.settle(assetId);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPublishingAssetId(null);
    }
  };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("teaching.library.title")}
        </h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          {t("teaching.library.description")}
        </p>
      </header>

      {library.candidates.length ? (
        <section className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold">
              {t("teaching.library.candidates")}
            </h2>
            <p className="mt-1 text-sm text-[var(--muted-foreground)]">
              {t("teaching.library.candidatesDescription")}
            </p>
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            {library.candidates.map(candidate => (
              <article
                key={candidate.reviewId}
                className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5"
              >
                <h3 className="font-semibold">{candidate.title}</h3>
                <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                  {candidate.courseId} / {candidate.assetId}
                </p>
                <dl className="mt-3 space-y-2 text-xs">
                  <div>
                    <dt className="text-[var(--muted-foreground)]">
                      {t("teaching.library.approvedRevision")}
                    </dt>
                    <dd className="font-mono">{candidate.draftRevision}</dd>
                  </div>
                  <div>
                    <dt className="text-[var(--muted-foreground)]">
                      {t("teaching.library.documentHash")}
                    </dt>
                    <dd className="break-all font-mono">
                      {candidate.documentSha256}
                    </dd>
                  </div>
                </dl>
                <button
                  type="button"
                  onClick={() => void publish(candidate.assetId)}
                  disabled={publishingAssetId !== null}
                  className="mt-4 inline-flex items-center gap-2 rounded-lg bg-[var(--primary)] px-4 py-2 text-sm font-semibold text-[var(--primary-foreground)] disabled:opacity-50"
                >
                  {publishingAssetId === candidate.assetId ? (
                    <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
                  ) : (
                    <Send className="h-4 w-4" aria-hidden />
                  )}
                  {t("teaching.library.publishTenant")}
                </button>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">
          {t("teaching.library.published")}
        </h2>
        {loading && !library.items.length ? (
          <div className="flex items-center gap-2 p-6 text-sm text-[var(--muted-foreground)]">
            <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            {t("teaching.common.loading")}
          </div>
        ) : library.items.length ? (
          <div className="grid gap-4 lg:grid-cols-2">
            {library.items.map(publication => (
              <article
                key={publication.publicationId}
                className="space-y-4 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5"
              >
                <div>
                  <h3 className="font-semibold">{publication.title}</h3>
                  <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                    {publication.courseId} / {publication.assetId}
                  </p>
                  <p className="mt-2 break-all font-mono text-xs">
                    {t("teaching.library.version")}: {publication.versionNumber} / {publication.versionId}
                  </p>
                  <p className="mt-1 break-all font-mono text-[11px] text-[var(--muted-foreground)]">
                    {publication.documentSha256}
                  </p>
                </div>
                <ClassroomExportMenu
                  target={{ kind: "version", versionId: publication.versionId }}
                  policy={{ mp4Enabled: false }}
                />
              </article>
            ))}
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-[var(--border)] p-8 text-center text-sm text-[var(--muted-foreground)]">
            {t("teaching.library.empty")}
          </div>
        )}
      </section>

      {error ? (
        <p role="alert" className="text-sm text-[var(--destructive)]">
          {error}
        </p>
      ) : null}
    </div>
  );
}
