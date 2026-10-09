"use client";

import { Suspense, useState, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { login, fetchAuthStatus, checkIsFirstUser } from "@/lib/auth";

function LoginPageContent() {
  const { t } = useTranslation();
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = searchParams.get("next") ?? "/";

  const registered = searchParams.get("registered") === "1";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // If already authenticated, skip login
    fetchAuthStatus().then((status) => {
      if (status?.authenticated) {
        router.replace(next);
        return;
      }
      // No users registered yet — send straight to the registration page
      checkIsFirstUser().then((first) => {
        if (first) router.replace("/register");
      });
    });
  }, [router, next]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    const result = await login(username, password);

    if (result.ok) {
      router.replace(next);
    } else {
      setError(result.error ?? t("Login failed"));
      setLoading(false);
    }
  }

  const learningPillars = [
    t("Ask with context"),
    t("Create with structure"),
    t("Remember what matters"),
  ];

  return (
    <div className="grid min-h-dvh w-full bg-[var(--background)] text-[var(--foreground)] lg:grid-cols-[minmax(0,1.08fr)_minmax(440px,0.92fr)]">
      <section className="relative overflow-hidden border-b border-[var(--border)] bg-[var(--secondary)] lg:border-r lg:border-b-0">
        <div
          aria-hidden="true"
          className="absolute inset-0 opacity-[0.35]"
          style={{
            backgroundImage:
              "linear-gradient(to right, var(--border) 1px, transparent 1px), linear-gradient(to bottom, var(--border) 1px, transparent 1px)",
            backgroundSize: "48px 48px",
          }}
        />
        <div
          aria-hidden="true"
          className="absolute top-0 right-0 h-40 w-40 border-b border-l border-[var(--border)] bg-[var(--card)]/45 lg:h-56 lg:w-56"
        />

        <div className="relative z-10 flex min-h-[235px] flex-col px-6 py-6 sm:px-10 sm:py-8 lg:min-h-dvh lg:px-14 lg:py-12 xl:px-20 xl:py-16">
          <div className="flex items-center gap-3">
            <span
              aria-hidden="true"
              className="grid size-9 place-items-center rounded-[10px] border border-[var(--border)] bg-[var(--card)] font-serif text-base font-semibold text-[var(--primary)] shadow-sm"
            >
              Y
            </span>
            <div>
              <p className="font-serif text-lg font-semibold tracking-[-0.02em]">
                yFeiSTAI
              </p>
              <p className="text-[10px] font-semibold tracking-[0.18em] text-[var(--muted-foreground)] uppercase">
                {t("A thoughtful place to learn")}
              </p>
            </div>
          </div>

          <div className="mt-9 max-w-2xl lg:my-auto lg:mt-16">
            <div className="mb-5 h-px w-14 bg-[var(--primary)]" />
            <h1 className="max-w-[12ch] font-serif text-[2.15rem] leading-[1.08] font-medium tracking-[-0.045em] text-balance sm:text-[2.75rem] lg:text-[clamp(3rem,4.3vw,4.9rem)]">
              {t("Turn every question into a path you can follow.")}
            </h1>
            <p className="mt-5 hidden max-w-xl text-[15px] leading-7 text-[var(--muted-foreground)] sm:block lg:text-base">
              {t(
                "Explore ideas, build structured courses, and return to the knowledge that matters.",
              )}
            </p>
          </div>

          <div className="hidden lg:block">
            <div className="grid grid-cols-3 border-y border-[var(--border)]">
              {learningPillars.map((pillar, index) => (
                <div
                  key={pillar}
                  className={`py-5 ${index > 0 ? "border-l border-[var(--border)] pl-5" : "pr-5"}`}
                >
                  <p className="font-serif text-xs text-[var(--primary)]">
                    0{index + 1}
                  </p>
                  <p className="mt-2 text-[13px] font-medium">{pillar}</p>
                </div>
              ))}
            </div>
            <p className="mt-5 text-xs text-[var(--muted-foreground)]">
              {t("Built for focused, self-directed learning.")}
            </p>
          </div>
        </div>
      </section>

      <main className="relative flex min-h-[530px] items-center justify-center px-5 py-9 sm:px-10 sm:py-12 lg:min-h-dvh lg:px-12 xl:px-20">
        <div className="w-full max-w-[440px]">
          <p className="text-[11px] font-semibold tracking-[0.18em] text-[var(--primary)] uppercase">
            {t("Your learning space")}
          </p>
          <h2 className="mt-3 font-serif text-3xl font-medium tracking-[-0.035em] sm:text-[2rem]">
            {t("Welcome back")}
          </h2>
          <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">
            {t("Sign in to continue your learning.")}
          </p>

          {registered && (
            <div
              role="status"
              className="mt-5 rounded-xl border border-green-500/30 bg-green-500/10 px-4 py-3 text-sm text-green-700 dark:text-green-400"
            >
              {t("Account created! Sign in to continue.")}
            </div>
          )}

          <div className="surface-card mt-6 px-5 py-6 shadow-[0_24px_70px_-48px_rgba(28,24,22,0.8)] sm:px-8 sm:py-8">
            <form onSubmit={handleSubmit} className="space-y-5">
              <div>
                <label
                  htmlFor="username"
                  className="mb-2 block text-[13px] font-medium text-[var(--foreground)]"
                >
                  {t("Email or username")}
                </label>
                <input
                  id="username"
                  type="text"
                  autoComplete="username"
                  required
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="h-12 w-full rounded-xl border border-[var(--input)] bg-[var(--background)] px-4 text-sm text-[var(--foreground)] outline-none transition-[border-color,box-shadow,background-color] placeholder:text-[var(--muted-foreground)] focus:border-[var(--primary)] focus:ring-3 focus:ring-[var(--primary)]/15"
                  placeholder={t("you@example.com")}
                />
              </div>

              <div>
                <label
                  htmlFor="password"
                  className="mb-2 block text-[13px] font-medium text-[var(--foreground)]"
                >
                  {t("Password")}
                </label>
                <input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="h-12 w-full rounded-xl border border-[var(--input)] bg-[var(--background)] px-4 text-sm text-[var(--foreground)] outline-none transition-[border-color,box-shadow,background-color] placeholder:text-[var(--muted-foreground)] focus:border-[var(--primary)] focus:ring-3 focus:ring-[var(--primary)]/15"
                  placeholder="••••••••"
                />
              </div>

              {error && (
                <p
                  role="alert"
                  className="rounded-xl border border-red-500/20 bg-red-500/10 px-3.5 py-2.5 text-sm text-red-600 dark:text-red-400"
                >
                  {error}
                </p>
              )}

              <button
                type="submit"
                disabled={loading}
                className="btn-primary flex h-12 w-full items-center justify-between rounded-xl bg-[var(--primary)] px-4 text-sm font-semibold text-[var(--primary-foreground)] transition-[opacity,transform,box-shadow] hover:-translate-y-px hover:opacity-95 hover:shadow-lg active:translate-y-0 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-none"
              >
                <span>{loading ? t("Signing in…") : t("Sign in")}</span>
                {!loading && (
                  <span aria-hidden="true" className="text-lg leading-none">
                    →
                  </span>
                )}
              </button>
            </form>

            <div className="mt-6 border-t border-[var(--border)] pt-5 text-center text-sm text-[var(--muted-foreground)]">
              {t("Don't have an account?")}{" "}
              <Link
                href="/register"
                className="font-semibold text-[var(--primary)] underline-offset-4 hover:underline"
              >
                {t("Create one")}
              </Link>
            </div>
          </div>

          <p className="mt-5 text-center text-xs leading-5 text-[var(--muted-foreground)]">
            {t("A quieter place to think, create, and learn.")}
          </p>
        </div>
      </main>
    </div>
  );
}

function LoginFallback() {
  const { t } = useTranslation();
  return (
    <div className="flex min-h-dvh w-full items-center justify-center bg-[var(--background)] px-5">
      <div className="text-center text-sm text-[var(--muted-foreground)]">
        {t("Loading sign in...")}
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginFallback />}>
      <LoginPageContent />
    </Suspense>
  );
}
