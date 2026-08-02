import {
  normalizeElement,
  type Action,
  type CodeLine,
  type PPTCodeElement,
  type PPTElement,
  type PPTElementOutline,
  type PPTLineElement,
  type PPTTableElement,
  type Slide,
  type TableCell,
} from '@openmaic/dsl'
import { renderToString } from 'katex'

import type { ClassroomDocument } from '../contracts'
import { sanitizeRendererElement, sanitizeRendererHtml } from '../sanitize'
import { readPlaybackAction, type PlaybackCursor, type WhiteboardAction } from './types'

export interface WhiteboardState {
  open: boolean
  slide: Slide
}

const theme = {
  backgroundColor: '#ffffff',
  themeColors: ['#2563eb', '#0f172a', '#f8fafc'],
  fontColor: '#0f172a',
  fontName: 'var(--openmaic-font-sans)',
}

export function createWhiteboardState(id: string): WhiteboardState {
  if (!id) throw new Error('whiteboard id is required')
  return {
    open: false,
    slide: { id, viewportSize: 1_000, viewportRatio: 9 / 16, theme, elements: [] },
  }
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function textHtml(value: string, fontSize?: number): string {
  const content = escapeHtml(value).replaceAll('\n', '<br>')
  if (fontSize === undefined) return content
  const safeSize = Math.min(256, Math.max(6, fontSize))
  return `<span style="font-size:${safeSize}px">${content}</span>`
}

function baseElement(action: { id: string; elementId?: string; x: number; y: number }) {
  return {
    id: action.elementId || action.id,
    left: action.x,
    top: action.y,
    rotate: 0,
  }
}

function shapePath(shape: 'rectangle' | 'circle' | 'triangle'): string {
  if (shape === 'circle') {
    return 'M100 0 A100 100 0 1 1 99.999 0 Z'
  }
  if (shape === 'triangle') return 'M100 0 L200 200 L0 200 Z'
  return 'M0 0 L200 0 L200 200 L0 200 Z'
}

function tableOutline(
  value: Extract<WhiteboardAction, { type: 'wb_draw_table' }>['outline']
): PPTElementOutline {
  const style = value?.style
  return {
    width: value?.width ?? 1,
    color: value?.color ?? theme.fontColor,
    style: style === 'dashed' || style === 'dotted' || style === 'solid' ? style : 'solid',
  }
}

function tableCells(
  data: string[][],
  elementId: string
): { cells: TableCell[][]; columns: number } {
  const columns = data[0]?.length ?? 0
  if (data.length === 0 || columns === 0 || data.some(row => row.length !== columns)) {
    throw new Error('whiteboard table must be a non-empty rectangle')
  }
  return {
    columns,
    cells: data.map((row, rowIndex) =>
      row.map((text, columnIndex) => ({
        id: `${elementId}-r${rowIndex + 1}-c${columnIndex + 1}`,
        colspan: 1,
        rowspan: 1,
        text: textHtml(text),
      }))
    ),
  }
}

function codeLines(value: string, actionId: string, initial = false): CodeLine[] {
  return value.split('\n').map((content, index) => ({
    id: initial ? `L${index + 1}` : `${actionId}-L${index + 1}`,
    content,
  }))
}

function appendedElement(action: WhiteboardAction): PPTElement | null {
  switch (action.type) {
    case 'wb_draw_text':
      return normalizeElement({
        ...baseElement(action),
        type: 'text',
        content: textHtml(action.content, action.fontSize),
        width: action.width ?? 320,
        height: action.height ?? 80,
        defaultFontName: theme.fontName,
        defaultColor: action.color ?? theme.fontColor,
      })
    case 'wb_draw_shape':
      return normalizeElement({
        ...baseElement(action),
        type: 'shape',
        width: action.width,
        height: action.height,
        viewBox: [200, 200],
        path: shapePath(action.shape),
        fill: action.fillColor ?? theme.themeColors[0],
      })
    case 'wb_draw_chart':
      return {
        ...baseElement(action),
        type: 'chart',
        width: action.width,
        height: action.height,
        chartType: action.chartType,
        data: action.data,
        themeColors: action.themeColors ?? theme.themeColors,
      }
    case 'wb_draw_latex':
      return {
        ...baseElement(action),
        type: 'latex',
        latex: action.latex,
        html: sanitizeRendererHtml(
          renderToString(action.latex, {
            output: 'htmlAndMathml',
            strict: 'ignore',
            throwOnError: false,
            trust: false,
          })
        ),
        width: action.width ?? 320,
        height: action.height ?? 80,
        color: action.color ?? theme.fontColor,
      }
    case 'wb_draw_table': {
      const id = action.elementId || action.id
      const { cells, columns } = tableCells(action.data, id)
      const table: PPTTableElement = {
        ...baseElement(action),
        type: 'table',
        width: action.width,
        height: action.height,
        data: cells,
        colWidths: Array.from({ length: columns }, () => 1 / columns),
        cellMinHeight: Math.max(24, action.height / cells.length),
        outline: tableOutline(action.outline),
        theme: action.theme
          ? {
              color: action.theme.color,
              rowHeader: true,
              rowFooter: false,
              colHeader: false,
              colFooter: false,
            }
          : undefined,
      }
      return table
    }
    case 'wb_draw_line': {
      const left = Math.min(action.startX, action.endX)
      const top = Math.min(action.startY, action.endY)
      const line: PPTLineElement = {
        id: action.elementId || action.id,
        type: 'line',
        left,
        top,
        width: action.width ?? 2,
        start: [action.startX - left, action.startY - top],
        end: [action.endX - left, action.endY - top],
        color: action.color ?? theme.fontColor,
        style: action.style ?? 'solid',
        points: action.points ?? ['', ''],
      }
      return line
    }
    case 'wb_draw_code': {
      const code: PPTCodeElement = {
        ...baseElement(action),
        type: 'code',
        language: action.language,
        lines: codeLines(action.code, action.id, true),
        width: action.width ?? 520,
        height: action.height ?? 280,
        fileName: action.fileName,
        showLineNumbers: true,
      }
      return code
    }
    default:
      return null
  }
}

function editCode(
  element: PPTElement,
  action: Extract<WhiteboardAction, { type: 'wb_edit_code' }>
): PPTElement {
  if (element.id !== action.elementId || element.type !== 'code') return element
  const source = [...element.lines]
  const requestedIds = [action.lineId, ...(action.lineIds ?? [])].filter((value): value is string =>
    Boolean(value)
  )
  if (requestedIds.length === 0 || requestedIds.some(id => !source.some(line => line.id === id))) {
    throw new Error('whiteboard code line is invalid')
  }
  if (action.operation === 'delete_lines') {
    const deleted = new Set(requestedIds)
    return { ...element, lines: source.filter(line => !deleted.has(line.id)) }
  }
  if (!action.lineId) throw new Error('whiteboard code line is invalid')
  const index = source.findIndex(line => line.id === action.lineId)
  const content = codeLines(action.content ?? '', action.id)
  if (action.operation === 'replace_lines') source.splice(index, 1, ...content)
  if (action.operation === 'insert_before') source.splice(index, 0, ...content)
  if (action.operation === 'insert_after') source.splice(index + 1, 0, ...content)
  return { ...element, lines: source }
}

export function applyWhiteboardAction(
  state: WhiteboardState,
  action: WhiteboardAction
): WhiteboardState {
  const slide = {
    ...state.slide,
    elements: state.slide.elements.map(element => ({ ...element })),
  }
  switch (action.type) {
    case 'wb_open':
      return { open: true, slide }
    case 'wb_close':
      return { open: false, slide }
    case 'wb_clear':
      return { open: state.open, slide: { ...slide, elements: [] } }
    case 'wb_delete':
      if (!slide.elements.some(element => element.id === action.elementId)) {
        throw new Error('whiteboard element does not exist')
      }
      return {
        open: state.open,
        slide: {
          ...slide,
          elements: slide.elements.filter(element => element.id !== action.elementId),
        },
      }
    case 'wb_edit_code':
      if (
        !slide.elements.some(element => element.id === action.elementId && element.type === 'code')
      ) {
        throw new Error('whiteboard code element does not exist')
      }
      return {
        open: state.open,
        slide: {
          ...slide,
          elements: slide.elements.map(element => editCode(element, action)),
        },
      }
    default: {
      const element = appendedElement(action)
      if (!element) throw new Error('unsupported whiteboard action')
      const sanitized = sanitizeRendererElement(element)
      if (slide.elements.some(existing => existing.id === sanitized.id)) {
        throw new Error('whiteboard element id already exists')
      }
      return {
        open: state.open,
        slide: { ...slide, elements: [...slide.elements, sanitized] },
      }
    }
  }
}

function isWhiteboardAction(action: Action): action is WhiteboardAction {
  return action.type.startsWith('wb_')
}

export function reconstructWhiteboardState(
  document: ClassroomDocument,
  cursor: Pick<PlaybackCursor, 'sceneIndex' | 'actionIndex'>
): WhiteboardState {
  const scenes = [...document.openmaic.scenes].sort((left, right) => left.order - right.order)
  if (
    !Number.isSafeInteger(cursor.sceneIndex) ||
    !Number.isSafeInteger(cursor.actionIndex) ||
    cursor.sceneIndex < 0 ||
    cursor.sceneIndex > scenes.length ||
    cursor.actionIndex < 0 ||
    (cursor.sceneIndex === scenes.length && cursor.actionIndex !== 0) ||
    (cursor.sceneIndex < scenes.length &&
      cursor.actionIndex > (scenes[cursor.sceneIndex]?.actions.length ?? 0))
  ) {
    throw new Error('whiteboard playback cursor is invalid')
  }

  let state = createWhiteboardState(`${document.classroomVersionId}-whiteboard`)
  scenes.forEach((scene, sceneIndex) => {
    if (sceneIndex > cursor.sceneIndex) return
    const limit = sceneIndex < cursor.sceneIndex ? scene.actions.length : cursor.actionIndex
    scene.actions.slice(0, limit).forEach(input => {
      const action = readPlaybackAction(input)
      if (isWhiteboardAction(action)) {
        state = applyWhiteboardAction(state, action)
      }
    })
  })
  return state
}
