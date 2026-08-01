import {
  validateAction,
  type Action,
  type DiscussionAction,
  type LaserAction,
  type PlayVideoAction,
  type SpeechAction,
  type SpotlightAction,
} from '@openmaic/dsl'

import type { ClassroomLearningEvent } from './events'

export type WhiteboardAction = Extract<Action, { type: `wb_${string}` }>
export type WidgetAction = Extract<Action, { type: `widget_${string}` }>
export type EffectAction = SpotlightAction | LaserAction

export interface PlaybackPendingAction {
  sceneId: string
  actionId: string
  actionIndex: number
  preparedAtRevision: number
  executionId: string
}

export interface PlaybackCursor {
  classroomVersionId: string
  documentFingerprint: string
  revision: number
  sceneIndex: number
  actionIndex: number
  consumed: string[]
  enteredSceneId: string | null
  pendingAction: PlaybackPendingAction | null
}

export interface PlaybackCheckpoint {
  /**
   * Hosts must treat this key idempotently. Retrying a checkpoint that committed but whose
   * response was lost must return success without appending its events a second time.
   */
  checkpointId: string
  expectedRevision: number
  cursor: PlaybackCursor
  events: readonly ClassroomLearningEvent[]
}

export interface PlaybackActionExecution {
  /** Hosts use this key to deduplicate a side effect after process recovery. */
  executionId: string
  classroomVersionId: string
  documentFingerprint: string
  sceneId: string
  actionId: string
}

export interface PlaybackPorts {
  renderScene(sceneId: string): void
  speak(
    action: SpeechAction,
    signal: AbortSignal,
    execution: PlaybackActionExecution
  ): Promise<void>
  playVideo(
    action: PlayVideoAction,
    signal: AbortSignal,
    execution: PlaybackActionExecution
  ): Promise<void>
  applyWhiteboard(action: WhiteboardAction, execution: PlaybackActionExecution): Promise<void>
  applyEffect(action: EffectAction, execution: PlaybackActionExecution): Promise<void> | void
  openDiscussion(
    action: DiscussionAction,
    execution: PlaybackActionExecution
  ): Promise<void>
  postWidgetAction(action: WidgetAction, execution: PlaybackActionExecution): Promise<void>
  /** Atomically persists the cursor and all events, with CAS on expectedRevision. */
  commitCheckpoint(checkpoint: PlaybackCheckpoint): Promise<void>
}

export type PlaybackState =
  | 'idle'
  | 'running'
  | 'paused'
  | 'switching'
  | 'stopped'
  | 'completed'

export function playbackStableId(kind: string, ...parts: readonly (string | number)[]): string {
  if (!kind || parts.some(part => typeof part === 'string' && (!part || part.includes('\0')))) {
    throw new Error('playback id binding is invalid')
  }
  return JSON.stringify(['yfeistai.playback', kind, ...parts])
}

export function playbackInteractionKey(sceneId: string, interactionId: string): string {
  if (!sceneId || !interactionId || sceneId.includes('\0') || interactionId.includes('\0')) {
    throw new Error('playback interaction binding is invalid')
  }
  return JSON.stringify([sceneId, interactionId])
}

export function readPlaybackInteractionKey(
  value: string
): readonly [sceneId: string, interactionId: string] {
  let parsed: unknown
  try {
    parsed = JSON.parse(value)
  } catch {
    throw new Error('playback interaction key is invalid')
  }
  if (
    !Array.isArray(parsed) ||
    parsed.length !== 2 ||
    parsed.some(item => typeof item !== 'string' || !item || item.includes('\0'))
  ) {
    throw new Error('playback interaction key is invalid')
  }
  return parsed as [string, string]
}

export function readPlaybackAction(input: unknown): Action {
  const report = validateAction(input)
  if (!report.valid) {
    throw new Error(
      `invalid playback action: ${report.errors
        .map(issue => `${issue.path}: ${issue.message}`)
        .join('; ')}`
    )
  }
  return input as Action
}
