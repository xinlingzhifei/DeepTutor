"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { GraduationCap } from "lucide-react";
import { useTranslation } from "react-i18next";
import { SidebarShell } from "@/components/sidebar/SidebarShell";
import { LogoutButton } from "@/components/auth/LogoutButton";
import { AdminLink } from "@/components/auth/AdminLink";
import { ProfileLink } from "@/components/auth/ProfileLink";
import { useAppShell } from "@/context/AppShellContext";
import {
  deleteSession,
  listSessions,
  updateSessionTitle,
  type SessionSummary,
} from "@/lib/session-api";

export default function UtilitySidebar() {
  const { t } = useTranslation();
  const router = useRouter();
  const pathname = usePathname();
  const { activeSessionId, setActiveSessionId } = useAppShell();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const hasLoadedSessionsRef = useRef(false);

  const refreshSessions = useCallback(async () => {
    if (!hasLoadedSessionsRef.current) {
      setLoadingSessions(true);
    }
    try {
      setSessions(await listSessions(50, 0, { force: true }));
      hasLoadedSessionsRef.current = true;
    } catch (error) {
      console.error("Failed to load sessions", error);
    } finally {
      setLoadingSessions(false);
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  const handleSelectSession = useCallback(
    async (sessionId: string) => {
      setActiveSessionId(sessionId);
      router.push(`/home/${sessionId}`);
    },
    [router, setActiveSessionId],
  );

  const handleRenameSession = useCallback(
    async (sessionId: string, title: string) => {
      const updated = await updateSessionTitle(sessionId, title);
      setSessions((prev) =>
        prev.map((session) =>
          session.session_id === sessionId
            ? {
                ...session,
                title: updated.title,
                updated_at: updated.updated_at,
              }
            : session,
        ),
      );
    },
    [],
  );

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      if (!window.confirm(t("Delete this chat history?"))) return;
      await deleteSession(sessionId);
      setSessions((prev) =>
        prev.filter((session) => session.session_id !== sessionId),
      );
      if (activeSessionId === sessionId) {
        setActiveSessionId(null);
      }
    },
    [activeSessionId, setActiveSessionId, t],
  );

  return (
    <SidebarShell
      showSessions
      sessions={sessions}
      activeSessionId={activeSessionId}
      loadingSessions={loadingSessions}
      onNewChat={() => setActiveSessionId(null)}
      onSelectSession={handleSelectSession}
      onRenameSession={handleRenameSession}
      onDeleteSession={handleDeleteSession}
      footerSlot={(collapsed) => (
        <>
          <Link
            href="/teaching/classrooms"
            aria-label={t("Teaching workspace")}
            title={t("Teaching workspace")}
            className={`flex items-center rounded-lg transition-colors ${
              collapsed ? "h-9 w-9 justify-center" : "gap-2.5 px-3 py-2 text-[13.5px]"
            } ${
              pathname.startsWith("/teaching")
                ? "bg-[var(--accent)] font-medium text-[var(--foreground)]"
                : "text-[var(--foreground)]/85 hover:bg-[var(--background)]/60 hover:text-[var(--foreground)]"
            }`}
          >
            <GraduationCap size={collapsed ? 18 : 16} strokeWidth={1.7} />
            {!collapsed ? <span>{t("Teaching workspace")}</span> : null}
          </Link>
          <ProfileLink collapsed={collapsed} />
          <AdminLink collapsed={collapsed} />
          <LogoutButton collapsed={collapsed} />
        </>
      )}
    />
  );
}
