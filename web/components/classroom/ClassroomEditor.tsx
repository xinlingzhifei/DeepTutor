"use client";

import dynamic from "next/dynamic";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import type {
  ClassroomDocument,
  ClassroomScene,
  ClassroomThemeId,
} from "@/lib/openmaic-adapter/contracts";
import { resolveDraftClassroomMediaReferences } from "@/lib/openmaic-adapter/contracts";
import {
  applyEditIntents,
  classroomSlideForEditing,
  EMPTY_CLASSROOM_SELECTION,
  type ClassroomEditIntent,
  type ClassroomSelection,
} from "@/lib/openmaic-adapter/edit-intents";
import {
  createHistory,
  pushHistory,
  redo,
  replacePresent,
  undo,
  type EditorHistory,
} from "@/lib/openmaic-adapter/editor-history";
import {
  mergeImportedSlidesIntoHistory,
  type ImportedSlides,
} from "@/lib/openmaic-adapter/importer";
import {
  applySceneOperations,
  saveClassroomDraft,
  type SceneOperation,
} from "@/lib/openmaic-adapter/scene-operations";

import {
  ClassroomEditorToolbar,
  type ClassroomEditorSaveState,
} from "./ClassroomEditorToolbar";
import { SceneNavigator } from "./SceneNavigator";
import { ScenePropertiesPanel } from "./ScenePropertiesPanel";

const EditableSlideCanvas = dynamic(
  () =>
    import("@/lib/openmaic-adapter").then(
      module => module.EditableClassroomCanvas,
    ),
  { ssr: false },
);

type EditorSaveState =
  | ClassroomEditorSaveState
  | {
      status: "conflict";
      clientRevision: string;
      serverRevision: string;
      serverDocument: ClassroomDocument;
    };

export interface ClassroomEditorProps {
  initialDocument: ClassroomDocument;
  initialRevision: string;
  draftMediaAssetId?: string;
  theme?: ClassroomThemeId;
  disabled?: boolean;
  onDirtyChange?(dirty: boolean): void;
  onSaved?(document: ClassroomDocument, revision: string): void;
  onConflict?(
    localDocument: ClassroomDocument,
    localRevision: string,
    serverDocument: ClassroomDocument,
    serverRevision: string,
  ): void;
}

export interface ClassroomEditorHandle {
  importSlides(result: ImportedSlides): void;
}

function editorId(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${suffix}`;
}

function createScene(
  type: ClassroomScene["type"],
  stageId: string,
  order: number,
  t: TFunction,
): ClassroomScene {
  const id = editorId(type);
  if (type === "slide") {
    return {
      id,
      stageId,
      title: t("classroom.editor.newSlide"),
      order,
      type,
      content: {
        type,
        canvas: {
          id,
          viewportSize: 1_000,
          viewportRatio: 9 / 16,
          elements: [],
        },
      },
      actions: [],
    };
  }
  if (type === "quiz") {
    const questionId = editorId("question");
    return {
      id,
      stageId,
      title: t("classroom.editor.newQuiz"),
      order,
      type,
      content: {
        type,
        questions: [
          {
            id: questionId,
            prompt: t("classroom.editor.newQuestion"),
            questionType: "single_choice",
            options: [
              { id: `${questionId}-yes`, label: t("classroom.editor.yes") },
              { id: `${questionId}-no`, label: t("classroom.editor.no") },
            ],
            correctOptionIds: [`${questionId}-yes`],
            explanation: t("classroom.editor.explainAnswer"),
          },
        ],
      },
      actions: [],
    };
  }
  if (type === "interactive") {
    return {
      id,
      stageId,
      title: t("classroom.editor.newInteraction"),
      order,
      type,
      content: {
        type,
        html: `<main><p>${t("classroom.editor.interactiveContent")}</p></main>`,
        bridgeVersion: "1.0",
        sandbox: { allowScripts: true, allowSameOrigin: false },
      },
      actions: [],
    };
  }
  return {
    id,
    stageId,
    title: t("classroom.editor.newPblScene"),
    order,
    type,
    content: {
      type,
      scenario: t("classroom.editor.describeProblemScenario"),
      roles: [
        {
          id: editorId("role"),
          name: t("classroom.editor.learner"),
          brief: t("classroom.editor.investigateProblem"),
        },
      ],
      milestones: [
        {
          id: editorId("milestone"),
          title: t("classroom.editor.plan"),
          rubric: t("classroom.editor.planRubric"),
        },
      ],
    },
    actions: [],
  };
}

function fingerprint(document: ClassroomDocument): string {
  return JSON.stringify(document);
}

function message(error: unknown, t: TFunction): string {
  return error instanceof Error
    ? error.message
    : t("classroom.editor.editFailed");
}

function retainedSelection(
  selection: ClassroomSelection,
  document: ClassroomDocument,
  sceneId: string,
): ClassroomSelection {
  const ids = new Set(
    classroomSlideForEditing(document, sceneId).elements.map(element => element.id),
  );
  const elementIds = selection.elementIds.filter(id => ids.has(id));
  const primaryId = selection.primaryId && ids.has(selection.primaryId)
    ? selection.primaryId
    : undefined;
  const editingId = selection.editingId && ids.has(selection.editingId)
    ? selection.editingId
    : undefined;
  return { ...selection, elementIds, primaryId, editingId };
}

export const ClassroomEditor = forwardRef<ClassroomEditorHandle, ClassroomEditorProps>(function ClassroomEditor({
  initialDocument,
  initialRevision,
  draftMediaAssetId,
  theme = "snow",
  disabled: externallyDisabled = false,
  onDirtyChange,
  onSaved,
  onConflict,
}, ref) {
  const { t } = useTranslation();
  const [history, setHistory] = useState(() => createHistory(initialDocument));
  const historyRef = useRef<EditorHistory<ClassroomDocument>>(history);
  const [selectedSceneId, setSelectedSceneId] = useState(
    initialDocument.openmaic.scenes[0]?.id ?? "",
  );
  const [selection, setSelection] = useState<ClassroomSelection>(
    EMPTY_CLASSROOM_SELECTION,
  );
  const [revision, setRevision] = useState(initialRevision);
  const [savedFingerprint, setSavedFingerprint] = useState(() =>
    fingerprint(initialDocument),
  );
  const [saveState, setSaveState] = useState<EditorSaveState>({ status: "idle" });
  const saveInFlight = useRef(false);

  const currentDocument = history.present;
  const scenes = currentDocument.openmaic.scenes;
  const currentScene = scenes.find(scene => scene.id === selectedSceneId) ?? scenes[0];
  const dirty = fingerprint(currentDocument) !== savedFingerprint;
  const mutationDisabled = externallyDisabled || saveState.status === "saving";
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  const editableSlide = useMemo(
    () => {
      if (currentScene?.type !== "slide") return null;
      const renderDocument = draftMediaAssetId
        ? resolveDraftClassroomMediaReferences(currentDocument, draftMediaAssetId)
        : currentDocument;
      return classroomSlideForEditing(renderDocument, currentScene.id, theme);
    },
    [currentDocument, currentScene, draftMediaAssetId, theme],
  );
  const knowledgePointIds = useMemo(
    () =>
      currentScene
        ? currentDocument.knowledgePointMappings
            .filter(mapping => mapping.sceneIds.includes(currentScene.id))
            .map(mapping => mapping.knowledgePointId)
        : [],
    [currentDocument, currentScene],
  );

  const setEditorHistory = (next: EditorHistory<ClassroomDocument>) => {
    historyRef.current = next;
    setHistory(next);
    const selectedStillExists = next.present.openmaic.scenes.some(
      scene => scene.id === selectedSceneId,
    );
    if (!selectedStillExists) {
      setSelectedSceneId(next.present.openmaic.scenes[0]?.id ?? "");
      setSelection(EMPTY_CLASSROOM_SELECTION);
    }
  };

  const commitDocument = (next: ClassroomDocument) => {
    if (mutationDisabled) return;
    setEditorHistory(pushHistory(historyRef.current, next));
    setSaveState({ status: "idle" });
  };

  useImperativeHandle(ref, () => ({
    importSlides(result) {
      if (mutationDisabled || saveInFlight.current) {
        throw new Error("Cannot import slides while the classroom is locked");
      }
      try {
        const previousSceneCount = historyRef.current.present.openmaic.scenes.length;
        const next = mergeImportedSlidesIntoHistory(
          historyRef.current,
          result.slides,
          result.media,
          editorId,
        );
        const firstImported = next.present.openmaic.scenes[previousSceneCount];
        setEditorHistory(next);
        setSelectedSceneId(firstImported?.id ?? selectedSceneId);
        setSelection(EMPTY_CLASSROOM_SELECTION);
        setSaveState({ status: "idle" });
      } catch (error) {
        setSaveState({ status: "error", message: message(error, t) });
        throw error;
      }
    },
  }));

  const commitSceneOperations = (
    operations: readonly SceneOperation[],
  ): ClassroomDocument | null => {
    if (mutationDisabled || saveInFlight.current) return null;
    try {
      const next = applySceneOperations(historyRef.current.present, operations);
      commitDocument(next);
      return next;
    } catch (error) {
      setSaveState({ status: "error", message: message(error, t) });
      return null;
    }
  };

  const handleEditIntents = (intents: ClassroomEditIntent[]) => {
    if (
      !currentScene ||
      currentScene.type !== "slide" ||
      mutationDisabled ||
      saveInFlight.current
    ) return;
    try {
      const next = applyEditIntents(
        historyRef.current.present,
        intents,
        currentScene.id,
      );
      commitDocument(next);
      setSelection(current => retainedSelection(current, next, currentScene.id));
    } catch (error) {
      setSaveState({ status: "error", message: message(error, t) });
    }
  };

  const handleSave = async () => {
    if (!dirty || mutationDisabled || saveInFlight.current) return;
    saveInFlight.current = true;
    setSaveState({ status: "saving" });
    const localDocument = historyRef.current.present;
    const localRevision = revision;
    try {
      const result = await saveClassroomDraft({
        classroomId: localDocument.classroomId,
        revision: localRevision,
        document: localDocument,
      });
      if (result.status === "conflict") {
        setSaveState({
          status: "conflict",
          clientRevision: result.clientRevision,
          serverRevision: result.serverRevision,
          serverDocument: result.serverDocument,
        });
        onConflict?.(
          localDocument,
          result.clientRevision,
          result.serverDocument,
          result.serverRevision,
        );
        return;
      }
      setRevision(result.revision);
      setSavedFingerprint(fingerprint(result.document));
      setEditorHistory(replacePresent(historyRef.current, result.document));
      setSaveState({ status: "saved" });
      onSaved?.(result.document, result.revision);
    } catch (error) {
      setSaveState({ status: "error", message: message(error, t) });
    } finally {
      saveInFlight.current = false;
    }
  };

  if (!currentScene) {
    return (
      <p className="p-4 text-sm text-[var(--destructive)]">
        {t("classroom.editor.noScenes")}
      </p>
    );
  }

  return (
    <section className="openmaic-surface flex h-full min-h-[640px] flex-col overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--background)] text-[var(--foreground)]">
      <ClassroomEditorToolbar
        canUndo={history.past.length > 0}
        canRedo={history.future.length > 0}
        dirty={dirty}
        disabled={mutationDisabled}
        saveState={saveState}
        onUndo={() => {
          if (mutationDisabled || saveInFlight.current) return;
          setEditorHistory(undo(historyRef.current));
          setSelection(EMPTY_CLASSROOM_SELECTION);
          setSaveState({ status: "idle" });
        }}
        onRedo={() => {
          if (mutationDisabled || saveInFlight.current) return;
          setEditorHistory(redo(historyRef.current));
          setSelection(EMPTY_CLASSROOM_SELECTION);
          setSaveState({ status: "idle" });
        }}
        onSave={handleSave}
      />
      {saveState.status === "conflict" && "serverDocument" in saveState && (
        <details className="border-b border-[var(--destructive)]/40 bg-[var(--destructive)]/5 px-4 py-2 text-xs">
          <summary className="cursor-pointer font-semibold">
            {t("classroom.editor.compareConflict", {
              local: saveState.clientRevision,
              server: saveState.serverRevision,
            })}
          </summary>
          <div className="mt-2 grid max-h-72 grid-cols-2 gap-2 overflow-auto">
            <pre className="whitespace-pre-wrap rounded bg-[var(--background)] p-2">
              {JSON.stringify(currentDocument, null, 2)}
            </pre>
            <pre className="whitespace-pre-wrap rounded bg-[var(--background)] p-2">
              {JSON.stringify(saveState.serverDocument, null, 2)}
            </pre>
          </div>
        </details>
      )}
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <SceneNavigator
          scenes={scenes}
          selectedSceneId={currentScene.id}
          disabled={mutationDisabled}
          onSelect={sceneId => {
            setSelectedSceneId(sceneId);
            setSelection(EMPTY_CLASSROOM_SELECTION);
          }}
          onMove={(sceneId, toIndex) =>
            commitSceneOperations([{ type: "scene.reorder", sceneId, toIndex }])
          }
          onDuplicate={sceneId => {
            const newSceneId = editorId("scene");
            const next = commitSceneOperations([
              { type: "scene.duplicate", sceneId, newSceneId },
            ]);
            if (next) {
              setSelectedSceneId(newSceneId);
              setSelection(EMPTY_CLASSROOM_SELECTION);
            }
          }}
          onDelete={sceneId => {
            const next = commitSceneOperations([{ type: "scene.delete", sceneId }]);
            if (next && sceneId === currentScene.id) {
              setSelectedSceneId(next.openmaic.scenes[0]?.id ?? "");
              setSelection(EMPTY_CLASSROOM_SELECTION);
            }
          }}
          onAdd={type => {
            const scene = createScene(
              type,
              currentDocument.openmaic.stage.id,
              currentDocument.openmaic.scenes.length,
              t,
            );
            const next = commitSceneOperations([{ type: "scene.add", scene }]);
            if (next) {
              setSelectedSceneId(scene.id);
              setSelection(EMPTY_CLASSROOM_SELECTION);
            }
          }}
        />
        <main className="min-h-[22rem] min-w-0 flex-1 overflow-auto bg-[var(--muted)]/25 p-4">
          {editableSlide ? (
            <div className="h-[22rem] w-full lg:h-full">
              <EditableSlideCanvas
                slide={editableSlide}
                selection={selection}
                onSelectionChange={setSelection}
                onElementsChange={handleEditIntents}
                snapping={{ toCanvas: true, toElements: true, range: 5 }}
                grid={25}
                ruler
                className="mx-auto"
                style={{ pointerEvents: mutationDisabled ? "none" : undefined }}
              />
            </div>
          ) : (
            <div className="mx-auto max-w-3xl rounded-xl border border-[var(--border)] bg-[var(--card)] p-6">
              <h1 className="text-lg font-semibold">{currentScene.title}</h1>
              <p className="mt-1 text-sm capitalize text-[var(--muted-foreground)]">
                {t("classroom.editor.scenePropertiesHint", {
                  type: t(`classroom.sceneType.${currentScene.type}`),
                })}
              </p>
            </div>
          )}
        </main>
        <ScenePropertiesPanel
          scene={currentScene}
          knowledgePointIds={knowledgePointIds}
          disabled={mutationDisabled}
          selectedElementCount={selection.elementIds.length}
          onOperation={operation => commitSceneOperations([operation])}
        />
      </div>
      <footer className="border-t border-[var(--border)] px-4 py-1 text-xs text-[var(--muted-foreground)]">
        {t("classroom.editor.footer", {
          revision,
          theme: t(`classroom.theme.${theme}`),
          current: currentScene.order + 1,
          total: scenes.length,
        })}
      </footer>
    </section>
  );
});
