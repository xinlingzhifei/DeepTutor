import { parseFragment } from 'parse5'

export const ALLOWED_INTERACTIVE_EVENTS = [
  'interactive.ready',
  'interactive.answer',
  'interactive.progress',
  'interactive.completed',
] as const

export type InteractiveEventType = (typeof ALLOWED_INTERACTIVE_EVENTS)[number]

export interface InteractiveEvent {
  eventId: string
  type: InteractiveEventType
  payload?: Readonly<Record<string, unknown>>
}

interface HtmlAttribute {
  name: string
  value: string
}

interface HtmlNode {
  nodeName: string
  tagName?: string
  value?: string
  attrs?: HtmlAttribute[]
  childNodes?: HtmlNode[]
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

interface PayloadBudget {
  nodes: number
  characters: number
}

function jsonValue(value: unknown, depth: number, budget: PayloadBudget): unknown {
  budget.nodes += 1
  if (depth > 8 || budget.nodes > 1_000) {
    throw new Error('interactive event payload is too complex')
  }
  if (value === null || typeof value === 'boolean') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw new Error('interactive event payload contains an invalid number')
    }
    return value
  }
  if (typeof value === 'string') {
    budget.characters += value.length
    if (budget.characters > 65_536) {
      throw new Error('interactive event payload is too large')
    }
    return value
  }
  if (Array.isArray(value)) {
    if (value.length > 100) {
      throw new Error('interactive event payload has too many items')
    }
    return value.map(item => jsonValue(item, depth + 1, budget))
  }
  if (!isRecord(value)) {
    throw new Error('interactive event payload must contain JSON values')
  }
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) {
    throw new Error('interactive event payload must be a plain object')
  }
  const entries = Object.entries(value)
  if (entries.length > 100) {
    throw new Error('interactive event payload has too many fields')
  }
  const output: Record<string, unknown> = {}
  for (const [key, item] of entries) {
    if (['__proto__', 'prototype', 'constructor'].includes(key)) {
      throw new Error('interactive event payload contains an unsafe field')
    }
    budget.characters += key.length
    output[key] = jsonValue(item, depth + 1, budget)
  }
  return output
}

export function sanitizeInteractivePayload(
  value: unknown
): Readonly<Record<string, unknown>> | undefined {
  if (value === undefined) return undefined
  if (!isRecord(value)) {
    throw new Error('interactive event payload must be an object')
  }
  const parsed = jsonValue(value, 0, { nodes: 0, characters: 0 })
  const encoded = JSON.stringify(parsed)
  if (encoded.length > 65_536) {
    throw new Error('interactive event payload is too large')
  }
  return parsed as Readonly<Record<string, unknown>>
}

export function readInteractiveMessage(
  value: unknown,
  sessionNonce: string,
  frameInstanceId: string
): InteractiveEvent | null {
  if (!isRecord(value) || value.sessionNonce !== sessionNonce) return null
  if (
    typeof value.eventId !== 'string' ||
    value.eventId.length === 0 ||
    value.eventId.length > 512 ||
    !value.eventId.startsWith(`${sessionNonce}:${frameInstanceId}:`) ||
    /[\u0000-\u001f\u007f]/.test(value.eventId) ||
    typeof value.type !== 'string' ||
    !ALLOWED_INTERACTIVE_EVENTS.includes(value.type as InteractiveEventType)
  ) {
    return null
  }
  return {
    eventId: value.eventId,
    type: value.type as InteractiveEventType,
    payload: sanitizeInteractivePayload(value.payload),
  }
}

const INTERACTIVE_TAGS = new Set([
  'a',
  'article',
  'aside',
  'b',
  'blockquote',
  'br',
  'button',
  'code',
  'dd',
  'div',
  'dl',
  'dt',
  'em',
  'fieldset',
  'figcaption',
  'figure',
  'footer',
  'form',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'header',
  'hr',
  'i',
  'img',
  'input',
  'label',
  'legend',
  'li',
  'main',
  'ol',
  'option',
  'output',
  'p',
  'pre',
  'progress',
  'section',
  'select',
  'small',
  'span',
  'strong',
  'sub',
  'sup',
  'table',
  'tbody',
  'td',
  'textarea',
  'tfoot',
  'th',
  'thead',
  'tr',
  'u',
  'ul',
])

const INTERACTIVE_DROP_WITH_CONTENT = new Set([
  'base',
  'embed',
  'iframe',
  'link',
  'meta',
  'noscript',
  'object',
  'script',
  'style',
  'template',
])

const INTERACTIVE_VOID_TAGS = new Set(['br', 'hr', 'img', 'input'])

const INTERACTIVE_STYLE: Readonly<Record<string, RegExp>> = {
  color: /^(?:#[0-9a-f]{3,8}|(?:rgb|hsl)a?\([\d\s.,%+-]+\)|[a-z]+)$/i,
  'background-color': /^(?:#[0-9a-f]{3,8}|(?:rgb|hsl)a?\([\d\s.,%+-]+\)|[a-z]+)$/i,
  display: /^(?:block|inline|inline-block|flex|inline-flex|grid|none)$/,
  position: /^(?:static|relative|absolute)$/,
  width: /^\d+(?:\.\d+)?(?:px|em|rem|%|vw)?$/,
  height: /^\d+(?:\.\d+)?(?:px|em|rem|%|vh)?$/,
  'min-width': /^\d+(?:\.\d+)?(?:px|em|rem|%|vw)?$/,
  'min-height': /^\d+(?:\.\d+)?(?:px|em|rem|%|vh)?$/,
  'max-width': /^\d+(?:\.\d+)?(?:px|em|rem|%|vw)?$/,
  'max-height': /^\d+(?:\.\d+)?(?:px|em|rem|%|vh)?$/,
  margin: /^\d+(?:\.\d+)?(?:px|em|rem|%)(?:\s+\d+(?:\.\d+)?(?:px|em|rem|%)){0,3}$/,
  padding: /^\d+(?:\.\d+)?(?:px|em|rem|%)(?:\s+\d+(?:\.\d+)?(?:px|em|rem|%)){0,3}$/,
  gap: /^\d+(?:\.\d+)?(?:px|em|rem)$/,
  'font-size': /^\d+(?:\.\d+)?(?:px|pt|em|rem|%)$/,
  'font-weight': /^(?:normal|bold|[1-9]00)$/,
  'text-align': /^(?:left|right|center|justify)$/,
  'line-height': /^\d+(?:\.\d+)?(?:px|pt|em|rem|%)?$/,
  'flex-direction': /^(?:row|row-reverse|column|column-reverse)$/,
  'align-items': /^(?:stretch|flex-start|flex-end|center|baseline)$/,
  'justify-content': /^(?:flex-start|flex-end|center|space-between|space-around|space-evenly)$/,
  cursor: /^(?:default|pointer|text|not-allowed)$/,
}

function escapeText(value: string): string {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
}

function escapeAttribute(value: string): string {
  return escapeText(value).replaceAll('"', '&quot;')
}

function safeInteractiveStyle(value: string): string | null {
  const output: string[] = []
  value.split(';').forEach(declaration => {
    const separator = declaration.indexOf(':')
    if (separator < 1) return
    const property = declaration.slice(0, separator).trim().toLowerCase()
    const candidate = declaration.slice(separator + 1).trim()
    if (INTERACTIVE_STYLE[property]?.test(candidate)) output.push(`${property}:${candidate}`)
  })
  return output.length > 0 ? output.join(';') : null
}

function safeInteractiveAttribute(tag: string, attribute: HtmlAttribute): HtmlAttribute | null {
  const name = attribute.name.toLowerCase()
  const value = attribute.value
  if (/^on/i.test(name) || ['action', 'formaction', 'target', 'srcdoc'].includes(name)) return null
  if (name === 'style') {
    const style = safeInteractiveStyle(value)
    return style ? { name, value: style } : null
  }
  if (name === 'data-yfeistai-event') {
    return ALLOWED_INTERACTIVE_EVENTS.includes(value as InteractiveEventType)
      ? { name, value }
      : null
  }
  if (name === 'data-yfeistai-payload') {
    try {
      const payload = sanitizeInteractivePayload(JSON.parse(value))
      return payload ? { name, value: JSON.stringify(payload) } : null
    } catch {
      return null
    }
  }
  if (name === 'href') {
    return tag === 'a' && /^#[A-Za-z][A-Za-z0-9_.:-]*$/.test(value) ? { name, value } : null
  }
  if (name === 'src') {
    return tag === 'img' && /^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$/i.test(value)
      ? { name, value }
      : null
  }
  if (name === 'type') {
    return /^(?:button|submit|text|number|radio|checkbox|range)$/i.test(value)
      ? { name, value: value.toLowerCase() }
      : null
  }
  if (
    ['class', 'id', 'title', 'role', 'name', 'value', 'placeholder', 'for'].includes(name) &&
    value.length <= 1_024 &&
    !/[\u0000-\u001f\u007f<>]/.test(value)
  ) {
    return { name, value }
  }
  if (/^aria-(?:label|labelledby|describedby|hidden|live)$/.test(name) && value.length <= 512) {
    return { name, value }
  }
  if (['checked', 'disabled', 'multiple', 'required', 'selected'].includes(name)) {
    return { name, value: '' }
  }
  if (['min', 'max', 'step', 'rows', 'cols', 'colspan', 'rowspan'].includes(name) && /^\d+(?:\.\d+)?$/.test(value)) {
    return { name, value }
  }
  return null
}

function renderInteractiveNode(node: HtmlNode): string {
  if (node.nodeName === '#text') return escapeText(node.value ?? '')
  if (node.nodeName === '#comment') return ''
  const tag = node.tagName?.toLowerCase()
  const children = (node.childNodes ?? []).map(renderInteractiveNode).join('')
  if (!tag) return children
  if (INTERACTIVE_DROP_WITH_CONTENT.has(tag)) return ''
  if (!INTERACTIVE_TAGS.has(tag)) return children
  const attributes = (node.attrs ?? [])
    .map(attribute => safeInteractiveAttribute(tag, attribute))
    .filter((attribute): attribute is HtmlAttribute => Boolean(attribute))
    .map(attribute =>
      attribute.value === ''
        ? attribute.name
        : `${attribute.name}="${escapeAttribute(attribute.value)}"`
    )
    .join(' ')
  const opening = attributes ? `<${tag} ${attributes}>` : `<${tag}>`
  return INTERACTIVE_VOID_TAGS.has(tag) ? opening : `${opening}${children}</${tag}>`
}

export function sanitizeInteractiveHtml(input: string): string {
  if (typeof input !== 'string' || input.length > 1_000_000) {
    throw new Error('interactive HTML exceeds the supported size')
  }
  const fragment = parseFragment(input) as unknown as HtmlNode
  return (fragment.childNodes ?? []).map(renderInteractiveNode).join('')
}

function scriptValue(value: string): string {
  return JSON.stringify(value)
    .replaceAll('<', '\\u003c')
    .replaceAll('\u2028', '\\u2028')
    .replaceAll('\u2029', '\\u2029')
}

function cspNonce(value: string): string {
  let hash = 2_166_136_261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16_777_619)
  }
  return `yfeistai${(hash >>> 0).toString(16)}${value.length.toString(16)}`
}

function controlledBridgeRuntime(sessionNonce: string, frameInstanceId: string): string {
  const nonce = scriptValue(sessionNonce)
  const frameId = scriptValue(frameInstanceId)
  return `(()=>{"use strict";const nonce=${nonce};const frameId=${frameId};let sequence=0;let pending=null;Object.defineProperty(window,"__YFEISTAI_SESSION_NONCE__",{value:nonce,writable:false,configurable:false});const post=(type,payload)=>{if(pending){parent.postMessage(pending,"*");return}sequence+=1;pending={sessionNonce:nonce,eventId:nonce+":"+frameId+":"+sequence,type,payload};parent.postMessage(pending,"*")};addEventListener("message",event=>{const data=event.data;if(event.source!==parent||!data||typeof data!=="object"||data.sessionNonce!==nonce||data.type!=="interactive.ack"||!pending||data.eventId!==pending.eventId)return;pending=null});const payloadFor=node=>{const encoded=node.getAttribute("data-yfeistai-payload");if(encoded){try{return JSON.parse(encoded)}catch{return undefined}}if(node instanceof HTMLFormElement){const payload={};for(const [key,value] of new FormData(node).entries()){if(typeof value==="string")payload[key]=value}return payload}return undefined};addEventListener("click",event=>{const anchor=event.target instanceof Element?event.target.closest("a"):null;if(anchor)event.preventDefault();const node=event.target instanceof Element?event.target.closest("[data-yfeistai-event]"):null;if(!node||node instanceof HTMLInputElement||node instanceof HTMLSelectElement||node instanceof HTMLTextAreaElement)return;event.preventDefault();post(node.getAttribute("data-yfeistai-event"),payloadFor(node))},true);addEventListener("change",event=>{const node=event.target instanceof Element?event.target.closest("[data-yfeistai-event]"):null;if(node)post(node.getAttribute("data-yfeistai-event"),payloadFor(node))},true);addEventListener("submit",event=>{event.preventDefault();const form=event.target;if(form instanceof HTMLFormElement){const node=form.closest("[data-yfeistai-event]");if(node)post(node.getAttribute("data-yfeistai-event"),payloadFor(form))}},true);post("interactive.ready",undefined)})();`
}

export function createInteractiveDocumentSource(
  html: string,
  sessionNonce: string,
  frameInstanceId: string
): string {
  if (
    !sessionNonce ||
    sessionNonce.length > 256 ||
    /[\u0000-\u001f\u007f]/.test(sessionNonce) ||
    !/^[A-Za-z0-9._~-]{1,128}$/.test(frameInstanceId)
  ) {
    throw new Error('interactive session nonce is invalid')
  }
  const nonce = cspNonce(`${sessionNonce}\0${frameInstanceId}`)
  const body = sanitizeInteractiveHtml(html)
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; media-src data: blob:; style-src 'unsafe-inline'; script-src 'nonce-${nonce}'; connect-src 'none'; frame-src 'none'; form-action 'none'; navigate-to 'none'; base-uri 'none'">
<script nonce="${nonce}">${controlledBridgeRuntime(sessionNonce, frameInstanceId)}</script>
</head>
<body>${body}</body>
</html>`
}

export interface InteractiveIngressLimiter {
  accept(): boolean
}

export function createInteractiveIngressLimiter(
  maximum: number,
  windowMs: number,
  now: () => number = Date.now
): InteractiveIngressLimiter {
  if (!Number.isSafeInteger(maximum) || maximum < 1 || !Number.isFinite(windowMs) || windowMs <= 0) {
    throw new Error('interactive ingress limit is invalid')
  }
  let windowStartedAt = now()
  let accepted = 0
  return {
    accept() {
      const current = now()
      if (current < windowStartedAt || current - windowStartedAt >= windowMs) {
        windowStartedAt = current
        accepted = 0
      }
      if (accepted >= maximum) return false
      accepted += 1
      return true
    },
  }
}

export interface InteractiveEventQueue {
  readonly pending: number
  canAccept(): boolean
  enqueue(task: () => Promise<void> | void): boolean
  cancel(): void
}

export function createInteractiveEventQueue(
  capacity: number,
  onError: (error: Error) => void
): InteractiveEventQueue {
  if (!Number.isSafeInteger(capacity) || capacity < 1) {
    throw new Error('interactive event queue capacity is invalid')
  }
  let tail: Promise<void> = Promise.resolve()
  let pending = 0
  let cancelled = false
  return {
    get pending() {
      return pending
    },
    canAccept() {
      return !cancelled && pending < capacity
    },
    enqueue(task) {
      if (cancelled || pending >= capacity) return false
      pending += 1
      tail = tail
        .then(() => (cancelled ? undefined : task()))
        .catch(reason => {
          onError(reason instanceof Error ? reason : new Error('interactive event failed'))
        })
        .finally(() => {
          pending -= 1
        })
      return true
    },
    cancel() {
      cancelled = true
    },
  }
}
