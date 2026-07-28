"use client";

import { Building2, ChevronDown, LoaderCircle, RotateCw } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { useTenant } from "@/context/TenantContext";

export type TenantSwitcherMode = "hidden" | "label" | "action" | "select";

export function getTenantSwitcherMode(
  tenants: readonly { tenant_id: string }[],
  activeTenantId: string | null,
): TenantSwitcherMode {
  if (tenants.length === 0) return "hidden";
  if (tenants.length === 1) {
    return tenants[0].tenant_id === activeTenantId ? "label" : "action";
  }
  return "select";
}

export function TenantSwitcher({ collapsed = false }: { collapsed?: boolean }) {
  const { t } = useTranslation();
  const selectId = useId();
  const {
    tenants,
    activeTenantId,
    activeTenant,
    loading,
    loadError,
    switching,
    error,
    switchTenant,
    retrySwitch,
    retryStatus,
  } = useTenant();
  const mode = getTenantSwitcherMode(tenants, activeTenantId);
  const errorText = error ? t(error, { defaultValue: error }) : null;
  const loadErrorText = loadError
    ? t(loadError, { defaultValue: loadError })
    : null;
  const retryingLoad = !errorText && Boolean(loadErrorText);
  const feedbackError = errorText ?? loadErrorText;
  const retryLabel = t(
    retryingLoad ? "Retry loading organizations" : "Retry",
  ) as string;
  const errorFeedback = feedbackError ? (
    <div className={collapsed ? "mt-1 flex justify-center" : "mt-1.5"}>
      <p
        role="alert"
        className={
          collapsed
            ? "sr-only"
            : "text-[11px] leading-4 text-red-600 dark:text-red-400"
        }
      >
        {feedbackError}
      </p>
      <button
        type="button"
        onClick={() => void (retryingLoad ? retryStatus() : retrySwitch())}
        disabled={retryingLoad ? loading : switching}
        title={retryLabel}
        aria-label={retryLabel}
        className={
          collapsed
            ? "flex h-7 w-7 items-center justify-center rounded-md text-red-600 hover:bg-[var(--background)]/60 disabled:opacity-50 dark:text-red-400"
            : "mt-1 text-[11px] font-medium text-red-600 underline-offset-2 hover:underline disabled:opacity-50 dark:text-red-400"
        }
      >
        {collapsed ? <RotateCw size={13} /> : t("Retry")}
      </button>
    </div>
  ) : null;

  if (mode === "hidden") {
    if (!errorFeedback) return null;
    return (
      <div className={collapsed ? "mb-2 w-full" : "mb-2 w-full px-3"}>
        {errorFeedback}
      </div>
    );
  }

  const currentTenant = activeTenant ?? tenants[0];
  const currentTitle = `${t("Current organization")}: ${currentTenant.name}`;

  const feedback = (
    <>
      {switching && (
        <p
          aria-live="polite"
          className={
            collapsed
              ? "sr-only"
              : "mt-1 text-[11px] text-[var(--muted-foreground)]"
          }
        >
          {t("Switching organization…")}
        </p>
      )}
      {errorFeedback}
    </>
  );

  if (mode === "label") {
    return (
      <div className={collapsed ? "mb-2 w-full" : "mb-2 w-full px-3"}>
        <div
          title={currentTitle}
          aria-label={currentTitle}
          className={
            collapsed
              ? "mx-auto flex h-9 w-9 items-center justify-center rounded-xl bg-[var(--background)]/55 text-[var(--muted-foreground)]"
              : "flex min-w-0 items-center gap-2 rounded-lg bg-[var(--background)]/45 px-2.5 py-2 text-[12px] text-[var(--foreground)]/85"
          }
        >
          <Building2 size={collapsed ? 16 : 14} className="shrink-0" />
          <span className={collapsed ? "sr-only" : "truncate"}>
            {currentTenant.name}
          </span>
        </div>
        {feedback}
      </div>
    );
  }

  if (mode === "action") {
    const actionTitle = `${t("Switch organization")}: ${currentTenant.name}`;
    const ActionIcon = switching ? LoaderCircle : Building2;
    return (
      <div className={collapsed ? "mb-2 w-full" : "mb-2 w-full px-3"}>
        <button
          type="button"
          onClick={() => void switchTenant(currentTenant.tenant_id)}
          disabled={loading || switching}
          title={actionTitle}
          aria-label={actionTitle}
          aria-busy={switching}
          className={
            collapsed
              ? "mx-auto flex h-9 w-9 items-center justify-center rounded-xl bg-[var(--background)]/55 text-[var(--muted-foreground)] hover:bg-[var(--background)]/75 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] disabled:cursor-wait disabled:opacity-60"
              : "flex w-full min-w-0 items-center gap-2 rounded-lg bg-[var(--background)]/55 px-2.5 py-2 text-left text-[12px] text-[var(--foreground)] hover:bg-[var(--background)]/75 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] disabled:cursor-wait disabled:opacity-60"
          }
        >
          <ActionIcon
            size={collapsed ? 16 : 14}
            className={`shrink-0 ${switching ? "animate-spin" : ""}`}
          />
          <span className={collapsed ? "sr-only" : "truncate"}>
            {currentTenant.name}
          </span>
        </button>
        {feedback}
      </div>
    );
  }

  if (collapsed) {
    return (
      <div className="mb-2 w-full">
        <div
          className="relative mx-auto h-9 w-9 rounded-xl bg-[var(--background)]/55 text-[var(--muted-foreground)] focus-within:ring-2 focus-within:ring-[var(--primary)]"
          title={currentTitle}
        >
          <select
            id={selectId}
            value={activeTenantId ?? ""}
            onChange={event => void switchTenant(event.target.value)}
            disabled={loading || switching}
            title={currentTitle}
            aria-label={`${t("Switch organization")}: ${currentTenant.name}`}
            aria-busy={switching}
            className="absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0 disabled:cursor-wait"
          >
            <option value="" disabled>
              {t("Select organization")}
            </option>
            {tenants.map(tenant => (
              <option key={tenant.tenant_id} value={tenant.tenant_id}>
                {tenant.name}
              </option>
            ))}
          </select>
          <div
            aria-hidden
            className="flex h-full w-full items-center justify-center"
          >
            {switching ? (
              <LoaderCircle size={16} className="animate-spin" />
            ) : (
              <Building2 size={16} />
            )}
            <ChevronDown
              size={9}
              className="absolute bottom-1 right-1 opacity-70"
            />
          </div>
        </div>
        {feedback}
      </div>
    );
  }

  return (
    <div className="mb-2 w-full px-3">
      <label
        htmlFor={selectId}
        className="mb-1 block px-1 text-[10px] font-medium uppercase tracking-wide text-[var(--muted-foreground)]/75"
      >
        {t("Current organization")}
      </label>
      <div className="relative">
        <select
          id={selectId}
          value={activeTenantId ?? ""}
          onChange={event => void switchTenant(event.target.value)}
          disabled={loading || switching}
          aria-busy={switching}
          className="h-9 w-full appearance-none rounded-lg border border-[var(--border)]/60 bg-[var(--background)]/55 py-1.5 pl-2.5 pr-8 text-[12px] text-[var(--foreground)] outline-none transition-colors hover:bg-[var(--background)]/75 focus:border-[var(--primary)] disabled:cursor-wait disabled:opacity-60"
        >
          <option value="" disabled>
            {t("Select organization")}
          </option>
          {tenants.map(tenant => (
            <option key={tenant.tenant_id} value={tenant.tenant_id}>
              {tenant.name}
            </option>
          ))}
        </select>
        {switching ? (
          <LoaderCircle
            size={14}
            aria-hidden
            className="pointer-events-none absolute right-2.5 top-2.5 animate-spin text-[var(--muted-foreground)]"
          />
        ) : (
          <ChevronDown
            size={14}
            aria-hidden
            className="pointer-events-none absolute right-2.5 top-2.5 text-[var(--muted-foreground)]"
          />
        )}
      </div>
      {feedback}
    </div>
  );
}
