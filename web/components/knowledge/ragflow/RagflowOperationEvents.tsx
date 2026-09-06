"use client";

import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronRight, Loader2 } from "lucide-react";
import { ragflowApi, ragflowErrorMessage, type RagflowOperationEvent } from "@/lib/ragflow-api";

export function RagflowOperationEvents({
  kbRef,
  operationId,
}: {
  kbRef: string;
  operationId: string;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();
  return (
    <div className="mt-3 border-t border-[var(--border)] pt-3">
      <button
        type="button"
        className="inline-flex items-center gap-1 text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setExpanded((value) => !value)}
      >
        <ChevronRight size={13} className={expanded ? "rotate-90" : ""} />
        {t("ragflow.viewEvents", { defaultValue: "查看事件" })}
      </button>
      {expanded && (
        <div id={panelId} className="mt-3">
          <EventList key={`${kbRef}:${operationId}`} kbRef={kbRef} operationId={operationId} />
        </div>
      )}
    </div>
  );
}

function EventList({ kbRef, operationId }: { kbRef: string; operationId: string }) {
  const { t, i18n } = useTranslation();
  const [events, setEvents] = useState<RagflowOperationEvent[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    ragflowApi
      .getEvents(kbRef, operationId, { after_id: 0 }, { signal: controller.signal })
      .then((response) => {
        if (!controller.signal.aborted) setEvents(response.events);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(ragflowErrorMessage(failure, t));
      });
    return () => controller.abort();
  }, [kbRef, operationId, t]);

  if (error)
    return (
      <p role="alert" className="text-xs text-red-700 dark:text-red-300">
        {error}
      </p>
    );
  if (!events)
    return (
      <p role="status" className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
        <Loader2 size={12} className="animate-spin" />
        {t("Loading...")}
      </p>
    );
  if (!events.length)
    return (
      <p role="status" className="text-xs text-[var(--muted-foreground)]">
        {t("ragflow.noEvents", { defaultValue: "暂无事件记录" })}
      </p>
    );
  return (
    <ol className="space-y-2">
      {events.map((event) => {
        const date = new Date(event.created_at * 1000);
        return (
          <li
            key={event.id}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-[var(--muted)]/40 px-3 py-2 text-xs"
          >
            <code>{event.event_type}</code>
            <span>{t(`ragflow.state.${event.state}`, { defaultValue: event.state })}</span>
            <span className="font-mono text-[var(--muted-foreground)]">{`r${event.revision}`}</span>
            {Number.isFinite(date.getTime()) && (
              <time dateTime={date.toISOString()} className="ml-auto text-[var(--muted-foreground)]">
                {date.toLocaleString(i18n.resolvedLanguage ?? "zh-CN")}
              </time>
            )}
          </li>
        );
      })}
    </ol>
  );
}
