"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslation } from "react-i18next";

const NAV = [
  ["/teaching/classrooms", "teaching.nav.classrooms"],
  ["/teaching/classrooms/new", "teaching.nav.new"],
  ["/teaching/reviews", "teaching.nav.reviews"],
  ["/teaching/library", "teaching.nav.library"],
  ["/teaching/batches", "teaching.nav.batches"],
] as const;

export default function TeachingLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const pathname = usePathname();
  const { t } = useTranslation();
  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden bg-[var(--background)] text-[var(--foreground)]">
      <header className="shrink-0 border-b border-[var(--border)] bg-[var(--background)]/95 px-5 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-[1500px] flex-wrap items-center justify-between gap-3">
          <Link href="/teaching/classrooms" className="font-semibold tracking-tight">
            {t("teaching.title")}
          </Link>
          <nav className="flex max-w-full gap-1 overflow-x-auto" aria-label={t("teaching.title")}>
            {NAV.map(([href, label]) => {
              const active =
                href === "/teaching/classrooms"
                  ? pathname === href || /^\/teaching\/classrooms\/[^/]+\/(?:outline|edit)$/.test(pathname)
                  : pathname === href;
              return (
                <Link
                  key={href}
                  href={href}
                  className={`whitespace-nowrap rounded-lg px-3 py-2 text-sm transition-colors ${
                    active
                      ? "bg-[var(--accent)] font-semibold"
                      : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]/60 hover:text-[var(--foreground)]"
                  }`}
                >
                  {t(label)}
                </Link>
              );
            })}
          </nav>
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
        <div className="mx-auto w-full max-w-[1500px] px-5 py-6">{children}</div>
      </div>
    </div>
  );
}
