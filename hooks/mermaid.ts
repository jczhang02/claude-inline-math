// Mermaid code blocks drawn as Unicode box-drawing art. Pure and synchronous:
// no `$`, no I/O. The drawing itself comes from the vendored beautiful-mermaid
// ASCII renderer (hooks/vendor, see VENDOR.md); this module prepares the
// source for it, measures and checks the result, and splits each row into
// segments by role so the drawer can colour them.
//
// Roles:
// - 'border': box outlines (nodes, actors, subgraphs, chart axes)
// - 'edge': lines, arrowheads, bends and chart series (bars, lines, legend keys)
// - 'label': text written by the diagram's author (node and edge labels, titles)
// - 'dim': chart grid dots
// - 'text': everything else (the spaces between)
//
// Results are memoised by (source, maxColumns), so a redraw costs a lookup;
// a failure that holds at other widths too (a parse error, too big, too
// wide for this width and every narrower one) is memoised by source, so a
// resize does not draw a diagram that cannot fit again.

import { stringWidth } from './layout'
import { renderMermaidASCII } from './vendor/mermaid-ascii.js'

export type DiagramRole = 'text' | 'border' | 'edge' | 'label' | 'dim'
export type DiagramSegment = { text: string; role: DiagramRole }
export type DiagramResult = { ok: true; rows: DiagramSegment[][]; columns: number } | { ok: false; reason: string }

const MAX_SOURCE = 8000
// Drawing time grows fast with size (a 300-edge flowchart takes about a
// second); past these a diagram can't fit 80 rows anyway.
const MAX_SOURCE_LINES = 150
// A drawing this many cells big is not drawn again for a better fit (the
// retries would cost more than they could win). Counted, not timed, so the
// answer for one source and width never depends on the machine's load.
const RETRY_CELLS = 20000
const MAX_ROWS = 80
const MEMO_SIZE = 64
// Node counts past which a flowchart is refused before its layout: the
// layout's time grows fast with nodes in subgraphs (120 of them took 0.6 s)
// and a diagram this big does not fit the terminal anyway.
const MAX_NODES = 100
const MAX_SUBGRAPH_NODES = 60

/** True for a fence info string that names a mermaid block ('mermaid', any case, extra words ignored). */
export function isMermaidInfo(info: string): boolean {
  return /^mermaid$/i.test(info.trim().split(/\s+/)[0] ?? '')
}

const memo = new Map<string, DiagramResult>()

// What a source's failures say about other widths: `always`, a failure no
// width changes; else every width up to `failedAt` fails, and every width
// below `below` too (the layouts draw() would try there are all wider).
type Failed = { always?: string; failedAt: number; narrowest: number; below: number }
const failures = new Map<string, Failed>()

function remember<K, V>(map: Map<K, V>, key: K, value: V) {
  map.delete(key)
  map.set(key, value)
  if (map.size > MEMO_SIZE) {
    map.delete(map.keys().next().value as K)
  }
}

function knownFailure(source: string, maxColumns: number): DiagramResult | undefined {
  const failed = failures.get(source)
  if (!failed) {
    return undefined
  }
  if (failed.always !== undefined) {
    return { ok: false, reason: failed.always }
  }
  if (maxColumns <= failed.failedAt || maxColumns < failed.below) {
    return { ok: false, reason: `diagram is ${failed.narrowest} columns wide, ${maxColumns} available` }
  }

  return undefined
}

/** The diagram as rows of coloured segments, no row wider than maxColumns; else why not. */
export function renderMermaid(source: string, maxColumns: number): DiagramResult {
  const key = `${maxColumns}\n${source}`
  const known = memo.get(key) ?? knownFailure(source, maxColumns)
  if (known) {
    remember(memo, key, known)

    return known
  }
  const outcome = draw(source, maxColumns)
  remember(memo, key, outcome.result)
  if (!outcome.result.ok && outcome.always) {
    remember(failures, source, { always: outcome.result.reason, failedAt: 0, narrowest: 0, below: 0 })
  } else if (!outcome.result.ok && outcome.narrowest !== undefined) {
    const before = failures.get(source)
    remember(failures, source, {
      failedAt: Math.max(maxColumns, before?.failedAt ?? 0),
      narrowest: outcome.narrowest,
      below: Math.max(outcome.below ?? 0, before?.below ?? 0),
    })
  }

  return outcome.result
}

// ---------------------------------------------------------------------------
// Diagram kinds

type Kind = 'flowchart' | 'state' | 'sequence' | 'class' | 'er' | 'xychart'

const KINDS: [RegExp, Kind][] = [
  [/^(flowchart|graph)$/i, 'flowchart'],
  [/^statediagram(-v2)?$/i, 'state'],
  [/^sequencediagram$/i, 'sequence'],
  [/^classdiagram(-v2)?$/i, 'class'],
  [/^erdiagram$/i, 'er'],
  [/^xychart(-beta)?$/i, 'xychart'],
]

function kindOf(word: string): Kind | undefined {
  return KINDS.find(([pattern]) => pattern.test(word))?.[1]
}

// ---------------------------------------------------------------------------
// Source preparation

// Control characters (but '\n' and '\t'), bidi controls and noncharacters
// would break the grid or reach the terminal; the drawing never needs them.
const UNSAFE = /[\x00-\x08\x0b-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩﷐-﷯￾￿]/g
const ODD_SPACE = /[  -   ]/g

// Inline HTML and markdown emphasis: the library's SVG path styles them, its
// text path would print the tags, so they are dropped (as prismantis does).
const INLINE_TAG = /<\/?(?:b|i|u|s|em|strong|del|ins|code|small|sub|sup|span|mark|font|kbd|q)\b[^<>\n]*>/gi

function clean(source: string): string {
  return source.replace(/\r\n?/g, '\n').replace(/\t/g, '    ').replace(UNSAFE, '').replace(ODD_SPACE, ' ')
}

// Front matter (---\n...\n---), %% comments and %%{init}%% directives, blank lines.
function body(source: string): string[] {
  let lines = source.split('\n')
  const first = lines.findIndex(line => line.trim() !== '')
  if (first >= 0 && lines[first]?.trim() === '---') {
    const close = lines.findIndex((line, i) => i > first && line.trim() === '---')
    lines = close > first ? lines.slice(close + 1) : lines
  }

  return lines.filter(line => line.trim() !== '' && !line.trim().startsWith('%%'))
}

function stripMarkup(line: string, kind: Kind): string {
  let out = line.replace(INLINE_TAG, '').replace(/\*\*(?=\S)(.+?)\*\*/g, '$1')
  if (kind !== 'class') {
    out = out.replace(/~~(?=\S)(.+?)~~/g, '$1')
  }
  if (kind === 'flowchart' || kind === 'state' || kind === 'sequence') {
    out = out.replace(/(?<![*\w])\*([^\s*](?:[^*]*[^\s*])?)\*(?![*\w])/g, '$1')
  }

  return out
}

// Split on ';' outside brackets and quotes ("graph TD; A-->B; B-->C;").
function statements(line: string): string[] {
  const out: string[] = []
  let depth = 0
  let quote = false
  let start = 0
  for (let i = 0; i < line.length; i++) {
    const char = line[i]
    if (char === '"') {
      quote = !quote
    } else if (!quote && '([{'.includes(char as string)) {
      depth++
    } else if (!quote && ')]}'.includes(char as string)) {
      depth = Math.max(0, depth - 1)
    } else if (char === ';' && !quote && depth === 0) {
      out.push(line.slice(start, i))
      start = i + 1
    }
  }
  out.push(line.slice(start))

  return out.map(part => part.trim()).filter(part => part !== '')
}

// Arrow operators at a statement's top level, in the forms the library knows.
// An arrow glued to a node id ("A-->B") would be read as part of the id.
const ARROW = /<?(?:-{2,}>|-{3,}|={2,}>|={3,}|-\.+->|-\.+-|--[ox](?=[\s|])|==[ox](?=[\s|])|--(?=\s)|==(?=\s)|-\.(?=\s))/y

function canonicalArrow(arrow: string): string {
  const head = arrow.startsWith('<') ? '<' : ''
  const op = head ? arrow.slice(1) : arrow
  let out = op
  if (/^-{2,}[>ox]$/.test(op)) {
    out = '-->'
  } else if (/^-{3,}$/.test(op)) {
    out = '---'
  } else if (/^={2,}[>ox]$/.test(op)) {
    out = '==>'
  } else if (/^={3,}$/.test(op)) {
    out = '==='
  } else if (/^-\.+->$/.test(op)) {
    out = '-.->'
  } else if (/^-\.+-$/.test(op)) {
    out = '-.-'
  }

  return head + out
}

// A node id the library can't read (it wants [\w-]+; mermaid takes any
// letters) becomes an ASCII one, labelled with itself when it has no shape.
const WORD = /[\p{L}\p{N}\p{M}_]+(?:-[\p{L}\p{N}\p{M}_]+)*/uy
const SHAPE_START = /^\s*[[({>]/

// Spaces arrows out of the node ids beside them, canonicalises them, and
// renames non-ASCII node ids (through `ids`, shared by the whole diagram).
function spaceArrows(statement: string, ids: Map<string, string>): string {
  let out = ''
  let depth = 0
  let quote = false
  let pipe = false
  let label = false // between the halves of "A -- text --> B"
  for (let i = 0; i < statement.length; ) {
    const char = statement[i] as string
    if (char === '"') {
      quote = !quote
    } else if (!quote && !pipe && '([{'.includes(char)) {
      depth++
    } else if (!quote && !pipe && ')]}'.includes(char)) {
      depth = Math.max(0, depth - 1)
    } else if (!quote && depth === 0 && char === '|') {
      pipe = !pipe
    } else if (!quote && !pipe && depth === 0 && (char === '-' || char === '=' || char === '<')) {
      ARROW.lastIndex = i
      const match = ARROW.exec(statement)
      if (match) {
        const arrow = canonicalArrow(match[0])
        label = arrow.length === 2 || (arrow.length === 3 && arrow.startsWith('<'))
        i += match[0].length
        while (statement[i] === ' ') {
          i++
        }
        const next = statement[i]
        out = `${out.trimEnd()} ${arrow}${next === undefined || next === '|' ? '' : ' '}`
        continue
      }
    } else if (!quote && !pipe && !label && depth === 0 && !/[\w-]/.test(statement[i - 1] ?? ' ')) {
      WORD.lastIndex = i
      const word = WORD.exec(statement)?.[0]
      if (word && /[^\x00-\x7f]/.test(word)) {
        let id = ids.get(word)
        if (id === undefined) {
          id = `__n${ids.size}`
          ids.set(word, id)
        }
        i += word.length
        out += SHAPE_START.test(statement.slice(i)) ? id : `${id}[${word}]`
        continue
      }
    }
    out += char
    i++
  }

  return out
}

const FLOW_KEYWORD = /^(subgraph|end|classDef|class|style|linkStyle|click|direction)\b/
// Statements that only style or link a drawing the text renderer does not
// colour: dropped (the library would draw `click A "url"` as a node).
const FLOW_STYLE = /^(classDef|class|style|linkStyle|click)\b/

// Mermaid's entity codes (#quot; #lt; #9829;) as the characters they stand
// for; a quote becomes a typographic one, as a straight one would end the
// quoted label it sits in.
const ENTITY_NAMES: Record<string, string> = { quot: '”', amp: '&', lt: '<', gt: '>', apos: "'", nbsp: ' ', hash: '#' }

function decodeEntities(line: string): string {
  return line.replace(/#([a-z]{2,8}|\d{1,6});/gi, (all, name: string) => {
    if (/^\d+$/.test(name)) {
      const code = Number(name)
      return code === 34 ? '”' : code >= 32 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : all
    }

    return ENTITY_NAMES[name.toLowerCase()] ?? all
  })
}

function prepareFlowchart(lines: string[]): string[] {
  const out: string[] = []
  const ids = new Map<string, string>()
  for (const [index, line] of lines.entries()) {
    for (const statement of statements(line)) {
      if (FLOW_STYLE.test(statement)) {
        continue
      }
      if (index === 0 && out.length === 0) {
        // A bare header means top-down; an unknown direction is left for
        // the library to refuse.
        const header = /^(?:flowchart|graph)(?:\s+(TD|TB|LR|BT|RL))?$/i.exec(statement)
        out.push(header ? `flowchart ${(header[1] ?? 'TD').toUpperCase()}` : statement)
      } else {
        out.push(FLOW_KEYWORD.test(statement) ? statement : spaceArrows(statement, ids))
      }
    }
  }

  return out
}

function prepare(lines: string[], kind: Kind): string {
  // A markdown string label ("`**bold** text`") keeps its text, not its backticks.
  const marked = lines.map(line => decodeEntities(stripMarkup(line, kind).replace(/"`([^`"]*)`"/g, '"$1"')))
  if (kind === 'flowchart') {
    return prepareFlowchart(marked).join('\n')
  }
  // The library matches some headers exactly ("sequenceDiagram" alone on its line).
  const header = (marked[0] as string).trim().replace(/;$/, '')

  return [header, ...marked.slice(1)].join('\n')
}

// ---------------------------------------------------------------------------
// Wide characters
//
// The library sizes text by string length, one cell per UTF-16 unit. Every
// non-ASCII grapheme is swapped for one placeholder letter (one unit, still a
// letter to the parsers), followed by a filler letter when it is two cells
// wide; after drawing, the placeholders turn back and the fillers go. The
// filler idea is prismantis's (U+FDD0 there); letters here keep state names
// in CJK valid for the state parser, which wants \p{L}.

const PLACEHOLDER_FIRST = 0xac00
const FILLER = '힣'
const PLACEHOLDER_LAST = 0xd7a2

const segmenter = typeof Intl !== 'undefined' && 'Segmenter' in Intl ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : undefined

function graphemes(text: string): string[] {
  if (segmenter) {
    return Array.from(segmenter.segment(text), part => part.segment)
  }
  const out: string[] = []
  for (const char of text) {
    if (out.length > 0 && (stringWidth(char) === 0 || out[out.length - 1]?.endsWith('‍'))) {
      out[out.length - 1] += char
    } else {
      out.push(char)
    }
  }

  return out
}

type Glyph = { text: string; width: number }
type Encoded = { text: string; glyphs: Glyph[] }

function encode(text: string): Encoded | undefined {
  const glyphs: Glyph[] = []
  if (/^[\x20-\x7e\n]*$/.test(text)) {
    return { text, glyphs }
  }
  const index = new Map<string, number>()
  let out = ''
  for (const cluster of graphemes(text)) {
    if (/^[\x20-\x7e\n]$/.test(cluster)) {
      out += cluster
      continue
    }
    let at = index.get(cluster)
    if (at === undefined) {
      at = glyphs.length
      if (PLACEHOLDER_FIRST + at > PLACEHOLDER_LAST) {
        return undefined
      }
      const width = stringWidth(cluster)
      // A lone combining mark is shown on a dotted circle, as fonts do.
      glyphs.push(width === 0 ? { text: `◌${cluster}`, width: 1 } : { text: cluster, width: Math.min(width, 2) })
      index.set(cluster, at)
    }
    out += String.fromCharCode(PLACEHOLDER_FIRST + at) + ((glyphs[at] as Glyph).width === 2 ? FILLER : '')
  }

  return { text: out, glyphs }
}

function decodeText(text: string, glyphs: Glyph[]): string {
  let out = ''
  for (const char of text) {
    const code = char.charCodeAt(0)
    if (char === FILLER) {
      continue
    }
    out += code >= PLACEHOLDER_FIRST && code < PLACEHOLDER_FIRST + glyphs.length ? (glyphs[code - PLACEHOLDER_FIRST] as Glyph).text : char
  }

  return out
}

// ---------------------------------------------------------------------------
// Drawing and reading the colours back
//
// The library colours by role. It draws once in truecolor with a theme whose
// colours are sentinels (#000001..#000008), so the escape codes say each
// cell's role; any other colour is a chart series.

const PROBE = {
  fg: '#000001',
  border: '#000002',
  line: '#000003',
  arrow: '#000004',
  corner: '#000005',
  junction: '#000006',
  accent: '#000007',
  bg: '#000008',
}

const SENTINEL: Record<string, DiagramRole> = {
  '38;2;0;0;1': 'label',
  '38;2;0;0;2': 'border',
  '38;2;0;0;3': 'edge',
  '38;2;0;0;4': 'edge',
  '38;2;0;0;5': 'edge',
  '38;2;0;0;6': 'border',
}

const SGR = /\x1b\[([\d;]*)m/g

type Cell = { text: string; width: number; role: DiagramRole }

function sgrRole(code: string): DiagramRole | undefined {
  if (code === '' || code === '0' || code === '39') {
    return undefined
  }

  return SENTINEL[code] ?? (code.startsWith('38;') ? 'edge' : undefined)
}

// One drawn row as cells; undefined when a wide character lost its filler
// (the library cut a label), which would shift the rest of the row.
function readRow(line: string, glyphs: Glyph[]): Cell[] | undefined {
  const cells: Cell[] = []
  let role: DiagramRole | undefined
  let at = 0
  let owed = false
  const take = (text: string): boolean => {
    for (const char of text) {
      const code = char.charCodeAt(0)
      if (char === FILLER) {
        if (!owed) {
          return false
        }
        owed = false
        continue
      }
      if (owed) {
        return false
      }
      if (code >= PLACEHOLDER_FIRST && code < PLACEHOLDER_FIRST + glyphs.length) {
        const glyph = glyphs[code - PLACEHOLDER_FIRST] as Glyph
        cells.push({ text: glyph.text, width: glyph.width, role: role ?? 'label' })
        owed = glyph.width === 2
      } else if (char === ' ') {
        cells.push({ text: ' ', width: 1, role: 'text' })
      } else {
        cells.push({ text: char, width: 1, role: char === '·' ? 'dim' : (role ?? 'text') })
      }
    }

    return true
  }
  for (const match of line.matchAll(SGR)) {
    if (!take(line.slice(at, match.index))) {
      return undefined
    }
    role = sgrRole(match[1] ?? '')
    at = match.index + match[0].length
  }

  return take(line.slice(at)) && !owed ? cells : undefined
}

function isBlank(row: Cell[]): boolean {
  return row.every(cell => cell.text === ' ')
}

// A row of nothing but vertical lines only stretches them; dropping it keeps
// the drawing short without breaking a line.
function isStretch(row: Cell[]): boolean {
  return row.every(cell => cell.text === ' ' || cell.text === '│')
}

// Adjacent cells of one role make one segment; spaces between two runs of
// the same role join them (a label "读取 文件" stays one segment).
function segments(row: Cell[]): DiagramSegment[] {
  const out: DiagramSegment[] = []
  for (const cell of row) {
    const last = out[out.length - 1]
    if (last && last.role === cell.role) {
      last.text += cell.text
      continue
    }
    const before = out[out.length - 2]
    if (last && before && before.role === cell.role && last.role === 'text' && /^ +$/.test(last.text)) {
      before.text += last.text + cell.text
      out.pop()
      continue
    }
    out.push({ text: cell.text, role: cell.role })
  }

  return out
}

type Art = { rows: DiagramSegment[][]; columns: number } | { reason: string }

// The library's own refusals are plain Errors; a TypeError or RangeError is
// a bug of its layout (a cycle into a subgraph, for one), not of the source.
function shortError(error: unknown, glyphs: Glyph[]): string {
  if (error instanceof TypeError || error instanceof RangeError) {
    return 'diagram renderer failed on this diagram'
  }
  const message = decodeText(error instanceof Error ? error.message : String(error), glyphs).split('\n')[0]?.trim() ?? ''
  const cut = message.length > 80 ? `${message.slice(0, 79)}…` : message

  return cut ? `diagram could not be parsed: ${cut}` : 'diagram could not be parsed'
}

function art(encoded: Encoded, kind: Kind, paddingX: number): Art {
  let drawn: string
  try {
    drawn = renderMermaidASCII(encoded.text, { useAscii: false, colorMode: 'truecolor', theme: PROBE, paddingX, paddingY: 1 })
  } catch (error) {
    return { reason: shortError(error, encoded.glyphs) }
  }
  let rows: Cell[][] = []
  for (const line of drawn.split('\n')) {
    const row = readRow(line, encoded.glyphs)
    if (!row) {
      return { reason: 'diagram cut a wide character in half' }
    }
    while (row.length > 0 && (row[row.length - 1] as Cell).text === ' ') {
      row.pop()
    }
    rows.push(row)
  }
  rows = rows.filter((row, i) => !isBlank(row) && (kind === 'xychart' || !isStretch(row) || rows[i - 1]?.some(cell => cell.text === '●')))
  if (rows.length === 0) {
    return { reason: 'diagram is empty or could not be parsed' }
  }
  const indent = Math.min(...rows.map(row => row.findIndex(cell => cell.text !== ' ')))
  const columns = Math.max(...rows.map(row => row.slice(indent).reduce((sum, cell) => sum + cell.width, 0)))

  return { rows: rows.map(row => segments(row.slice(indent))), columns }
}

// ---------------------------------------------------------------------------
// Checks on a flowchart: its size before layout, and whether the drawing
// kept every edge

// The node ids of a prepared flowchart's statements: (all, in subgraphs).
function countNodes(prepared: string): { all: number; inSubgraphs: number } {
  const all = new Set<string>()
  const nested = new Set<string>()
  let depth = 0
  for (const statement of prepared.split('\n').slice(1)) {
    if (/^subgraph\b/.test(statement)) {
      depth++
      continue
    }
    if (/^end\b/.test(statement)) {
      depth = Math.max(0, depth - 1)
      continue
    }
    if (FLOW_KEYWORD.test(statement)) {
      continue
    }
    let bare = statement.replace(/"[^"]*"/g, '').replace(/\|[^|]*\|/g, '').replace(/:::[\w-]+/g, '')
    // Edge text ("A -- yes --> B") and shapes with their labels go first.
    bare = bare.replace(/ (?:--|==|-\.) .*? (<?-->|<?==>|<?-\.->|---|===|-\.-)(?= |$)/g, ' $1')
    for (let before = ''; before !== bare; ) {
      before = bare
      bare = bare.replace(/\[[^[\]]*\]|\([^()]*\)|\{[^{}]*\}|(?<=\w)>[^\]]*\]/g, '')
    }
    for (const id of bare.match(/(?<![\w-])[A-Za-z_][\w]*(?:-[\w]+)*/g) ?? []) {
      all.add(id)
      if (depth > 0) {
        nested.add(id)
      }
    }
  }

  return { all: all.size, inSubgraphs: nested.size }
}

// The directed edges of a prepared flowchart (`-->`, `==>`, `-.->`; `A & B`
// on either side) or state diagram (`X --> Y`, `[*]` left out).
function edges(prepared: string, kind: Kind): [string, string][] {
  const found: [string, string][] = []
  for (const statement of prepared.split('\n').slice(1)) {
    if (kind === 'state') {
      const match = /^\s*([^\s:]+)\s*-->\s*([^\s:]+)/.exec(statement)
      if (match && match[1] !== '[*]' && match[2] !== '[*]') {
        found.push([match[1] as string, match[2] as string])
      }
      continue
    }
    if (FLOW_KEYWORD.test(statement)) {
      continue
    }
    // As in countNodes: labels, edge text and shapes go, leaving ids and arrows.
    let bare = statement.replace(/"[^"]*"/g, '').replace(/\|[^|]*\|/g, '').replace(/:::[\w-]+/g, '')
    bare = bare.replace(/ (?:--|==|-\.) .*? (<?-->|<?==>|<?-\.->|---|===|-\.-)(?= |$)/g, ' $1')
    for (let before = ''; before !== bare; ) {
      before = bare
      bare = bare.replace(/\[[^[\]]*\]|\([^()]*\)|\{[^{}]*\}|(?<=\w)>[^\]]*\]/g, '')
    }
    const parts = bare.split(/\s*(<?-->|<?==>|<?-\.->|---|===|-\.-)\s*/)
    const ids = (part: string) => part.match(/(?<![\w-])[A-Za-z_][\w]*(?:-[\w]+)*/g) ?? []
    for (let i = 1; i + 1 < parts.length; i += 2) {
      if (['-->', '==>', '-.->'].includes(parts[i] as string)) {
        for (const from of ids(parts[i - 1] as string)) {
          for (const to of ids(parts[i + 1] as string)) {
            found.push([from, to])
          }
        }
      }
    }
  }

  return found
}

/**
 * Does the diagram have a loop (a retry, a training loop, A --> A)? The
 * library draws every back edge wrongly: over another edge, with its label
 * on the wrong edge, without arrowheads, or through a box.
 */
function hasLoop(prepared: string, kind: Kind): boolean {
  const next = new Map<string, string[]>()
  for (const [from, to] of edges(prepared, kind)) {
    next.set(from, [...(next.get(from) ?? []), to])
  }
  const state = new Map<string, 'open' | 'done'>()
  const visit = (node: string): boolean => {
    state.set(node, 'open')
    for (const to of next.get(node) ?? []) {
      if (state.get(to) === 'open' || (!state.has(to) && visit(to))) {
        return true
      }
    }
    state.set(node, 'done')
    return false
  }

  return [...next.keys()].some(node => !state.has(node) && visit(node))
}

const HEADS = /[▲▼◄►◥◤◢◣]/g

// The distinct nodes that edges with a head point at (after `-->`, `==>`,
// `-.->`; `A & B` both): the drawing needs at least one head for each.
function targets(prepared: string): number {
  const found = new Set<string>()
  for (const statement of prepared.split('\n').slice(1)) {
    if (FLOW_KEYWORD.test(statement)) {
      continue
    }
    const bare = statement.replace(/"[^"]*"/g, '""')
    for (const match of bare.matchAll(/(?:-->|==>|-\.->)(?:\|[^|]*\|)?\s*((?:[\w-]+\s*&\s*)*[\w-]+)/g)) {
      for (const id of (match[1] as string).split('&')) {
        found.add(id.trim())
      }
    }
  }

  return found.size
}

/**
 * Did the library drop or cut an edge? (It draws a back edge of a left-right
 * loop over another edge, and an edge leaving the bottom row of its canvas
 * is cut off.) Too few arrowheads for the nodes they point at, or a line
 * that leaves the drawing's outer edge.
 */
function lostEdge(prepared: string, rows: DiagramSegment[][]): boolean {
  let heads = 0
  for (const row of rows) {
    for (const segment of row) {
      if (segment.role === 'edge') {
        heads += segment.text.match(HEADS)?.length ?? 0
      }
    }
  }
  if (heads < targets(prepared)) {
    return true
  }
  // Nothing is drawn above the first row or below the last: a line that
  // leaves the canvas there (a box's ┴ or ┬ connector) was cut off.
  const first = rows[0]?.map(segment => segment.text).join('') ?? ''
  const last = rows[rows.length - 1]?.map(segment => segment.text).join('') ?? ''

  return first.includes('┴') || last.includes('┬')
}

type Drawn = { result: DiagramResult; always?: boolean; narrowest?: number; below?: number }

function fail(reason: string, always: boolean): Drawn {
  return { result: { ok: false, reason }, always }
}

function draw(source: string, maxColumns: number): Drawn {
  if (source.length > MAX_SOURCE) {
    return fail(`diagram source is over ${MAX_SOURCE} characters`, true)
  }
  const lines = body(clean(source))
  if (lines.length === 0) {
    return fail('diagram is empty', true)
  }
  if (lines.length > MAX_SOURCE_LINES) {
    return fail(`diagram source is over ${MAX_SOURCE_LINES} lines`, true)
  }
  const word = (lines[0] as string).trim().split(/[\s;]/)[0] ?? ''
  const kind = kindOf(word)
  if (!kind) {
    return fail(`${word.slice(0, 40)} diagrams are not supported`, true)
  }
  const prepared = prepare(lines, kind)
  if ((kind === 'flowchart' || kind === 'state') && hasLoop(prepared, kind)) {
    return fail('diagram has a loop, which the text renderer cannot draw', true)
  }
  if (kind === 'flowchart') {
    const nodes = countNodes(prepared)
    if (nodes.all > MAX_NODES) {
      return fail(`diagram has ${nodes.all} nodes, more than ${MAX_NODES}`, true)
    }
    if (nodes.inSubgraphs > MAX_SUBGRAPH_NODES) {
      return fail(`diagram has ${nodes.inSubgraphs} nodes in subgraphs, more than ${MAX_SUBGRAPH_NODES}`, true)
    }
  }
  // Wider than the space: try top-down for a left-right flowchart, then
  // tighter spacing. A layout that lost an edge is passed over too.
  const variants = [prepared]
  const sideways = /^flowchart (LR|RL)$/m
  if (kind === 'flowchart' && sideways.test(prepared)) {
    variants.push(prepared.replace(sideways, 'flowchart TD'))
  }
  let tallest = 0
  let narrowest = Infinity
  let cells = 0
  let tried = 0
  let isLost = false
  let isCut = false // tries left out by RETRY_CELLS, which no width changes
  for (const paddingX of [3, 1]) {
    for (const variant of variants) {
      if (cells > RETRY_CELLS || (paddingX === 1 && narrowest * 0.7 > maxColumns)) {
        isCut ||= cells > RETRY_CELLS
        break
      }
      tried++
      const encoded = encode(variant)
      if (!encoded) {
        return fail('diagram has too many different characters', true)
      }
      const found = art(encoded, kind, paddingX)
      if ('reason' in found) {
        return fail(found.reason, true)
      }
      cells = Math.max(cells, found.rows.length * found.columns)
      if (found.rows.length > MAX_ROWS) {
        tallest = tallest === 0 ? found.rows.length : Math.min(tallest, found.rows.length)
      } else if (found.columns > maxColumns) {
        narrowest = Math.min(narrowest, found.columns)
      } else if (kind === 'flowchart' && lostEdge(variant, found.rows)) {
        isLost = true
      } else {
        return { result: { ok: true, rows: found.rows, columns: found.columns } }
      }
    }
  }
  if (isLost) {
    // Not remembered by source: a wider space may allow a layout that keeps every edge.
    return { result: { ok: false, reason: 'diagram renderer dropped or cut an edge of this diagram' } }
  }

  // Below `below` draw() tries the same layouts or fewer, all too wide: with
  // every layout tried, or tries cut by size, that is `narrowest`; with the
  // tighter spacing skipped for being hopeless, 0.7 of it.
  const below = tried === 2 * variants.length || isCut ? narrowest : Math.floor(narrowest * 0.7)

  return narrowest < Infinity
    ? { result: { ok: false, reason: `diagram is ${narrowest} columns wide, ${maxColumns} available` }, narrowest, below }
    : fail(`diagram is ${tallest} rows tall, limit ${MAX_ROWS}`, true)
}
