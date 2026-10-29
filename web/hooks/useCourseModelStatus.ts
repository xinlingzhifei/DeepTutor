"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { COURSE_MODEL_CHANGED, fetchCourseModelStatus, type CourseModelStatus } from "@/lib/course-model-status";

export function useCourseModelStatus(teaching = false) {
  const [status, setStatus] = useState<CourseModelStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const request = useRef(0);

  const refresh = useCallback(async (resync = false) => {
    const current = ++request.current;
    setLoading(true);
    try {
      const next = await fetchCourseModelStatus(teaching, resync);
      if (request.current === current) {
        setStatus(next);
        setError(false);
      }
      return request.current === current && next.ready;
    } catch {
      if (request.current === current) {
        setStatus(null);
        setError(true);
      }
      return false;
    } finally {
      if (request.current === current) setLoading(false);
    }
  }, [teaching]);

  useEffect(() => {
    const pending = request;
    const reload = () => { if (!document.hidden) void refresh(); };
    void refresh();
    window.addEventListener("focus", reload);
    window.addEventListener(COURSE_MODEL_CHANGED, reload);
    document.addEventListener("visibilitychange", reload);
    const timer = window.setInterval(reload, 30_000);
    return () => {
      pending.current++;
      window.clearInterval(timer);
      window.removeEventListener("focus", reload);
      window.removeEventListener(COURSE_MODEL_CHANGED, reload);
      document.removeEventListener("visibilitychange", reload);
    };
  }, [refresh]);

  return { status, loading, error, refresh, ready: status?.ready === true && !loading && !error };
}
