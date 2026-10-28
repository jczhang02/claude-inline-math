import type { EngineInterface, PluginOptions } from 'claude-code'

import type { MathRef, Picture, PictureState } from './layout'

// The one place that runs the backend (bin/client.py, which hands each
// request to the long-lived render daemon, or renders in-process when the
// daemon cannot be reached). A draw asks `lookup` for each formula and gets
// what is ready now; what is missing is queued, with the drawn site (the
// message's requestId) noted as waiting for it. A worker the session starts
// (`runWorker`) sends the queue in batched calls a moment later; when a batch
// lands, only the sites that waited for one of its formulas are drawn again
// (each reads its member of the `redraw` state family while drawing, and a
// write to it redraws that site alone). A formula that is ready or failed is
// never asked for again, so a redraw that finds everything ready starts no
// work and no further redraw. `prefetch` queues a formula no site waits for
// yet (the streaming prerender), so it is cached by the time it is drawn:
// such formulas gather for up to PREFETCH_MS, so a streaming reply costs a
// few backend calls and not one per formula, and `flush` sends them at once
// when their block has ended (its draw comes next).
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
  packages: string[] // extra TeX Live packages, in the precompiled format
  macros: string // the user's own definitions, in the precompiled format
  diagramColor: boolean
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
  // What client.py --check says of the render daemon and the precompiled
  // format; shapes the backend owns, shown as given.
  daemon?: unknown
  format?: unknown
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
// eager: a draw waits for it (sent after COALESCE_MS), else a prefetch.
type Job = { key: string; ref: MathRef; maxColumns: number; eager: boolean }
type Answer = { cell?: { width: number; height: number }; fg?: string; results?: unknown[] }
// What one backend call came to: its answer, a failure (`transient`: worth
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
  packages: [],
  macros: '',
  diagramColor: true,
}

const REDRAW = { plugin: 'inline-math', key: 'redraw' } as const

const MAX_COLUMNS = 255 // widest Image the terminal draws
const COALESCE_MS = 30 // requests closer than this share one backend call
const PREFETCH_MS = 400 // prefetches gather this long (a draw's request or flush() ends the wait)
const WARM_AFTER_MS = 60_000 // a turn that starts this long after the last backend call warms the daemon
const BATCH_ITEMS = 16 // keeps one call well inside its time limit
const RUN_TIMEOUT_MS = 90_000
const MAX_PACKAGES = 64
const MAX_MACROS = 64 * 1024 // characters of the user's definitions, as the backend takes
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
let setupKey = '' // stands for the packages and macros: they change every picture
let resolvedFg: string | undefined
let resolvedCell: string | undefined
let backendDown: string | undefined
let problemHandler: (reason: string) => void = () => {}
let batches = 0
let wake: (() => void) | undefined
// Ends the worker's gathering wait sooner: after `ms`, or at once for 0.
let hurry: ((ms: number) => void) | undefined
let isUrgent = false // flush() came while the worker was not gathering: its next gathering ends at once
let lastCallAt = Number.NEGATIVE_INFINITY // performance.now() of the last backend call
let worker = 0 // the running worker's number; an older one stops
let cacheBytes = 0

const cache = new Map<string, Result>()
const queue = new Map<string, Job>()
const inFlight = new Set<string>()
const retries = new Map<string, number>()
const renders = new Map<string, number>()
const errors: { latex: string; error: string }[] = []
// Which drawn sites wait for a queued formula, and how often each was asked to redraw.
const waiters = new Map<string, Set<string>>()
const redraws = new Map<string, number>()
let diagramsShown = false

function number(value: unknown, fallback: number, low: number, high: number): number {
  const n = typeof value === 'number' ? value : Number(value)

  return Number.isFinite(n) && n >= low && n <= high ? n : fallback
}

/**
 * The `packages` option: names split at commas, semicolons and white space,
 * or a list of names. The backend checks each name (and says which it
 * skipped in --check), so any word is passed on.
 */
function readPackages(value: unknown): string[] {
  const words = Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : typeof value === 'string' ? value.split(/[\s,;]+/) : []
  const out: string[] = []
  for (const word of words) {
    const name = word.trim().slice(0, 64)
    if (name !== '' && !out.includes(name) && out.length < MAX_PACKAGES) out.push(name)
  }

  return out
}

/** A short stable hash (FNV-1a), for keys. */
function hash(text: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }

  return h.toString(36)
}

/** The manifest's userConfig values, checked, with defaults for anything off. */
export function readSettings(options: PluginOptions): Settings {
  const fg = String(options.foreground ?? 'auto').trim()
  const cell = String(options.cell ?? 'auto').trim()
  const macros = typeof options.macros === 'string' ? options.macros.replace(/\r\n?/g, '\n').trim() : ''

  return {
    enabled: options.enabled !== false,
    foreground: /^#[0-9a-fA-F]{6}$/.test(fg) ? fg.toLowerCase() : 'auto',
    cell: /^\d{1,3}x\d{1,3}$/.test(cell) ? cell : 'auto',
    baseline: number(options.baseline, DEFAULTS.baseline, 0.3, 0.95),
    ptPx: number(options.ptPx, DEFAULTS.ptPx, 1, 12),
    minShrink: number(options.minShrink, DEFAULTS.minShrink, 0.3, 1),
    promptHint: options.promptHint !== false,
    packages: readPackages(options.packages),
    macros: macros.length <= MAX_MACROS ? macros : '',
    diagramColor: options.diagramColor !== false,
  }
}

/** Takes the settings the module loaded with. */
export function configure(next: Settings, options: PluginOptions = {}) {
  settings = next
  setupKey = next.packages.length > 0 || next.macros !== '' ? hash(JSON.stringify([next.packages, next.macros])) : ''
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

/** An item error the backend gives for a passing reason, not for the formula. */
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
  return [currentFg(), currentCell(), settings.baseline, settings.ptPx, settings.minShrink, setupKey].join(',')
}

/**
 * Light or dark, as the colour formulas are drawn in says (light text: a
 * dark background); undefined while an 'auto' colour is not known yet.
 */
export function theme(): 'light' | 'dark' | undefined {
  const fg = currentFg()
  if (!/^#[0-9a-f]{6}$/i.test(fg)) return undefined
  const [r, g, b] = [1, 3, 5].map(at => parseInt(fg.slice(at, at + 2), 16) / 255) as [number, number, number]

  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5 ? 'dark' : 'light'
}

/** A diagram was drawn: a light/dark switch must redraw it, formulas or not. */
export function noteDiagram() {
  diagramsShown = true
  isDrawn = true
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
function enqueue(ref: MathRef, maxColumns: number, eager = true): boolean {
  const key = keyOf(ref, maxColumns)
  if (inFlight.has(key)) return true
  const queued = queue.get(key)
  if (queued) {
    if (eager && !queued.eager) {
      queued.eager = true
      hurry?.(COALESCE_MS)
    }

    return true
  }
  if ((renders.get(key) ?? 0) >= RENDER_LIMIT) return false
  queue.set(key, { key, ref, maxColumns, eager })
  wake?.()
  if (eager) hurry?.(COALESCE_MS)

  return true
}

// Set by every lookup, so the poller knows formulas are being drawn.
let isDrawn = false

const EVICTED: Failure = { error: 'more formulas on screen than the picture cache holds', isBackend: true }
// The same object for as long as the backend stays down, so a draw that
// compares what it drew with what is ready now sees no change.
let downFailure: Failure = { error: '', isBackend: true }

function waitFor(key: string, site: string | undefined) {
  if (site === undefined) return
  if (waiters.size > 4 * CACHE_LIMIT) waiters.clear()
  const sites = waiters.get(key)
  if (sites) sites.add(site)
  else waiters.set(key, new Set([site]))
}

/** Queues `key` (noting `site` as waiting for it): 'pending', or EVICTED when it may not be asked for again. */
function ask(ref: MathRef, maxColumns: number, site: string | undefined): PictureState {
  if (!enqueue(ref, maxColumns)) return EVICTED
  waitFor(keyOf(ref, maxColumns), site)

  return 'pending'
}

/**
 * What is ready for `ref` at `width` columns: a picture, an error, or
 * 'pending' (then queued for the worker, with `site` noted as waiting for
 * it). A picture wider than `width` is asked for again at that width, so a
 * long formula shrinks to fit. The same state comes back as the same object
 * until it changes.
 */
export function lookup(ref: MathRef, width: number, site?: string): PictureState {
  isDrawn = true
  if (backendDown !== undefined) {
    if (downFailure.error !== backendDown) downFailure = { error: backendDown, isBackend: true }

    return downFailure
  }
  const base = hit(keyOf(ref, MAX_COLUMNS))
  if (base === undefined) return ask(ref, MAX_COLUMNS, site)
  if ('error' in base || base.columns <= width) return base
  const fit = Math.max(1, Math.min(MAX_COLUMNS, Math.floor(width)))
  const narrow = hit(keyOf(ref, fit))
  if (narrow === undefined) return ask(ref, fit, site)

  return narrow
}

/**
 * Queues `ref` for `width` columns ahead of any draw (a formula seen closed
 * while the reply streams), so its picture is ready when the reply is drawn.
 * Nothing waits for it, so its landing redraws nothing.
 */
export function prefetch(ref: MathRef, width: number) {
  if (backendDown !== undefined) return
  const base = cache.get(keyOf(ref, MAX_COLUMNS))
  if (base === undefined) {
    enqueue(ref, MAX_COLUMNS, false)

    return
  }
  if ('error' in base || base.columns <= width) return
  const fit = Math.max(1, Math.min(MAX_COLUMNS, Math.floor(width)))
  if (!cache.has(keyOf(ref, fit))) enqueue(ref, fit, false)
}

/**
 * Sends the prefetched formulas now: the block they came from has ended,
 * and its draw comes next.
 */
export function flush() {
  if (queue.size === 0) return
  if (hurry) hurry(0)
  else isUrgent = true
}

/**
 * A turn is starting: when the backend was last called a while ago (its
 * daemon may have stopped after its idle time), starts it and has it load
 * what the next formula needs, so the first batch of the reply is a warm one.
 */
export function warmUp($: EngineInterface) {
  if (backendDown !== undefined) return
  const now = performance.now()
  if (now - lastCallAt < WARM_AFTER_MS) return
  lastCallAt = now
  void $.process
    .run(backend($, '--warm'), { stdin: JSON.stringify({ options: setupOptions() }), timeoutMs: CHECK_TIMEOUT_MS })
    .catch(() => {})
}

function takeBatch(): Job[] {
  isUrgent = false // what was queued when flush() came goes now
  const batch: Job[] = []
  // What a draw waits for first, then prefetches.
  const jobs = [...queue.values()].sort((a, b) => Number(b.eager) - Number(a.eager))
  for (const job of jobs) {
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

/** Does a failed run mean python or the backend cannot run at all? */
function isStartFailure(text: string): boolean {
  return /ENOENT|EACCES|not found|No such file|cannot (?:start|run|spawn|execute)|spawn/i.test(text)
}

/** The backend's command line, with `flags`. */
function backend($: EngineInterface, ...flags: string[]): string[] {
  return ['python3', '-I', '-S', `${$.plugin.root}/bin/client.py`, ...flags]
}

/** What every request carries besides its items: the user's packages and macros. */
function setupOptions(): { packages: string[]; macros: string } {
  return { packages: settings.packages, macros: settings.macros }
}

/** One backend call for `batch`. */
async function callRender($: EngineInterface, batch: Job[]): Promise<Outcome> {
  const request = {
    items: batch.map(job => ({ latex: job.ref.latex, display: job.ref.display })),
    options: {
      // The resolved values when known, so each call draws for the key it is
      // filed under and the backend skips its own lookup.
      fg: currentFg(),
      cell: currentCell(),
      baseline: settings.baseline,
      ptPx: settings.ptPx,
      minShrink: settings.minShrink,
      cacheDir: null, // the backend's own: ~/.cache/claude-inline-math
      maxColumns: batch[0]?.maxColumns ?? MAX_COLUMNS,
      ...setupOptions(),
    },
  }
  let run: Awaited<ReturnType<EngineInterface['process']['run']>>
  lastCallAt = performance.now()
  try {
    run = await $.process.run(backend($), {
      stdin: JSON.stringify(request),
      timeoutMs: RUN_TIMEOUT_MS,
    })
  } catch (err) {
    const text = String(err).slice(0, 200)
    if (/time ?out|timed out/i.test(text)) return { failure: `the renderer timed out (${text})`, transient: true, down: false }

    return { failure: `could not run python3: ${text}`, transient: !isStartFailure(text), down: isStartFailure(text) }
  }
  if (run.isStdoutTruncated) return 'split'
  try {
    const answer = JSON.parse(run.stdout) as Answer
    if (Array.isArray(answer?.results)) return { answer }
  } catch {
    // reported below
  }
  const reason = lastLine(run.stderr) || `client.py exited ${run.exitCode} with no answer`
  // A missing interpreter or module breaks every formula: stop asking.
  const down = /ModuleNotFoundError|No module named|can't open file|No such file or directory: .*(?:render|client|renderd)\.py/i.test(reason)

  return { failure: reason, transient: !down, down }
}

/** Runs the backend for `batch` and keeps what it answered; halves a batch whose answer was too big. */
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
 * Lets queued requests gather before a call: COALESCE_MS when a draw waits
 * for one, PREFETCH_MS when all are prefetches (until a draw's request comes,
 * or flush()).
 */
function gather($: EngineInterface): Promise<void> {
  return new Promise<void>(resolve => {
    let isDone = false
    const done = () => {
      if (isDone) return
      isDone = true
      hurry = undefined
      resolve()
    }
    hurry = ms => {
      if (ms <= 0) done()
      else $.clock.after(ms, done)
    }
    if (isUrgent) {
      done()

      return
    }
    const eager = [...queue.values()].some(job => job.eager)
    $.clock.after(eager ? COALESCE_MS : PREFETCH_MS, done)
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
    await gather($)
    while (queue.size > 0 && me === worker) {
      const batch = takeBatch()
      const isLeftOver = queue.size > 0 // already gathered: goes after this call without a wait
      try {
        await runBatch($, batch)
      } catch {
        // runBatch keeps its own failures; nothing reaches here but a bug
      } finally {
        for (const job of batch) inFlight.delete(job.key)
      }
      if (me === worker) await redrawWaiting($, batch)
      // What came during the call gathers as the first requests did (a
      // stream's prefetches would otherwise go one or two to a call).
      if (!isLeftOver && queue.size > 0 && queue.size < BATCH_ITEMS && me === worker) await gather($)
    }
  }
}

/**
 * Draws again the sites that waited for a formula of `batch`, and no other:
 * one write each to their member of the `redraw` family, which they read
 * while drawing. Where that write is refused, one redraw of everything.
 */
async function redrawWaiting($: EngineInterface, batch: Job[]) {
  const sites = new Set<string>()
  for (const job of batch) {
    for (const site of waiters.get(job.key) ?? []) sites.add(site)
    waiters.delete(job.key)
  }
  if (sites.size === 0) return
  if (redraws.size > 4 * CACHE_LIMIT) redraws.clear()
  const written = await Promise.all(
    [...sites].map(async site => {
      const count = (redraws.get(site) ?? 0) + 1
      redraws.set(site, count)
      try {
        await $.state.set({ ...REDRAW, id: site }, count)

        return true
      } catch {
        return false
      }
    }),
  )
  if (written.includes(false)) $.ui.invalidate('ui.render')
}

async function runCheck($: EngineInterface): Promise<Health> {
  lastCallAt = performance.now()
  try {
    const run = await $.process.run(backend($, '--check'), {
      stdin: JSON.stringify({ options: setupOptions() }),
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
      problems: [`client.py --check failed: ${String(err).slice(0, 300)}`],
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
 * Runs `client.py --check` and adopts what it found: a broken backend (told
 * once, then every formula shows as text), a repaired one, or a new colour or
 * cell size (the desktop switched light and dark, the font size changed).
 */
export async function check($: EngineInterface): Promise<Health> {
  const health = await runCheck($)
  if (!health.ok) {
    reportDown(health.problems.join('; ') || 'the renderer is not ready')

    return health
  }
  const wasDown = backendDown !== undefined
  backendDown = undefined
  const changed = adopt(health)
  if ((changed && (cache.size > 0 || diagramsShown)) || wasDown) $.ui.invalidate('ui.render')

  return health
}

/** `client.py --detect`: the 'auto' colour and cell size alone, without a test render. */
async function detect($: EngineInterface) {
  try {
    const run = await $.process.run(backend($, '--detect'), {
      timeoutMs: DETECT_TIMEOUT_MS,
    })
    if (adopt(JSON.parse(run.stdout) as { fg?: unknown; cell?: unknown }) && (cache.size > 0 || diagramsShown)) $.ui.invalidate('ui.render')
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
    if (!auto || (cache.size === 0 && !diagramsShown)) return
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
  waiters.clear()
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
