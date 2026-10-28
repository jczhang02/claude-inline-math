import type { Elements, Register, RenderElement } from 'claude-code'

import { hasMath, layoutParagraph, mathRefs, parseMessage, stringWidth, toUnicode } from './layout'
import type { Block, Line, MathRef, PictureState, Run } from './layout'
import { check, clear, configure, lookup, onProblem, readSettings, runWorker, startPolling, stats } from './render-client'
import type { Health, Settings } from './render-client'

// inline-math: draws the LaTeX in Claude's replies as typeset pictures, inline
// with the terminal text and on its baseline. Blocks without formulas go to
// the surface's own Markdown element; paragraphs with formulas are wrapped
// here, line by line (layout.ts), since a picture is a box of whole cells
// that no Text flows around. Pictures come from bin/render.py through
// render-client.ts; a draw never waits for them.

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

const PROMPT_HINT = [
  '# Math in replies',
  'This terminal draws LaTeX in your replies as typeset formulas. Write math as LaTeX:',
  '$...$ inline (no space after the opening $ or before the closing $), and $$...$$ on lines of their own for display math.',
  'Never put math in code spans or code blocks: code is drawn as written.',
  'Plain-text or Unicode versions of a formula are not needed.',
].join('\n')

const DEMO = [
  "**Demo.** Euler's identity $e^{i\\pi} + 1 = 0$ links $e$, $i$ and $\\pi$. For samples $x_i \\in \\mathbb{R}$ the mean is $\\bar{x} = \\frac{1}{n}\\sum_{i=1}^{n} x_i$ and the variance $\\sigma^2 = \\mathbb{E}[(X-\\mu)^2]$.",
  'A tall formula makes only its own line taller: $\\sqrt{\\frac{x^2+1}{y}}$, $\\binom{n}{k}$ and $\\int_0^1 f(x)\\,dx$ sit on the text baseline, and the lines around them keep one row.',
  '$$\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}$$',
  '中文混排: 设 $f(x) = x^2$, 则导数 $f\'(x) = 2x$, 且 $\\lim_{h \\to 0} \\frac{f(x+h) - f(x)}{h} = f\'(x)$. 中文字符占两格, 换行照样对齐.',
  '- A list item with $\\alpha + \\beta = \\gamma$ wraps under its own text, not under the marker, when the line is long enough to need it.\n- **Bold text with $E = mc^2$ inside** and `inline code` keep their styles.',
  'Prices like $5 and $10 stay text, and `$x$` in code stays code.',
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

function plan(text: string): Plan {
  const blocks = parseMessage(text)
  if (blocks.some(block => block.kind !== 'markdown')) return { blocks }
  const joined = blocks.map(block => (block.kind === 'markdown' ? block.text : '')).join('\n\n')

  return joined !== text && joined.trim() !== '' ? { text: joined } : null
}

/** Draws blocks with their formulas. */
function drawMarkdown(t: Table, blocks: Block[], frame: Frame): RenderElement {
  const picture = (ref: MathRef, width: number) => lookup(ref, width)
  // Ask for every formula at once, so one batch carries them all.
  for (const ref of mathRefs(blocks)) picture(ref, frame.width)

  return drawFrame(t, frame, drawBlocks(t, blocks, picture, frame.width))
}

// ---------------------------------------------------------------------------
// Status

function yesNo(value: boolean | undefined): string {
  return value ? 'yes' : 'no'
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
    `cache: ${now.pictures} pictures, ${now.failed} failed, ${now.pending} pending, ${now.batches} render calls${health.cacheDir ? `; on disk: ${health.cacheDir}` : ''}`,
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
      $.ui.invalidate('ui.render')

      return { text: 'forgot the drawn formulas; each is drawn again when it shows.' }
    }
    if (verb === '' || verb === 'status') return { text: statusText(await check($)) }

    return { text: `Unknown argument "${verb}".\n${USAGE}` }
  }).catch(($, e) => ({ text: `/${COMMAND} ${e.args} failed.` }))

  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    try {
      if (e.surface !== 'terminal' || !isEnabled() || e.props.isSummary || !hasMath(e.props.text)) return next(e)
      const found = plan(e.props.text)
      if (found === null) return next(e)
      // Spreading props carries isSummary and onScreen on as received.
      if ('text' in found) return next({ ...e, props: { ...e.props, text: found.text } })
      const t = $.ui.resolve(e)
      const gutter = e.props.isFirstOfReply ? BULLET_GUTTER : 0
      const frame: Frame = {
        bullet: e.props.isFirstOfReply ? bulletGlyph : undefined,
        width: frameWidth(e.viewport?.columns, gutter),
        // The engine's addMargin, which the props do not carry: 1 in the
        // transcript as drawn today (a blank row above each block).
        marginTop: 1,
      }

      return drawMarkdown(t, found.blocks, frame)
    } catch {
      return next(e)
    }
  }).catch(($, e, next) => next(e))

  // The demo's output row is drawn by the same code as a reply.
  on('ui.render', { component: 'CommandOutput', props: { command: COMMAND } }, async ($, e, next) => {
    try {
      if (e.surface !== 'terminal' || e.props.isErrored || e.props.args.trim().toLowerCase() !== 'demo') return next(e)
      const found = plan(e.props.text)
      if (found === null || !('blocks' in found)) return next(e)
      const t = $.ui.resolve(e)
      const frame: Frame = { prefix: OUTPUT_PREFIX, width: frameWidth(e.viewport?.columns, OUTPUT_GUTTER), marginTop: 0 }

      return drawMarkdown(t, found.blocks, frame)
    } catch {
      return next(e)
    }
  }).catch(($, e, next) => next(e))
}
