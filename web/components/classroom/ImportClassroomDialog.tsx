"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { FileUp, LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import Modal from "@/components/common/Modal";
import { uploadDraftClassroomMedia } from "@/lib/classroom-api";
import {
  importPptxInBrowser,
  type ImportedSlides,
} from "@/lib/openmaic-adapter/importer";

interface ImportClassroomDialogProps {
  isOpen: boolean;
  assetId: string;
  onClose: () => void;
  onImported: (result: ImportedSlides) => void | Promise<void>;
}

export function ImportClassroomDialog({
  isOpen,
  assetId,
  onClose,
  onImported,
}: ImportClassroomDialogProps) {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const boundary = JSON.stringify([assetId, isOpen]);
  const activeBoundaryRef = useRef(boundary);

  useLayoutEffect(() => {
    activeBoundaryRef.current = boundary;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setImporting(false);
    setFile(null);
    setError(null);
    return () => {
      const controller = controllerRef.current;
      controllerRef.current = null;
      controller?.abort();
    };
  }, [boundary]);

  const close = () => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setImporting(false);
    onClose();
  };

  const importFile = async () => {
    if (!file || importing) return;
    const controller = new AbortController();
    const requestBoundary = boundary;
    controllerRef.current = controller;
    setImporting(true);
    setError(null);
    try {
      const result = await importPptxInBrowser(
        file,
        (blob, filename) =>
          uploadDraftClassroomMedia(
            assetId,
            blob,
            filename,
            controller.signal,
          ),
        controller.signal,
      );
      if (
        controller.signal.aborted ||
        activeBoundaryRef.current !== requestBoundary
      ) {
        return;
      }
      await onImported(result);
      if (
        controller.signal.aborted ||
        activeBoundaryRef.current !== requestBoundary
      ) {
        return;
      }
      close();
    } catch {
      if (!controller.signal.aborted) {
        setError(t("classroom.import.failed"));
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setImporting(false);
      }
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={close}
      closeOnBackdrop={!importing}
      closeOnEscape={!importing}
      title={t("classroom.import.title")}
      titleIcon={<FileUp className="h-5 w-5" aria-hidden="true" />}
      footer={
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={close}
            className="rounded-lg border border-[var(--border)] px-4 py-2 text-sm font-medium text-[var(--foreground)] hover:bg-[var(--muted)]"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={importFile}
            disabled={!file || importing}
            className="inline-flex items-center gap-2 rounded-lg bg-[var(--primary)] px-4 py-2 text-sm font-semibold text-[var(--primary-foreground)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {importing ? (
              <LoaderCircle
                className="h-4 w-4 animate-spin motion-reduce:animate-none"
                aria-hidden="true"
              />
            ) : null}
            {importing
              ? t("classroom.import.importing")
              : t("classroom.import.action")}
          </button>
        </div>
      }
    >
      <div className="space-y-4 p-5">
        <p className="text-sm text-[var(--muted-foreground)]">
          {t("classroom.import.description")}
        </p>
        <label className="block rounded-xl border border-dashed border-[var(--border)] bg-[var(--muted)]/40 p-5 text-center">
          <span className="mb-2 block text-sm font-medium text-[var(--foreground)]">
            {file?.name ?? t("classroom.import.chooseFile")}
          </span>
          <input
            type="file"
            accept=".pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation"
            disabled={importing}
            onChange={event => {
              const next = event.currentTarget.files?.[0] ?? null;
              setFile(next);
              setError(null);
            }}
            className="mx-auto block max-w-full text-sm text-[var(--muted-foreground)] file:mr-3 file:rounded-lg file:border-0 file:bg-[var(--card)] file:px-3 file:py-2 file:font-medium file:text-[var(--foreground)]"
          />
        </label>
        {error ? (
          <p role="alert" className="text-sm text-[var(--destructive)]">
            {error}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
