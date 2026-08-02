import { parseFragment } from 'parse5'

import type {
  ImageElementFilters,
  PPTElement,
  PPTElementOutline,
  PPTElementShadow,
  Slide,
  TableCellStyle,
} from '@openmaic/dsl'

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

const MAX_RENDERER_HTML_LENGTH = 1_000_000

const ALLOWED_TAGS = new Set([
  'p',
  'div',
  'span',
  'br',
  'b',
  'strong',
  'i',
  'em',
  'u',
  's',
  'sub',
  'sup',
  'ul',
  'ol',
  'li',
  'blockquote',
  'code',
  'pre',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'math',
  'semantics',
  'annotation',
  'mrow',
  'mi',
  'mn',
  'mo',
  'mtext',
  'ms',
  'mspace',
  'msup',
  'msub',
  'msubsup',
  'mfrac',
  'msqrt',
  'mroot',
  'mover',
  'munder',
  'munderover',
  'mtable',
  'mtr',
  'mtd',
  'svg',
  'g',
  'path',
  'rect',
  'line',
  'polyline',
  'polygon',
  'circle',
  'ellipse',
])

const DROP_WITH_CONTENT = new Set([
  'script',
  'style',
  'iframe',
  'object',
  'embed',
  'template',
  'noscript',
])

const SAFE_STYLE: Readonly<Record<string, RegExp>> = {
  color: /^(?:#[0-9a-f]{3,8}|(?:rgb|hsl)a?\([\d\s.,%+-]+\)|[a-z]+)$/i,
  'background-color': /^(?:#[0-9a-f]{3,8}|(?:rgb|hsl)a?\([\d\s.,%+-]+\)|[a-z]+)$/i,
  'font-size': /^\d+(?:\.\d+)?(?:px|pt|em|rem|%)$/i,
  'font-family': /^[^;{}()<>\\]+$/,
  'font-weight': /^(?:normal|bold|[1-9]00)$/i,
  'font-style': /^(?:normal|italic|oblique)$/i,
  'text-decoration': /^(?:none|underline|line-through)(?:\s+(?:solid|double|dotted|dashed))?$/i,
  'text-align': /^(?:left|right|center|justify)$/i,
  'line-height': /^\d+(?:\.\d+)?(?:px|pt|em|rem|%)?$/i,
  'letter-spacing': /^-?\d+(?:\.\d+)?(?:px|pt|em|rem)$/i,
  'vertical-align': /^(?:baseline|sub|super|top|middle|bottom)$/i,
}

const SAFE_ATTRIBUTE: Readonly<Record<string, RegExp>> = {
  class: /^[A-Za-z0-9 _:-]+$/,
  id: /^[A-Za-z][A-Za-z0-9_.:-]*$/,
  role: /^[a-z]+$/i,
  dir: /^(?:ltr|rtl|auto)$/i,
  lang: /^[A-Za-z0-9-]+$/,
  'aria-hidden': /^(?:true|false)$/,
  xmlns: /^http:\/\/www\.w3\.org\/(?:1998\/Math\/MathML|2000\/svg)$/,
  width: /^\d+(?:\.\d+)?(?:px|em|rem|%)?$/i,
  height: /^\d+(?:\.\d+)?(?:px|em|rem|%)?$/i,
  viewbox: /^-?[\d.]+(?:[ ,]+-?[\d.]+){3}$/i,
  d: /^[A-Za-z\d\s.,+\-]+$/,
  x: /^-?\d+(?:\.\d+)?(?:%)?$/,
  y: /^-?\d+(?:\.\d+)?(?:%)?$/,
  x1: /^-?\d+(?:\.\d+)?(?:%)?$/,
  y1: /^-?\d+(?:\.\d+)?(?:%)?$/,
  x2: /^-?\d+(?:\.\d+)?(?:%)?$/,
  y2: /^-?\d+(?:\.\d+)?(?:%)?$/,
  cx: /^-?\d+(?:\.\d+)?(?:%)?$/,
  cy: /^-?\d+(?:\.\d+)?(?:%)?$/,
  r: /^\d+(?:\.\d+)?(?:%)?$/,
  rx: /^\d+(?:\.\d+)?(?:%)?$/,
  ry: /^\d+(?:\.\d+)?(?:%)?$/,
  points: /^[\d\s.,+\-]+$/,
  fill: /^(?:none|currentColor|#[0-9a-f]{3,8}|[a-z]+)$/i,
  stroke: /^(?:none|currentColor|#[0-9a-f]{3,8}|[a-z]+)$/i,
  'stroke-width': /^\d+(?:\.\d+)?$/,
  transform: /^[A-Za-z\d\s.,()+\-]+$/,
  display: /^(?:block|inline)$/i,
  mathvariant: /^[a-z-]+$/i,
  encoding: /^(?:application\/x-tex|text\/plain)$/i,
}

function escapeText(value: string): string {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
}

function escapeAttribute(value: string): string {
  return escapeText(value).replaceAll('"', '&quot;')
}

function sanitizedStyle(value: string): string | null {
  const declarations: string[] = []
  value.split(';').forEach(declaration => {
    const separator = declaration.indexOf(':')
    if (separator < 1) return
    const property = declaration.slice(0, separator).trim().toLowerCase()
    const candidate = declaration.slice(separator + 1).trim()
    const pattern = SAFE_STYLE[property]
    if (pattern?.test(candidate)) declarations.push(`${property}:${candidate}`)
  })
  return declarations.length > 0 ? declarations.join(';') : null
}

function sanitizedAttributes(attributes: readonly HtmlAttribute[]): string {
  const output: string[] = []
  attributes.forEach(attribute => {
    const name = attribute.name.toLowerCase()
    if (name === 'style') {
      const style = sanitizedStyle(attribute.value)
      if (style) output.push(`style="${escapeAttribute(style)}"`)
      return
    }
    const pattern = SAFE_ATTRIBUTE[name]
    if (pattern?.test(attribute.value)) {
      const renderedName = name === 'viewbox' ? 'viewBox' : name
      output.push(`${renderedName}="${escapeAttribute(attribute.value)}"`)
    }
  })
  return output.length > 0 ? ` ${output.join(' ')}` : ''
}

function renderNode(node: HtmlNode): string {
  if (node.nodeName === '#text') return escapeText(node.value ?? '')
  if (node.nodeName === '#comment') return ''
  const tag = node.tagName?.toLowerCase()
  const children = (node.childNodes ?? []).map(renderNode).join('')
  if (!tag) return children
  if (DROP_WITH_CONTENT.has(tag)) return ''
  if (!ALLOWED_TAGS.has(tag)) return children
  const attributes = sanitizedAttributes(node.attrs ?? [])
  if (tag === 'br') return `<br${attributes}>`
  return `<${tag}${attributes}>${children}</${tag}>`
}

export function sanitizeRendererHtml(input: string): string {
  if (input.length > MAX_RENDERER_HTML_LENGTH) {
    throw new Error('renderer HTML exceeds the supported size')
  }
  const fragment = parseFragment(input) as unknown as HtmlNode
  return (fragment.childNodes ?? []).map(renderNode).join('')
}

function safeColor(value: unknown, fallback?: string): string | undefined {
  if (value === undefined) return fallback
  return typeof value === 'string' &&
    /^(?:#[0-9a-f]{3,8}|(?:rgb|hsl)a?\([\d\s.,%+-]+\)|[a-z]+)$/i.test(value.trim())
    ? value.trim()
    : fallback
}

function safeOutline(outline: PPTElementOutline | undefined): PPTElementOutline | undefined {
  if (!outline) return undefined
  return { ...outline, color: safeColor(outline.color) }
}

function safeShadow(shadow: PPTElementShadow | undefined): PPTElementShadow | undefined {
  if (!shadow) return undefined
  const color = safeColor(shadow.color)
  if (
    !color ||
    !Number.isFinite(shadow.h) ||
    !Number.isFinite(shadow.v) ||
    !Number.isFinite(shadow.blur)
  ) {
    return undefined
  }
  return { ...shadow, color }
}

function safeMediaUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length === 0 || value.length > 1_000_000) return undefined
  if (/^\/api\/v1\/classrooms\/versions\/[A-Za-z0-9._~%-]+\/media\/[A-Za-z0-9._~%-]+$/.test(value)) {
    return value
  }
  if (/^blob:[A-Za-z0-9+.-]+:\/\/[^\s"'<>]+$/.test(value)) return value
  if (/^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$/i.test(value)) return value
  return undefined
}

const FILTER_PATTERNS: Readonly<Record<keyof ImageElementFilters, RegExp>> = {
  blur: /^\d+(?:\.\d+)?(?:px)?$/i,
  brightness: /^\d+(?:\.\d+)?(?:%)?$/,
  contrast: /^\d+(?:\.\d+)?(?:%)?$/,
  grayscale: /^\d+(?:\.\d+)?(?:%)?$/,
  saturate: /^\d+(?:\.\d+)?(?:%)?$/,
  'hue-rotate': /^-?\d+(?:\.\d+)?(?:deg)?$/i,
  sepia: /^\d+(?:\.\d+)?(?:%)?$/,
  invert: /^\d+(?:\.\d+)?(?:%)?$/,
  opacity: /^\d+(?:\.\d+)?(?:%)?$/,
}

function safeImageFilters(filters: ImageElementFilters | undefined): ImageElementFilters | undefined {
  if (!filters) return undefined
  const sanitized: ImageElementFilters = {}
  for (const key of Object.keys(FILTER_PATTERNS) as Array<keyof ImageElementFilters>) {
    const value = filters[key]
    if (typeof value === 'string' && FILTER_PATTERNS[key].test(value.trim())) {
      sanitized[key] = value.trim()
    }
  }
  return Object.keys(sanitized).length > 0 ? sanitized : undefined
}

function safeCellStyle(style: TableCellStyle | undefined): TableCellStyle | undefined {
  if (!style) return undefined
  return {
    ...style,
    color: safeColor(style.color),
    backcolor: safeColor(style.backcolor),
  }
}

export function sanitizeRendererElement(element: PPTElement): PPTElement {
  if (element.type === 'text') {
    return {
      ...element,
      content: sanitizeRendererHtml(element.content),
      defaultColor: safeColor(element.defaultColor, '#333333')!,
      outline: safeOutline(element.outline),
      fill: safeColor(element.fill),
      shadow: safeShadow(element.shadow),
    }
  }
  if (element.type === 'image') {
    return {
      ...element,
      src: safeMediaUrl(element.src) ?? '',
      outline: safeOutline(element.outline),
      filters: safeImageFilters(element.filters),
      shadow: safeShadow(element.shadow),
      colorMask: safeColor(element.colorMask),
    }
  }
  if (element.type === 'shape') {
    return {
      ...element,
      fill: safeColor(element.fill, '#5b9bd5')!,
      outline: safeOutline(element.outline),
      shadow: safeShadow(element.shadow),
      pattern: safeMediaUrl(element.pattern),
      gradient: element.gradient
        ? {
            ...element.gradient,
            colors: element.gradient.colors.map(color => ({
              ...color,
              color: safeColor(color.color, '#5b9bd5')!,
            })),
          }
        : undefined,
      text: element.text
        ? {
            ...element.text,
            content: sanitizeRendererHtml(element.text.content),
            defaultColor: safeColor(element.text.defaultColor, '#333333')!,
          }
        : undefined,
    }
  }
  if (element.type === 'table') {
    if (!Array.isArray(element.data)) throw new Error('renderer table data is invalid')
    return {
      ...element,
      outline: safeOutline(element.outline) ?? {},
      theme: element.theme
        ? { ...element.theme, color: safeColor(element.theme.color, '#2563eb')! }
        : undefined,
      data: element.data.map(row =>
        row.map(cell => ({
          ...cell,
          text: sanitizeRendererHtml(cell.text),
          style: safeCellStyle(cell.style),
          borders: cell.borders
            ? Object.fromEntries(
                Object.entries(cell.borders).flatMap(([side, border]) => {
                  const color = safeColor(border?.color)
                  return color && border ? [[side, { ...border, color }]] : []
                })
              )
            : undefined,
        }))
      ),
    }
  }
  if (element.type === 'latex') {
    return {
      ...element,
      color: safeColor(element.color),
      html: element.html ? sanitizeRendererHtml(element.html) : undefined,
    }
  }
  if (element.type === 'line') {
    return {
      ...element,
      color: safeColor(element.color, '#333333')!,
      shadow: safeShadow(element.shadow),
    }
  }
  if (element.type === 'chart') {
    if (!Array.isArray(element.themeColors)) {
      throw new Error('renderer chart theme colors are invalid')
    }
    return {
      ...element,
      fill: safeColor(element.fill),
      themeColors: element.themeColors.map(color => safeColor(color, '#2563eb')!),
      textColor: safeColor(element.textColor),
      lineColor: safeColor(element.lineColor),
      outline: safeOutline(element.outline),
    }
  }
  if (element.type === 'audio') {
    return {
      ...element,
      color: safeColor(element.color, '#333333')!,
      src: safeMediaUrl(element.src) ?? '',
    }
  }
  if (element.type === 'video') {
    return {
      ...element,
      src: safeMediaUrl(element.src),
      poster: safeMediaUrl(element.poster),
    }
  }
  return { ...element }
}

export function sanitizeRendererSlide(slide: Slide): Slide {
  if (!Array.isArray(slide.elements) || !Array.isArray(slide.theme?.themeColors)) {
    throw new Error('renderer slide structure is invalid')
  }
  return {
    ...slide,
    theme: {
      ...slide.theme,
      backgroundColor: safeColor(slide.theme.backgroundColor, '#ffffff')!,
      themeColors: slide.theme.themeColors.map(color => safeColor(color, '#2563eb')!),
      fontColor: safeColor(slide.theme.fontColor, '#333333')!,
      outline: safeOutline(slide.theme.outline),
      shadow: safeShadow(slide.theme.shadow),
    },
    background: slide.background
      ? {
          ...slide.background,
          color: safeColor(slide.background.color),
          image: slide.background.image
            ? (() => {
                const src = safeMediaUrl(slide.background?.image?.src)
                return src ? { ...slide.background!.image!, src } : undefined
              })()
            : undefined,
          gradient: slide.background.gradient
            ? {
                ...slide.background.gradient,
                colors: (Array.isArray(slide.background.gradient.colors)
                  ? slide.background.gradient.colors
                  : []
                ).map(color => ({
                  ...color,
                  color: safeColor(color.color, '#ffffff')!,
                })),
              }
            : undefined,
        }
      : undefined,
    elements: slide.elements.map(sanitizeRendererElement),
  }
}
