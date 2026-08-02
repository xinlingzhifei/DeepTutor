import { notFound } from "next/navigation";

import {
  ClassroomVisualFixture,
  type ClassroomVisualHost,
  type ClassroomVisualScene,
  type ClassroomVisualTheme,
} from "./ClassroomVisualFixture";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const HOSTS = new Set<ClassroomVisualHost>(["editor", "player"]);
const SCENES = new Set<ClassroomVisualScene>([
  "slide",
  "quiz",
  "interactive",
  "pbl",
]);
const THEMES = new Set<ClassroomVisualTheme>([
  "snow",
  "cream",
  "dark",
  "glass",
]);

function scalar(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export default async function ClassroomVisualBaselinePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  if (process.env.PW_VISUAL_BASELINE !== "1") notFound();

  const params = await searchParams;
  const host = scalar(params.host) ?? "editor";
  const scene = scalar(params.scene) ?? "slide";
  const theme = scalar(params.theme) ?? "snow";
  if (
    !HOSTS.has(host as ClassroomVisualHost) ||
    !SCENES.has(scene as ClassroomVisualScene) ||
    !THEMES.has(theme as ClassroomVisualTheme)
  ) {
    notFound();
  }

  return (
    <ClassroomVisualFixture
      host={host as ClassroomVisualHost}
      scene={scene as ClassroomVisualScene}
      theme={theme as ClassroomVisualTheme}
    />
  );
}
