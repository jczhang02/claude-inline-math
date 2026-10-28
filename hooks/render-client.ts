import type { EngineInterface, PluginOptions } from 'claude-code'

import type { MathRef, Picture, PictureState } from './layout'

// The one place that runs bin/render.py. A draw asks `lookup` for each
// formula and gets what is ready now; what is missing is queued. A worker the
// session starts (`runWorker`) sends the queue in batched calls a moment
// later and asks for one redraw when a batch lands. A formula that is ready
// or failed is never asked for again, so a redraw that finds everything ready
// starts no work and no further redraw.
//
// The engine allows `$` only as `$.noun.event(...)` at a call site and as an
// argument to a function like these, never kept in a variable, so the worker
// holds the session's `$` as its own parameter for the life of the module.

export type Settings = {
  enabled: boolean
  foreground: string // 'auto' or '#rrggbb'
  cell: string // 'auto' or 'WxH'
  baseline: number
  ptPx: number
  minShrink: number
  promptHint: boolean
}

export type Health = {
  ok: boolean
  latex: boolean
  dvipng: boolean
  pil: boolean
  cell?: { width: number; height: number }
  fg?: string
  cacheDir?: string
  problems: string[]
}

export type Stats = {
  pictures: number
  failed: number
  pending: number
  batches: number
  backendDown: string | undefined
  fg: string | undefined
  cell: string | undefined
  errors: { latex: string; error: string }[]
}

type Failure = { error: string; isBackend?: boolean }
type Result = Picture | Failure
type Job = { key: string; ref: MathRef; maxColumns: number }
type Answer = { cell?: { width: number; height: number }; fg?: string; results?: unknown[] }
// What one render.py call came to: its answer, a failure (`transient`: worth
// asking again; `down`: python itself cannot run), or 'split' when the answer
// was too big to come back whole.
type Outcome = { answer: Answer } | { failure: string; transient: boolean; down: boolean } | 'split'

export const DEFAULTS: Settings = {
  enabled: true,
  foreground: 'auto',
  cell: 'auto',
  baseline: 0.773,
  ptPx: 3.4,
  minShrink: 0.8,
  promptHint: true,
}

const MAX_COLUMNS = 255 // widest Image the terminal draws
const COALESCE_MS = 30 // requests closer than this share one render.py call
const BATCH_ITEMS = 16 // keeps one call well inside its time limit
const RUN_TIMEOUT_MS = 90_000
const CHECK_TIMEOUT_MS = 15_000
const DETECT_TIMEOUT_MS = 5_000
const DETECT_MS = 5_000 // how often an 'auto' colour and cell size are looked up again
const DOWN_RECHECK_MS = 60_000 // how often a broken backend is checked again
const IDLE_AFTER_MS = 600_000 // no formula drawn this long: look up only every IDLE_DETECT_MS
const IDLE_DETECT_MS = 60_000
// Pictures kept: by count and by base64 size, least recently drawn first out.
// Far above what one session shows at once, so a redraw never evicts what it draws.
const CACHE_LIMIT = 6000
const CACHE_BYTES = 192 * 1024 * 1024
// The bounds in force: the defaults, or a test's (the undocumented option
// `cacheEntries`, which only the tests set).
const cacheLimits = { entries: CACHE_LIMIT, bytes: CACHE_BYTES }
// A formula drawn this many times and evicted each time stops being asked
// for (text instead): more formulas on screen than the cache holds must not
// turn into a render-and-redraw loop.
const RENDER_LIMIT = 3
const RETRIES = 2 // for a formula that failed for a passing reason, not one TeX refused
const ERROR_LOG = 20

let settings: Settings = DEFAULTS
let resolvedFg: string | undefined
let resolvedCell: string | undefined
let backendDown: string | undefined
let problemHandler: (reason: string) => void = () => {}
let batches = 0
let wake: (() => void) | undefined
let worker = 0 // the running worker's number; an older one stops
let cacheBytes = 0

const cache = new Map<string, Result>()
const queue = new Map<string, Job>()
const inFlight = new Set<string>()
const retries = new Map<string, number>()
const renders = new Map<string, number>()
const errors: { latex: string; error: string }[] = []

function number(value: unknown, fallback: number, low: number, high: number): number {
  const n = typeof value === 'number' ? value : Number(value)

  return Number.isFinite(n) && n >= low && n <= high ? n : fallback
}

/** The manifest's userConfig values, checked, with defaults for anything off. */
export function readSettings(options: PluginOptions): Settings {
  const fg = String(options.foreground ?? 'auto').trim()
  const cell = String(options.cell ?? 'auto').trim()

  return {
    enabled: options.enabled !== false,
    foreground: /^#[0-9a-fA-F]{6}$/.test(fg) ? fg.toLowerCase() : 'auto',
    cell: /^\d{1,3}x\d{1,3}$/.test(cell) ? cell : 'auto',
    baseline: number(options.baseline, DEFAULTS.baseline, 0.3, 0.95),
    ptPx: number(options.ptPx, DEFAULTS.ptPx, 1, 12),
    minShrink: number(options.minShrink, DEFAULTS.minShrink, 0.3, 1),
    promptHint: options.promptHint !== false,
  }
}

/** Takes the settings the module loaded with. */
export function configure(next: Settings, options: PluginOptions = {}) {
  settings = next
  const entries = Number(options.cacheEntries)
  cacheLimits.entries = Number.isInteger(entries) && entries >= 2 ? entries : CACHE_LIMIT
}

/** Called once per backend failure, with the reason, when nothing can be drawn. */
export function onProblem(handler: (reason: string) => void) {
  problemHandler = handler
}

function isPicture(value: unknown): value is Picture {
  const p = value as Picture
  return (
    typeof p === 'object' &&
    p !== null &&
    typeof p.png === 'string' &&
    p.png.length > 0 &&
    Number.isInteger(p.columns) &&
    p.columns >= 1 &&
    p.columns <= MAX_COLUMNS &&
    Number.isInteger(p.rows) &&
    p.rows >= 1 &&
    p.rows <= MAX_COLUMNS &&
    Number.isInteger(p.textRow) &&
    p.textRow >= 0 &&
    p.textRow < p.rows
  )
}

/** An item error render.py gives for a passing reason, not for the formula. */
function isTransient(error: string): boolean {
  return /time ?out|timed out|deadline|^renderer|memory or output limit/i.test(error)
}

function lastLine(text: string): string {
  return text.trim().split('\n').filter(Boolean).pop()?.slice(0, 300) ?? ''
}

const cellText = (cell: { width: number; height: number }) => `${cell.width}x${cell.height}`

/** The colour pictures are drawn in now: the setting, or what 'auto' resolved to. */
function currentFg(): string {
  return settings.foreground === 'auto' ? (resolvedFg ?? 'auto') : settings.foreground
}

function currentCell(): string {
  return settings.cell === 'auto' ? (resolvedCell ?? 'auto') : settings.cell
}

function optionsKey(): string {
  return [currentFg(), currentCell(), settings.baseline, settings.ptPx, settings.minShrink].join(',')
}

function keyOf(ref: MathRef, maxColumns: number): string {
  return `${optionsKey()}|${maxColumns}|${ref.display ? 'D' : 'I'}|${ref.latex}`
}

function sizeOf(result: Result): number {
  return 'png' in result ? result.png.length : 64
}

function forget(key: string) {
  const old = cache.get(key)
  if (old === undefined) return
  cacheBytes -= sizeOf(old)
  cache.delete(key)
}

function remember(key: string, result: Result) {
  forget(key)
  cache.set(key, result)
  cacheBytes += sizeOf(result)
  while (cache.size > 1 && (cache.size > cacheLimits.entries || cacheBytes > cacheLimits.bytes)) {
    const oldest = cache.keys().next().value
    if (oldest === undefined) break
    forget(oldest)
  }
}

/** A cache hit, moved to the recent end (the cache is least recently used first out). */
function hit(key: string): Result | undefined {
  const found = cache.get(key)
  if (found !== undefined) {
    cache.delete(key)
    cache.set(key, found)
  }

  return found
}

function reportDown(reason: string) {
  if (backendDown !== undefined) return
  backendDown = reason
  problemHandler(reason)
}

/** Queues `ref`, or false when it was drawn and evicted too often to ask again. */
function enqueue(ref: MathRef, maxColumns: number): boolean {
  const key = keyOf(ref, maxColumns)
  if (inFlight.has(key) || queue.has(key)) return true
  if ((renders.get(key) ?? 0) >= RENDER_LIMIT) return false
  queue.set(key, { key, ref, maxColumns })
  wake?.()

  return true
}

// Set by every lookup, so the poller knows formulas are being drawn.
let isDrawn = false

const EVICTED: Failure = { error: 'more formulas on screen than the picture cache holds', isBackend: true }

/**
 * What is ready for `ref` at `width` columns: a picture, an error, or
 * 'pending' (then queued for the worker). A picture wider than `width` is
 * asked for again at that width, so a long formula shrinks to fit.
 */
export function lookup(ref: MathRef, width: number): PictureState {
  isDrawn = true
  if (backendDown !== undefined) return { error: backendDown, isBackend: true }
  const base = hit(keyOf(ref, MAX_COLUMNS))
  if (base === undefined) return enqueue(ref, MAX_COLUMNS) ? 'pending' : EVICTED
  if ('error' in base || base.columns <= width) return base
  const fit = Math.max(1, Math.min(MAX_COLUMNS, Math.floor(width)))
  const narrow = hit(keyOf(ref, fit))
  if (narrow === undefined) return enqueue(ref, fit) ? 'pending' : EVICTED

  return narrow
}

function takeBatch(): Job[] {
  const batch: Job[] = []
  for (const job of queue.values()) {
    if (batch.length === 0 || (job.maxColumns === batch[0]?.maxColumns && batch.length < BATCH_ITEMS)) {
      batch.push(job)
    }
  }
  for (const job of batch) {
    queue.delete(job.key)
    inFlight.add(job.key)
  }

  return batch
}

function settle(job: Job, result: Result, storeKey: string, transient: boolean) {
  if (transient) {
    const tries = (retries.get(job.key) ?? 0) + 1
    retries.set(job.key, tries)
    if (tries <= RETRIES) return // not kept: the next draw asks again
  }
  if ('error' in result) {
    errors.push({ latex: job.ref.latex, error: result.error })
    errors.splice(0, Math.max(0, errors.length - ERROR_LOG))
  } else {
    renders.set(storeKey, (renders.get(storeKey) ?? 0) + 1)
    if (renders.size > 4 * CACHE_LIMIT) renders.clear()
  }
  remember(storeKey, result)
}

/** Does a failed run mean python or render.py cannot run at all? */
function isStartFailure(text: string): boolean {
  return /ENOENT|EACCES|not found|No such file|cannot (?:start|run|spawn|execute)|spawn/i.test(text)
}

/** One render.py call for `batch`. */
async function callRender($: EngineInterface, batch: Job[]): Promise<Outcome> {
  const request = {
    items: batch.map(job => ({ latex: job.ref.latex, display: job.ref.display })),
    options: {
      // The resolved values when known, so each call draws for the key it is
      // filed under and render.py skips its own lookup.
      fg: currentFg(),
      cell: currentCell(),
      baseline: settings.baseline,
      ptPx: settings.ptPx,
      minShrink: settings.minShrink,
      cacheDir: null, // render.py's own: ~/.cache/claude-inline-math
      maxColumns: batch[0]?.maxColumns ?? MAX_COLUMNS,
    },
  }
  let run: Awaited<ReturnType<EngineInterface['process']['run']>>
  try {
    run = await $.process.run(['python3', '-I', `${$.plugin.root}/bin/render.py`], {
      stdin: JSON.stringify(request),
      timeoutMs: RUN_TIMEOUT_MS,
    })
  } catch (err) {
    const text = String(err).slice(0, 200)
    if (/time ?out|timed out/i.test(text)) return { failure: `render.py timed out (${text})`, transient: true, down: false }

    return { failure: `could not run python3: ${text}`, transient: !isStartFailure(text), down: isStartFailure(text) }
  }
  if (run.isStdoutTruncated) return 'split'
  try {
    const answer = JSON.parse(run.stdout) as Answer
    if (Array.isArray(answer?.results)) return { answer }
  } catch {
    // reported below
  }
  const reason = lastLine(run.stderr) || `render.py exited ${run.exitCode} with no answer`
  // A missing interpreter or module breaks every formula: stop asking.
  const down = /ModuleNotFoundError|No module named|can't open file|No such file or directory: .*render\.py/i.test(reason)

  return { failure: reason, transient: !down, down }
}

/** Runs render.py for `batch` and keeps what it answered; halves a batch whose answer was too big. */
async function runBatch($: EngineInterface, batch: Job[]): Promise<void> {
  const outcome = await callRender($, batch)
  batches += 1
  if (outcome === 'split') {
    if (batch.length > 1) {
      const half = Math.ceil(batch.length / 2)
      await runBatch($, batch.slice(0, half))
      await runBatch($, batch.slice(half))
    } else {
      const job = batch[0] as Job
      settle(job, { error: 'picture too large to pass back' }, job.key, false)
    }

    return
  }
  if ('failure' in outcome) {
    if (outcome.down) reportDown(outcome.failure)
    for (const job of batch) settle(job, { error: outcome.failure }, job.key, outcome.transient)

    return
  }

  // Kept under the colour and cell size the pictures were drawn for; when
  // those changed, every lookup now misses and asks again.
  const { answer } = outcome
  adopt(answer)
  batch.forEach((job, i) => {
    const raw = answer.results?.[i]
    const result: Result = isPicture(raw)
      ? { png: raw.png, columns: raw.columns, rows: raw.rows, textRow: raw.textRow }
      : { error: typeof (raw as Failure)?.error === 'string' ? (raw as Failure).error : 'no picture' }
    settle(job, result, keyOf(job.ref, job.maxColumns), 'error' in result && isTransient(result.error))
  })
}

function waitForWork(): Promise<void> {
  return new Promise(resolve => {
    wake = () => {
      wake = undefined
      resolve()
    }
    if (queue.size > 0) wake()
  })
}

/**
 * The session's render loop: waits for queued formulas, lets requests that
 * arrive close together gather, sends them, and asks for one redraw per round.
 * Started from `session.start`, whose `$` outlives the hook; a newer worker
 * (a reload's) ends the older one.
 */
export async function runWorker($: EngineInterface) {
  worker += 1
  const me = worker
  wake?.()
  for (;;) {
    await waitForWork()
    if (me !== worker) return
    await new Promise<void>(resolve => {
      $.clock.after(COALESCE_MS, resolve)
    })
    while (queue.size > 0 && me === worker) {
      const batch = takeBatch()
      try {
        await runBatch($, batch)
      } catch {
        // runBatch keeps its own failures; nothing reaches here but a bug
      } finally {
        for (const job of batch) inFlight.delete(job.key)
      }
    }
    if (me === worker) $.ui.invalidate('ui.render')
  }
}

async function runCheck($: EngineInterface): Promise<Health> {
  try {
    const run = await $.process.run(['python3', '-I', `${$.plugin.root}/bin/render.py`, '--check'], {
      timeoutMs: CHECK_TIMEOUT_MS,
    })
    const health = JSON.parse(run.stdout) as Health

    return { ...health, problems: Array.isArray(health.problems) ? health.problems : [] }
  } catch (err) {
    return {
      ok: false,
      latex: false,
      dvipng: false,
      pil: false,
      problems: [`render.py --check failed: ${String(err).slice(0, 300)}`],
    }
  }
}

/**
 * Adopts a looked-up 'auto' colour and cell size (only for the settings that
 * are 'auto'); true when either changed and pictures must be drawn again.
 */
function adopt(found: { fg?: unknown; cell?: unknown }): boolean {
  const before = optionsKey()
  if (settings.foreground === 'auto' && typeof found.fg === 'string' && /^#[0-9a-f]{6}$/i.test(found.fg)) {
    resolvedFg = found.fg.toLowerCase()
  }
  const cell = found.cell as { width?: unknown; height?: unknown } | undefined
  if (settings.cell === 'auto' && typeof cell?.width === 'number' && typeof cell.height === 'number') {
    resolvedCell = cellText({ width: cell.width, height: cell.height })
  }

  return optionsKey() !== before
}

/**
 * Runs `render.py --check` and adopts what it found: a broken backend (told
 * once, then every formula shows as text), a repaired one, or a new colour or
 * cell size (the desktop switched light and dark, the font size changed).
 */
export async function check($: EngineInterface): Promise<Health> {
  const health = await runCheck($)
  if (!health.ok) {
    reportDown(health.problems.join('; ') || 'render.py is not ready')

    return health
  }
  const wasDown = backendDown !== undefined
  backendDown = undefined
  const changed = adopt(health)
  if ((changed && cache.size > 0) || wasDown) $.ui.invalidate('ui.render')

  return health
}

/** `render.py --detect`: the 'auto' colour and cell size alone, without a test render. */
async function detect($: EngineInterface) {
  try {
    const run = await $.process.run(['python3', '-I', `${$.plugin.root}/bin/render.py`, '--detect'], {
      timeoutMs: DETECT_TIMEOUT_MS,
    })
    if (adopt(JSON.parse(run.stdout) as { fg?: unknown; cell?: unknown }) && cache.size > 0) $.ui.invalidate('ui.render')
  } catch {
    // the next round tries again
  }
}

/**
 * Follows the desktop: while the colour or cell size is 'auto', looks them
 * up again every few seconds for ten minutes after a formula was last drawn
 * (light and dark switch at once), then once a minute. A broken backend is checked again now and
 * then, and comes back by itself once repaired.
 */
export function startPolling($: EngineInterface) {
  const me = worker
  let busy = false
  let sinceCheck = 0
  let idleMs = 0
  const timer = $.clock.every(DETECT_MS, () => {
    if (me !== worker) {
      timer.cancel()

      return
    }
    if (busy) return
    if (backendDown !== undefined) {
      sinceCheck += DETECT_MS
      if (sinceCheck < DOWN_RECHECK_MS) return
      sinceCheck = 0
      busy = true
      void check($).finally(() => {
        busy = false
      })

      return
    }
    const auto = settings.foreground === 'auto' || settings.cell === 'auto'
    if (!auto || cache.size === 0) return
    idleMs = isDrawn ? 0 : idleMs + DETECT_MS
    isDrawn = false
    if (idleMs >= IDLE_AFTER_MS && idleMs % IDLE_DETECT_MS !== 0) return
    busy = true
    void detect($).finally(() => {
      busy = false
    })
  })
}

/** Forgets every picture and failure; the caller asks for the redraw. */
export function clear() {
  cache.clear()
  cacheBytes = 0
  retries.clear()
  renders.clear()
  errors.length = 0
  backendDown = undefined
}

export function stats(): Stats {
  let pictures = 0
  let failed = 0
  for (const value of cache.values()) {
    if ('error' in value) failed += 1
    else pictures += 1
  }

  return {
    pictures,
    failed,
    pending: queue.size + inFlight.size,
    batches,
    backendDown,
    fg: resolvedFg,
    cell: resolvedCell,
    errors: [...errors],
  }
}
