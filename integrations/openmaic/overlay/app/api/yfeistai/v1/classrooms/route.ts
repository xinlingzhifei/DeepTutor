import { callLLM } from "@/lib/ai/llm";
import {
  generateSceneActions,
  generateSceneContent,
} from "@/lib/generation/scene-generator";
import { resolveModel } from "@/lib/server/resolve-model";

import { artifactStore } from "../../../../../lib/yfeistai/artifact-manifest";
import {
  type GeneratedSceneResult,
  type JsonValue,
  buildOpenMaicSourcePrompt,
  createClassroomPostHandler,
  contentJobStore,
  toPortableOpenMaicSceneContent,
} from "../../../../../lib/yfeistai/content-generation";
import { readServiceSecret } from "../../../../../lib/yfeistai/service-auth";
import { runSceneRouteAdapter } from "../../../../../lib/yfeistai/generation-adapter";

function portableClone<T>(value: unknown): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function mediaExtension(mime: string): string {
  const extensions: Record<string, string> = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
  };
  const extension = extensions[mime];
  if (!extension) {
    throw new Error("generated media type is not allowed");
  }
  return extension;
}

async function materializeEmbeddedMedia(
  root: Record<string, unknown>,
  context: {
    order: number;
    mediaPolicy: { allowGeneration: boolean; allowedMimeTypes: string[] };
  },
  sceneId: string,
): Promise<GeneratedSceneResult["media"]> {
  const media: NonNullable<GeneratedSceneResult["media"]> = [];
  let mediaIndex = 0;
  const visit = async (value: unknown): Promise<unknown> => {
    if (typeof value === "string") {
      const match = /^data:([^;,]+);base64,([A-Za-z0-9+/=\s]+)$/.exec(value);
      if (!match) {
        return value;
      }
      const mime = match[1].toLowerCase();
      if (!context.mediaPolicy.allowGeneration) {
        throw new Error("teaching brief forbids generated media");
      }
      if (
        !context.mediaPolicy.allowedMimeTypes
          .map((value) => value.toLowerCase())
          .includes(mime)
      ) {
        throw new Error("generated media MIME type is not allowed");
      }
      const bytes = Buffer.from(match[2].replace(/\s/g, ""), "base64");
      if (bytes.byteLength === 0) {
        throw new Error("generated media is empty");
      }
      mediaIndex += 1;
      const mediaId = `${sceneId}-media-${mediaIndex}`;
      const relativePath =
        `media/scene-${context.order + 1}/` +
        `asset-${mediaIndex}.${mediaExtension(mime)}`;
      media.push({
        mediaId,
        relativePath,
        bytes,
        mime,
      });
      return relativePath;
    }
    if (Array.isArray(value)) {
      for (let index = 0; index < value.length; index += 1) {
        value[index] = await visit(value[index]);
      }
      return value;
    }
    if (value !== null && typeof value === "object") {
      const record = value as Record<string, unknown>;
      for (const [key, child] of Object.entries(record)) {
        record[key] = await visit(child);
      }
    }
    return value;
  };
  await visit(root);
  return media;
}

const postClassroom = createClassroomPostHandler({
  readSecret: readServiceSecret,
  store: contentJobStore,
  artifactStore,
  generateScenes: async (scene, context) => {
    const resolved = await resolveModel({ stage: "generate-classroom" });
    const aiCall = async (systemPrompt: string, userPrompt: string) => {
      const response = await callLLM(
        {
          model: resolved.model,
          messages: [
            { role: "system", content: systemPrompt },
            {
              role: "user",
              content: buildOpenMaicSourcePrompt(
                userPrompt,
                context.sourceFragments,
              ),
            },
          ],
          maxOutputTokens: resolved.modelInfo?.outputWindow,
          maxRetries: 0,
        },
        "generate-classroom-scene",
        undefined,
        resolved.thinkingConfig,
      );
      return response.text;
    };
    const upstreamOutline = {
      id: scene.sceneId,
      type: context.sceneType,
      title: scene.title,
      description: scene.summary,
      keyPoints: scene.knowledgePointIds,
      order: context.order,
      ...(context.sceneType === "quiz"
        ? {
            quizConfig: {
              questionCount: 3,
              difficulty: "medium",
              questionTypes: ["single", "multiple", "text"],
            },
          }
        : {}),
      ...(context.sceneType === "interactive"
        ? {
            interactiveConfig: {
              conceptName: scene.title,
              conceptOverview: scene.summary,
              designIdea: `Build an interactive exploration for ${scene.title}.`,
            },
          }
        : {}),
      ...(context.sceneType === "pbl"
        ? {
            pblConfig: {
              projectTopic: scene.title,
              projectDescription: scene.summary,
              targetSkills: scene.knowledgePointIds,
            },
          }
        : {}),
    } as Parameters<typeof generateSceneContent>[0];
    const generated = await runSceneRouteAdapter({
      outline: upstreamOutline,
      languageDirective: context.outline.language,
      languageModel: resolved.model,
      callProvider: aiCall,
      generate: generateSceneContent,
    });
    if (!generated) {
      throw new Error("OpenMAIC did not generate scene content");
    }
    const actions = await generateSceneActions(
      upstreamOutline,
      generated,
      aiCall,
      { languageDirective: context.outline.language },
    );
    const content = toPortableOpenMaicSceneContent(
      context.sceneType,
      generated,
    );
    const portableActions = portableClone<Array<Record<string, JsonValue>>>(
      actions,
    );
    const portableRoot = {
      content,
      actions: portableActions,
    };
    const media = await materializeEmbeddedMedia(
      portableRoot,
      context,
      scene.sceneId,
    );
    return {
      scene: {
        id: scene.sceneId,
        stageId: context.stageId,
        title: scene.title,
        order: context.order,
        type: context.sceneType,
        content,
        actions: portableActions,
      },
      media,
    };
  },
});

export async function POST(request: Request): Promise<Response> {
  return postClassroom(request);
}
