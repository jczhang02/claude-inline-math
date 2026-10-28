import type { Elements, Register, RenderElement } from 'claude-code'

import { layoutParagraph, mathRefs, parseMessage, stringWidth, toUnicode } from './layout'
import type { Block, Line, MathRef, PictureState, Run } from './layout'
import { renderMermaid } from './mermaid'
import type { DiagramRole, DiagramSegment } from './mermaid'
import { check, clear, configure, flush, lookup, noteDiagram, onProblem, prefetch, readSettings, runWorker, startPolling, stats, theme, warmUp } from './render-client'
import type { Health, Settings } from './render-client'

// inline-math: draws the LaTeX in Claude's replies as typeset pictures, inline
// with the terminal text and on its baseline, and ```mermaid blocks as
// Unicode diagrams. Blocks without either go to the surface's own Markdown
// element; paragraphs with formulas are wrapped here, line by line
// (layout.ts), since a picture is a box of whole cells that no Text flows
// around. Pictures come from the backend (bin/client.py and its daemon)
// through render-client.ts; a draw never waits for them.
//
// Cost: every redraw request runs this hook for every reply on screen, so a
// reply whose pictures are all ready and unchanged returns the tree it drew
// last time (checked formula by formula, a map lookup each). A reply still
// waiting reads its member of the `redraw` state family, and is drawn again
// when a batch it waited for lands; no other reply is.
//
// While a reply streams, the engine draws its text itself; a turn.step hook
// watches the text go by (passing every chunk on unchanged) and queues each
// formula as soon as it is closed, so its picture is ready when the
// finished block is drawn.

const COMMAND = 'inline-math'
const STORE_ENABLED = 'enabled'

// The engine draws an assistant block as one row: a 2-cell column holding the
// reply bullet (only on the block that opens a reply), then the markdown.
// Measured at 80 columns: the bullet at column 0, text from column 2, lines
// wrapped at 78. A hook's tree replaces that whole row, so the tree draws the
// bullet itself, and a block without it starts at column 0, as the engine's.
const BULLET_GUTTER = 2
// A command's output row: a dim "  ⎿  " (5 cells), then the text.
const OUTPUT_PREFIX = '  ⎿  '
const OUTPUT_GUTTER = 5
// The engine's reply bullet: U+23FA on macOS, U+25CF elsewhere.
const BULLET_MAC = '⏺'
const BULLET_OTHER = '●'
// The theme key the engine's Markdown draws inline code in.
const CODE_COLOR = 'permission'
// Diagram colours: Catppuccin blue for boxes, lavender for lines and arrows,
// in the flavour that matches the formulas' colour (Latte on a light
// background, Mocha on a dark one); labels in the body text colour.
const DIAGRAM_COLORS: Record<'light' | 'dark', { border: string; edge: string }> = {
  light: { border: '#1e66f5', edge: '#7287fd' },
  dark: { border: '#89b4fa', edge: '#b4befe' },
}
const DIAGRAM_ROWS = 80
// Borders and edges while the light or dark scheme is not known yet: a theme key.
const DIAGRAM_FALLBACK = 'suggestion'

// The state family a drawn reply reads, so a landed batch redraws it alone.
const REDRAW = { plugin: 'inline-math', key: 'redraw' } as const

// Memo bounds: trees by drawn site, parses by text. Above the replies of a
// long session, as one redraw of the whole transcript must find them all
// (a least-recently-used memo smaller than one pass evicts every entry
// before it is asked again). Entries are small: trees share their pictures.
const DRAWN_LIMIT = 5000
const PLAN_LIMIT = 5000

// Streaming prerender: how often a streaming block is read again at most,
// how much of it is watched, and the hook budget (ms) kept in reserve.
const SCAN_MS = 100
const SCAN_SHARE = 20 // a scan costing c ms (wall clock) waits at least c * SCAN_SHARE ms for the next
const SCAN_LIMIT = 60_000
const SCAN_RESERVE_MS = 5_000
const SCAN_TRIGGER = /\$|\\[)\]]|```|~~~/

const PROMPT_HINT = [
  '# Math and diagrams in replies',
  'This terminal typesets LaTeX: write $...$ inline (no space just inside the $) and $$...$$ on lines of their own for display.',
  'It also draws ```mermaid blocks (flowchart, sequence, state, class, ER) as diagrams.',
  'Never put math in code spans or code blocks.',
].join('\n')

const DEMO = [
  "**Demo.** Euler's identity $e^{i\\pi} + 1 = 0$ links $e$, $i$ and $\\pi$. For samples $x_i \\in \\mathbb{R}$ the mean is $\\bar{x} = \\frac{1}{n}\\sum_{i=1}^{n} x_i$ and the variance $\\sigma^2 = \\mathbb{E}[(X-\\mu)^2]$.",
  'A tall formula makes only its own line taller: $\\sqrt{\\frac{x^2+1}{y}}$, $\\binom{n}{k}$ and $\\int_0^1 f(x)\\,dx$ sit on the text baseline, and the lines around them keep one row.',
  '$$\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}$$',
  '中文混排: 设 $f(x) = x^2$, 则导数 $f\'(x) = 2x$, 且 $\\lim_{h \\to 0} \\frac{f(x+h) - f(x)}{h} = f\'(x)$. 中文字符占两格, 换行照样对齐.',
  '- A list item with $\\alpha + \\beta = \\gamma$ wraps under its own text, not under the marker, when the line is long enough to need it.\n- **Bold text with $E = mc^2$ inside** and `inline code` keep their styles.',
  'Prices like $5 and $10 stay text, and `$x$` in code stays code.',
  'A reply may define its own commands: $\\newcommand{\\R}{\\mathbb{R}}\\DeclareMathOperator{\\tr}{tr}$ then $\\tr A \\in \\R$ (the definition itself draws as nothing).',
  '```mermaid\nflowchart LR\n  A[回复 reply] --> B{formula?}\n  B -->|yes| C[picture]\n  B -->|no| D[text]\n```',
].join('\n\n')

const USAGE = [
  `/${COMMAND} status   backend health, settings, cache`,
  `/${COMMAND} on|off   draw formulas or leave replies as the engine draws them (kept across sessions)`,
  `/${COMMAND} demo     show a sample reply with formulas`,
  `/${COMMAND} clear    forget the drawn formulas and draw them again`,
].join('\n')

type Table = Elements['terminal']
type Frame = { bullet?: string; prefix?: string; width: number; marginTop: number }

let settings: Settings = readSettings({})
let enabled: boolean | undefined // the /inline-math on|off choice; undefined: settings.enabled
let bulletGlyph = BULLET_OTHER
let maxProseWidth: number | undefined
let lastColumns: number | undefined // the viewport width replies were last drawn at
let warned = false

const isEnabled = () => enabled ?? settings.enabled

// ---------------------------------------------------------------------------
// Drawing

// What a picture says where the terminal cannot draw it (no graphics, tmux
// without passthrough): the Unicode stand-in, cut to the picture's own
// columns so the line it sits in keeps the width it was laid out for.
function altFor(latex: string, columns: number): string {
  const text = toUnicode(latex).replace(/\s+/g, ' ').trim()
  if (stringWidth(text) <= columns) return text || ' '
  let cut = ''
  for (const char of text) {
    if (stringWidth(cut + char) > columns - 1) break
    cut += char
  }

  return `${cut}\u2026`
}

// A Link's address may be 2048 characters once percent-encoded; a longer
// one would refuse the whole tree, so its text is drawn plain.
function linkFits(href: string): boolean {
  try {
    return encodeURI(href).length <= 2048
  } catch {
    return false
  }
}

function textRun(t: Table, run: Extract<Run, { kind: 'text' }>): RenderElement {
  const { Box, Text, Link } = t
  const { style } = run
  const text = (
    <Text
      weight={style.bold ? 'bold' : undefined}
      italic={style.italic}
      strikethrough={style.strike}
      color={style.code ? CODE_COLOR : undefined}
      wrap="truncate"
    >
      {run.text}
    </Text>
  )
  const body = style.link && linkFits(style.link) ? <Link href={style.link}>{text}</Link> : text

  return (
    <Box marginTop={run.marginTop} flexShrink={0}>
      {body}
    </Box>
  )
}

function drawRun(t: Table, run: Run): RenderElement {
  const { Box, Text, Image } = t
  if (run.kind === 'text') return textRun(t, run)
  if (run.kind === 'fallback') {
    return (
      <Box marginTop={run.marginTop} flexShrink={0}>
        <Text dimColor={!run.isError} wrap="truncate">
          {run.text}
        </Text>
      </Box>
    )
  }

  return (
    <Box marginTop={run.marginTop} flexShrink={0}>
      <Image
        source={{ png: run.picture.png }}
        columns={run.picture.columns}
        rows={run.picture.rows}
        alt={altFor(run.latex, run.picture.columns)}
      />
    </Box>
  )
}

function drawLine(t: Table, line: Line): RenderElement {
  const { Box } = t

  return (
    <Box flexDirection="row" flexShrink={0} height={line.height}>
      {line.runs.map(run => drawRun(t, run))}
    </Box>
  )
}

function drawDisplay(t: Table, latex: string, indent: number, state: PictureState, width: number): RenderElement {
  const { Box, Text, Image } = t
  const room = Math.max(1, width - indent)
  const body =
    typeof state === 'object' && 'png' in state ? (
      <Image source={{ png: state.png }} columns={state.columns} rows={state.rows} alt={altFor(latex, state.columns)} />
    ) : (
      <Text dimColor={state === 'pending'}>{toUnicode(latex)}</Text>
    )

  return (
    <Box marginLeft={indent} width={room} justifyContent="center" flexShrink={0}>
      {body}
    </Box>
  )
}

/** A fenced block as the engine would have drawn it: a fence longer than any backtick run inside. */
function fenced(source: string): string {
  const longest = Math.max(2, ...(source.match(/`+/g) ?? []).map(run => run.length))
  const fence = '`'.repeat(longest + 1)

  return `${fence}mermaid\n${source}\n${fence}`
}

function segmentColor(role: DiagramRole): { color?: string; dimColor?: boolean } {
  if (role === 'dim') return { dimColor: true }
  if (role === 'text' || role === 'label') return { color: 'text' }
  if (!settings.diagramColor) return { color: 'text' }
  const scheme = theme()

  return { color: scheme ? DIAGRAM_COLORS[scheme][role] : DIAGRAM_FALLBACK }
}

function drawSegment(t: Table, segment: DiagramSegment): RenderElement {
  const { Text } = t

  return <Text {...segmentColor(segment.role)}>{segment.text}</Text>
}

/**
 * A mermaid block: the diagram as rows of coloured text; or, where it cannot
 * be drawn (another kind, a parse failure, too big), its source as the
 * engine draws a code block and one dim line saying why.
 */
function drawDiagram(t: Table, block: Extract<Block, { kind: 'mermaid' }>, width: number): RenderElement {
  const { Box, Text, Markdown } = t
  const indent = Math.min(block.indent, width - 1)
  const room = Math.max(1, width - indent)
  noteDiagram()
  const result = renderMermaid(block.source, room)
  const reason = !result.ok
    ? result.reason
    : result.rows.length > DIAGRAM_ROWS
      ? `${result.rows.length} rows, more than ${DIAGRAM_ROWS}`
      : result.columns > room
        ? `${result.columns} columns wide, ${room} available`
        : undefined
  const body =
    result.ok && reason === undefined ? (
      <Box flexDirection="column" flexShrink={0}>
        {result.rows.map(row => (
          <Box flexShrink={0} height={1}>
            <Text wrap="truncate">{row.map(segment => drawSegment(t, segment))}</Text>
          </Box>
        ))}
      </Box>
    ) : (
      <Box flexDirection="column" flexShrink={0}>
        <Markdown text={fenced(block.source)} />
        <Text dimColor wrap="truncate">
          {`mermaid diagram shown as source: ${(reason ?? 'not drawn').replace(/\s+/g, ' ').slice(0, 200)}`}
        </Text>
      </Box>
    )

  return indent > 0 ? (
    <Box marginLeft={indent} width={room} flexShrink={0}>
      {body}
    </Box>
  ) : (
    body
  )
}

// One blank row where the source had a blank line between two blocks, as the
// engine's markdown draws it; none where it had none ("Steps:\n1. ...", the
// items of a tight list).
function drawBlocks(t: Table, blocks: Block[], picture: (ref: MathRef, width: number) => PictureState, width: number): RenderElement[] {
  const { Box, Markdown } = t

  return blocks.map((block, i) => {
    const marginTop = i > 0 && block.gapBefore ? 1 : 0
    let body: RenderElement
    if (block.kind === 'markdown') {
      const indent = Math.min(block.indent ?? 0, width - 1)
      body =
        indent > 0 ? (
          <Box marginLeft={indent} width={width - indent} flexShrink={0}>
            <Markdown text={block.text} />
          </Box>
        ) : (
          <Markdown text={block.text} />
        )
    } else if (block.kind === 'display') {
      body = drawDisplay(t, block.latex, block.indent, picture({ latex: block.latex, display: true }, width - block.indent), width)
    } else if (block.kind === 'mermaid') {
      body = drawDiagram(t, block, width)
    } else {
      // A picture on a continuation line sits after the hang: it gets the rest.
      const room = Math.max(1, width - block.indent - stringWidth(block.marker ?? ''))
      const lines = layoutParagraph(block, ref => picture(ref, room), width)
      body = (
        <Box flexDirection="column" flexShrink={0}>
          {lines.map(line => drawLine(t, line))}
        </Box>
      )
    }

    return (
      <Box flexDirection="column" marginTop={marginTop} flexShrink={0}>
        {body}
      </Box>
    )
  })
}

/** The width the text of a block gets, as the engine wraps its own Markdown. */
function frameWidth(columns: number | undefined, gutter: number): number {
  const room = Math.max(10, (columns ?? 80) - gutter)

  return maxProseWidth === undefined ? room : Math.min(room, maxProseWidth)
}

function drawFrame(t: Table, frame: Frame, blocks: RenderElement[]): RenderElement {
  const { Box, Text } = t
  const lead =
    frame.bullet !== undefined ? (
      <Box minWidth={BULLET_GUTTER} flexShrink={0}>
        <Text color="text">{frame.bullet}</Text>
      </Box>
    ) : frame.prefix !== undefined ? (
      <Box minWidth={OUTPUT_GUTTER} flexShrink={0}>
        <Text dimColor>{frame.prefix}</Text>
      </Box>
    ) : null

  return (
    <Box flexDirection="row" alignItems="flex-start" marginTop={frame.marginTop} width="100%">
      {lead}
      <Box flexDirection="column" width={frame.width}>
        {blocks}
      </Box>
    </Box>
  )
}

/**
 * What to do with a block of markdown: draw it (formulas as pictures), hand
 * the engine a text whose formulas became Unicode (all of them sat in
 * headings, tables or quotes, which the engine's markdown draws), or leave it.
 */
type Plan = { blocks: Block[] } | { text: string } | null

const plans = new Map<string, Plan>()

/** Can this text hold a formula or a diagram at all? A scan, no parse. */
function mayDraw(text: string): boolean {
  return /\$\S|\\[([]/.test(text) || /mermaid/i.test(text)
}

function makePlan(text: string): Plan {
  const blocks = parseMessage(text)
  if (blocks.some(block => block.kind !== 'markdown')) return { blocks }
  const joined = blocks.map(block => (block.kind === 'markdown' ? block.text : '')).join('\n\n')

  return joined !== text && joined.trim() !== '' ? { text: joined } : null
}

/** The plan for a text, parsed once (least recently used parse first out). */
function plan(text: string): Plan {
  const known = plans.get(text)
  if (known !== undefined) {
    plans.delete(text)
    plans.set(text, known)

    return known
  }
  const made = makePlan(text)
  plans.set(text, made)
  if (plans.size > PLAN_LIMIT) plans.delete(plans.keys().next().value as string)

  return made
}

/** Every picture the blocks will ask for, at the width each is drawn in (as drawBlocks asks). */
function eachPicture(blocks: Block[], width: number, ask: (ref: MathRef, width: number) => void) {
  for (const block of blocks) {
    if (block.kind === 'display') ask({ latex: block.latex, display: true }, width - block.indent)
    if (block.kind !== 'paragraph') continue
    const room = Math.max(1, width - block.indent - stringWidth(block.marker ?? ''))
    for (const inline of block.inlines) if (inline.kind === 'math') ask({ latex: inline.latex, display: false }, room)
  }
}

// What a site was drawn with: its text, frame, and every picture state it
// was given; equal states now mean an equal tree.
type Asked = { ref: MathRef; width: number; state: PictureState }
type Drawn = { text: string; frameKey: string; asked: Asked[]; tree: RenderElement; isPending: boolean }

const drawn = new Map<string, Drawn>()

function frameKey(frame: Frame): string {
  return [frame.bullet ?? '', frame.prefix ?? '', frame.width, frame.marginTop, theme() ?? '', settings.diagramColor].join('|')
}

/**
 * Draws blocks with their formulas for the site `site` (the instance's
 * requestId, which a landed batch redraws), or returns the tree drawn last
 * time when nothing it showed has changed. `isPending`: some formula is
 * still on its way, so the site must stay subscribed to its redraw.
 */
function drawMarkdown(t: Table, site: string, text: string, blocks: Block[], frame: Frame): Drawn {
  const key = frameKey(frame)
  const last = drawn.get(site)
  if (last && last.text === text && last.frameKey === key && last.asked.every(({ ref, width, state }) => lookup(ref, width, site) === state)) {
    drawn.delete(site)
    drawn.set(site, last)

    return last
  }
  const asked: Asked[] = []
  const picture = (ref: MathRef, width: number) => {
    const state = lookup(ref, width, site)
    asked.push({ ref, width, state })

    return state
  }
  // Ask for every formula at once, so one batch carries them all.
  for (const ref of mathRefs(blocks)) picture(ref, frame.width)
  const tree = drawFrame(t, frame, drawBlocks(t, blocks, picture, frame.width))
  const made: Drawn = { text, frameKey: key, asked, tree, isPending: asked.some(({ state }) => state === 'pending') }
  drawn.delete(site)
  drawn.set(site, made)
  if (drawn.size > DRAWN_LIMIT) drawn.delete(drawn.keys().next().value as string)

  return made
}

// ---------------------------------------------------------------------------
// Streaming prerender

type Watched = { text: string; isCooling: boolean; isDirty: boolean }

/** Queues every formula closed so far in a streaming block, at the widths its draw will ask. */
function prerender(text: string) {
  // Not through the plan memo: a partial text is never drawn.
  const found = makePlan(text)
  if (found === null || !('blocks' in found)) return
  const width = lastColumns === undefined ? 255 : frameWidth(lastColumns, BULLET_GUTTER)
  eachPicture(found.blocks, width, (ref, room) => prefetch(ref, room))
}

// ---------------------------------------------------------------------------
// Status

function yesNo(value: boolean | undefined): string {
  return value ? 'yes' : 'no'
}

/** A part of --check's answer this mod does not shape (daemon, format), as one line. */
function described(value: unknown): string {
  if (value === undefined || value === null) return 'unknown'
  if (typeof value !== 'object') return String(value).slice(0, 200)
  const parts = Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item === null || ['string', 'number', 'boolean'].includes(typeof item))
    .map(([name, item]) => `${name} ${typeof item === 'boolean' ? yesNo(item) : String(item).slice(0, 80)}`)

  return parts.length > 0 ? parts.join(', ') : 'unknown'
}

function daemonText(health: Health): string {
  if (health.daemon === null) return 'not reached: each batch renders in its own process (slower)'
  const daemon = health.daemon as { pid?: unknown; uptime?: unknown; served?: unknown } | undefined
  if (typeof daemon?.pid === 'number') {
    const uptime = typeof daemon.uptime === 'number' ? `, up ${Math.round(daemon.uptime)} s` : ''
    const served = typeof daemon.served === 'number' ? `, ${daemon.served} requests served` : ''

    return `running (pid ${daemon.pid}${uptime}${served})`
  }

  return described(health.daemon)
}

function formatText(health: Health): string {
  const format = health.format as { ok?: unknown; path?: unknown } | undefined
  if (format?.ok === true) return `precompiled${typeof format.path === 'string' ? ` (${format.path})` : ''}`
  if (format?.ok === false) return 'not built: every TeX run reads the whole preamble (slower)'

  return described(health.format)
}

/** How many definitions the `macros` option holds. */
function definitionCount(macros: string): number {
  return (macros.match(/\\(?:(?:re)?newcommand|providecommand|[gex]?def|let|DeclareMathOperator)(?![A-Za-z])/g) ?? []).length
}

function statusText(health: Health): string {
  const now = stats()
  const cell = health.cell ? `${health.cell.width}x${health.cell.height} px` : 'unknown'
  const source = enabled === undefined ? 'from settings' : `by /${COMMAND} ${enabled ? 'on' : 'off'}`
  const lines = [
    `${isEnabled() ? 'on' : 'off'} (${source})`,
    `backend: ${health.ok ? 'ready' : 'NOT READY'} - latex ${yesNo(health.latex)}, dvipng ${yesNo(health.dvipng)}, Pillow ${yesNo(health.pil)}`,
    ...health.problems.map(problem => `  problem: ${problem}`),
    `cell: ${cell} (setting: ${settings.cell}), baseline ${settings.baseline}, ${settings.ptPx} px/pt, min shrink ${settings.minShrink}`,
    `colour: ${health.fg ?? 'unknown'} (setting: ${settings.foreground})`,
    `daemon: ${daemonText(health)}`,
    `format: ${formatText(health)}`,
    `packages: ${settings.packages.length > 0 ? settings.packages.join(', ') : 'none extra'}; macros: ${definitionCount(settings.macros)} definitions`,
    `cache: ${now.pictures} pictures, ${now.failed} failed, ${now.pending} pending, ${now.batches} render calls${health.cacheDir ? `; on disk: ${health.cacheDir}` : ''}`,
    `diagrams: ${settings.diagramColor ? `colour (${theme() ?? 'theme not known yet'})` : 'monochrome'}`,
    `prompt hint: ${settings.promptHint ? 'on' : 'off'}`,
  ]
  if (now.errors.length > 0) {
    lines.push('recent errors:')
    for (const { latex, error } of now.errors.slice(-5)) {
      lines.push(`  ${latex.slice(0, 60)}: ${error.slice(0, 160)}`)
    }
  }

  return lines.join('\n')
}

// ---------------------------------------------------------------------------
// Hooks

export const register: Register = (on, options) => {
  settings = readSettings(options)
  configure(settings, options)
  warned = false

  on('session.start', async ($, e, next) => {
    onProblem(reason => {
      if (warned) return
      warned = true
      $.ui.toast(`inline-math: formulas shown as text (${reason.slice(0, 200)}). Run /${COMMAND} status for details.`, {
        timeoutMs: 10_000,
      })
    })
    // The worker first: nothing awaited below may keep formulas from drawing.
    // Health at once too, so a broken backend is told once and never run per formula.
    void runWorker($)
    void check($)
    startPolling($)
    await $.command
      .register({
        name: COMMAND,
        description: 'Typeset LaTeX in replies: status, on, off, demo, clear',
        argumentHint: 'status | on | off | demo | clear',
      })
      .catch(() => {})
    const stored = await $.store.get(STORE_ENABLED).catch(() => undefined)
    enabled = typeof stored === 'boolean' ? stored : undefined
    const prose = await $.settings.read({ key: 'maxProseWidth' }).catch(() => undefined)
    maxProseWidth = typeof prose === 'number' && prose >= 40 ? prose : undefined
    void $.process
      .run(['uname', '-s'], { timeoutMs: 5000 })
      .then(({ stdout }) => {
        bulletGlyph = stdout.trim() === 'Darwin' ? BULLET_MAC : BULLET_OTHER
      })
      .catch(() => {})

    return next(e)
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (!settings.promptHint || !isEnabled() || !e.surfaces.includes('terminal')) return composed

    return {
      sections: [
        ...composed.sections.filter(section => section.id !== 'inline-math:hint'),
        { id: 'inline-math:hint', text: PROMPT_HINT, scope: 'session' as const },
      ],
    }
  })

  on('command.run', { command: COMMAND }, async ($, e) => {
    const verb = e.args.trim().toLowerCase()
    if (verb === 'on' || verb === 'off') {
      enabled = verb === 'on'
      await $.store.set(STORE_ENABLED, enabled).catch(() => {})
      $.ui.invalidate('ui.render')

      return { text: enabled ? 'on: formulas in replies are drawn as pictures.' : 'off: replies are drawn as the engine draws them.' }
    }
    if (verb === 'demo') return { text: DEMO }
    if (verb === 'clear') {
      clear()
      drawn.clear()
      plans.clear()
      $.ui.invalidate('ui.render')

      return { text: 'forgot the drawn formulas; each is drawn again when it shows.' }
    }
    if (verb === '' || verb === 'status') return { text: statusText(await check($)) }

    return { text: `Unknown argument "${verb}".\n${USAGE}` }
  }).catch(($, e) => ({ text: `/${COMMAND} ${e.args} failed.` }))

  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    try {
      if (e.surface !== 'terminal' || !isEnabled() || e.props.isSummary || !mayDraw(e.props.text)) return next(e)
      const found = plan(e.props.text)
      if (found === null) return next(e)
      // Spreading props carries isSummary and onScreen on as received.
      if ('text' in found) return next({ ...e, props: { ...e.props, text: found.text } })
      if (e.viewport?.columns !== undefined) lastColumns = e.viewport.columns
      const gutter = e.props.isFirstOfReply ? BULLET_GUTTER : 0
      const frame: Frame = {
        bullet: e.props.isFirstOfReply ? bulletGlyph : undefined,
        width: frameWidth(e.viewport?.columns, gutter),
        // The engine's addMargin, which the props do not carry: 1 in the
        // transcript as drawn today (a blank row above each block).
        marginTop: 1,
      }
      const made = drawMarkdown($.ui.resolve(e), e.requestId, e.props.text, found.blocks, frame)
      // Subscribes this reply alone to the batch that brings its pictures.
      if (made.isPending) await $.state.get({ ...REDRAW, id: e.requestId }).catch(() => undefined)

      return made.tree
    } catch {
      return next(e)
    }
  }).catch(($, e, next) => next(e))

  // Watches a reply stream by (every chunk passed on as it came, in order)
  // and queues each formula once it is closed, at most every SCAN_MS per
  // block (longer when a scan is costly), so its picture is ready when the
  // finished block is drawn. When a block ends (a chunk of another kind or
  // block, or the stream's end), what closed since its last scan is queued
  // at once and everything queued is sent without waiting: its draw is next.
  on('turn.step', async function* ($, e, next) {
    const stream = next(e)
    if (e.agentId !== undefined || !isEnabled()) return yield* stream
    warmUp($)
    const blocks = new Map<number, Watched>()
    let isOver = false
    let streaming: Watched | undefined // the text block the last chunk belonged to
    const scan = (watched: Watched) => {
      watched.isDirty = false
      if (isOver || next.budget.remainingMs < SCAN_RESERVE_MS) return
      watched.isCooling = true
      // Wall clock: a rescan runs from a timer, where the hook's budget does not count.
      const started = performance.now()
      try {
        prerender(watched.text)
      } catch {
        // a prerender is only ever a head start
      }
      const cost = performance.now() - started
      $.clock.after(Math.max(SCAN_MS, cost * SCAN_SHARE), () => {
        watched.isCooling = false
        if (watched.isDirty) scan(watched)
      })
    }
    const finish = (watched: Watched | undefined) => {
      if (watched?.isDirty) scan(watched)
      flush()
    }
    try {
      for await (const chunk of stream) {
        // An engine chunk (an envelope, a block's start or end marker) ends nothing by itself.
        const watched = chunk.kind === 'text' ? (blocks.get(chunk.index) ?? { text: '', isCooling: false, isDirty: false }) : chunk.kind === 'engine' ? streaming : undefined
        if (streaming !== undefined && watched !== streaming) finish(streaming)
        streaming = watched
        if (chunk.kind === 'text' && watched !== undefined) {
          blocks.set(chunk.index, watched)
          const seen = watched.text.slice(-1) + chunk.text
          watched.text += chunk.text
          if (watched.text.length <= SCAN_LIMIT && SCAN_TRIGGER.test(seen)) {
            if (watched.isCooling) watched.isDirty = true
            else scan(watched)
          }
        }
        yield chunk
      }
      // What closed since the last scan, once: the block is drawn next.
      for (const watched of blocks.values()) if (watched.isDirty) scan(watched)
      flush()
    } finally {
      isOver = true
    }

    return await stream.result
  })

  // The demo's output row is drawn by the same code as a reply.
  on('ui.render', { component: 'CommandOutput', props: { command: COMMAND } }, async ($, e, next) => {
    try {
      if (e.surface !== 'terminal' || e.props.isErrored || e.props.args.trim().toLowerCase() !== 'demo') return next(e)
      const found = plan(e.props.text)
      if (found === null || !('blocks' in found)) return next(e)
      const frame: Frame = { prefix: OUTPUT_PREFIX, width: frameWidth(e.viewport?.columns, OUTPUT_GUTTER), marginTop: 0 }
      const made = drawMarkdown($.ui.resolve(e), e.requestId, e.props.text, found.blocks, frame)
      if (made.isPending) await $.state.get({ ...REDRAW, id: e.requestId }).catch(() => undefined)

      return made.tree
    } catch {
      return next(e)
    }
  }).catch(($, e, next) => next(e))
}
