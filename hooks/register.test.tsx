import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

// A 1x1 transparent PNG: what render.py's pictures stand for here.
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='

const HEALTHY = {
  ok: true,
  latex: true,
  dvipng: true,
  pil: true,
  cell: { width: 19, height: 42 },
  fg: '#4c4f69',
  problems: [],
}

type Item = { latex: string; display: boolean }
type Request = { items: Item[]; options: { fg: string; cell: string; packages?: string[]; macros?: string } }
type Backend = { batches: Item[][]; checks: number; detects: number; engineTexts: string[]; requests: Request[]; argv: string[][]; checkStdin: string[] }
// What a test's render.py answers instead of the usual: a result for the
// call, or undefined for the usual one.
type Respond = (request: Request) => { value: unknown } | { deny: string } | undefined | Promise<undefined>

const RUN = { origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 80 } }

function ran(stdout: string) {
  return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
}

// Stands for the host: render.py (healthy unless told), uname, the command
// registry, settings and the store.
function host(on: On, health: object = HEALTHY, respond?: Respond, detected: object = { cell: { width: 19, height: 42 }, fg: '#4c4f69' }): Backend {
  const backend: Backend = { batches: [], checks: 0, detects: 0, engineTexts: [], requests: [], argv: [], checkStdin: [] }
  mock.store(on)
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('process.run', async (_, e) => {
    if (e.argv[0] === 'uname') return ran('Linux\n')
    backend.argv.push([...e.argv])
    if (e.argv.includes('--check')) {
      backend.checks += 1
      backend.checkStdin.push(e.init?.stdin ?? '')

      return ran(JSON.stringify(health))
    }
    if (e.argv.includes('--detect')) {
      backend.detects += 1

      return ran(JSON.stringify(detected))
    }
    if (e.argv.includes('--warm')) return ran(JSON.stringify({ daemon: null }))
    const request = JSON.parse(e.init?.stdin ?? '{}') as Request
    backend.batches.push(request.items)
    backend.requests.push(request)
    const answer = await respond?.(request)
    if (answer) return answer as ReturnType<typeof ran>
    const results = request.items.map(item =>
      item.latex.includes('\\broken') ? { error: '! Undefined control sequence.' } : { png: PNG, columns: item.display ? 12 : 4, rows: item.display ? 2 : 1, textRow: 0 },
    )

    return ran(JSON.stringify({ cell: { width: 19, height: 42 }, fg: '#4c4f69', results }))
  })
  // The engine's own drawing, for whatever the mod hands on.
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    if (e.component === 'AssistantMessage') backend.engineTexts.push(e.props.text)

    return <Text>engine</Text>
  })

  return backend
}

const reply = (text: string, isFirstOfReply = true, requestId = 'msg-1', columns = 80) => ({
  plugin: 'inline-math',
  component: 'AssistantMessage' as const,
  requestId,
  viewport: { columns, rows: 40 },
  props: { text, isFirstOfReply },
})

const STEP = { turnId: 'turn-1', index: 0, model: 'm', messageCount: 1 }

// Records the redraws the plugin asks for: its `redraw` state writes (by
// site) and any whole-event invalidate.
function redraws(on: On): { sites: string[]; invalidates: number } {
  const seen = { sites: [] as string[], invalidates: 0 }
  on('state.set', (_, e, next) => {
    if (e.key === 'redraw' && typeof e.id === 'string') seen.sites.push(e.id)

    return next(e)
  })
  on('ui.invalidate', (_, e, next) => {
    if (e.event === 'ui.render') seen.invalidates += 1

    return next(e)
  })

  return seen
}

describe('replies', () => {
  test('text draws at once, pictures land after one batched call', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)

    const ui = await $.ui.mount({ ...reply('Energy $E=mc^2$ and **bold $\\alpha$** `code`, see [docs](https://example.com).\n\n$$\\int_0^1 x\\,dx$$\n\n- item $y$\n- plain item'), surface: 'terminal' })
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(0)
    expect(await ui.find({ type: 'Text', text: /Energy/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '●' })).toBeDefined()

    await clock.advance(100)
    expect(backend.batches).toHaveLength(1)
    expect(backend.batches[0]).toHaveLength(4)
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(4)

    // Everything ready: a redraw asks for nothing more.
    await clock.advance(1000)
    expect(backend.batches).toHaveLength(1)
    await ui.unmount()
  })

  test('a formula TeX refuses stays readable text and is not asked for again', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)

    const ui = await $.ui.mount({ ...reply('Bad $\\broken{x}$ and good $x^2$.'), surface: 'terminal' })
    await clock.advance(100)
    await clock.advance(1000)
    expect(backend.batches).toHaveLength(1)
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(1)
    await ui.unmount()
  })

  test('the formula of an unclosed tail is not asked for', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)

    const ui = await $.ui.mount({ ...reply('Done $a+b$ and streaming $c+'), surface: 'terminal' })
    await clock.advance(100)
    expect(backend.batches.flat().map(item => item.latex)).toEqual(['a+b'])
    await ui.unmount()
  })

  test('replies without math, summaries and other surfaces are the engine\'s', async ($, on) => {
    host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

    for (const surface of ['terminal', 'desktop'] as const) {
      const plain = await $.ui.mount({ ...reply('No formulas, $5 and $10.'), surface })
      expect((await plain.find({ type: 'Text' }))?.text).toBe('engine')
      await plain.unmount()
    }
    const summary = await $.ui.mount({ ...reply('Has $x$.'), props: { text: 'Has $x$.', isFirstOfReply: true, isSummary: true as const }, surface: 'terminal' })
    expect((await summary.find({ type: 'Text' }))?.text).toBe('engine')
    await summary.unmount()
    const desktop = await $.ui.mount({ ...reply('Has $x$.'), surface: 'desktop' })
    expect((await desktop.find({ type: 'Text' }))?.text).toBe('engine')
    await desktop.unmount()
  })

  test('a backend that cannot run is told once and replies stay text', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on, { ok: false, latex: false, dvipng: false, pil: true, problems: ['latex not found'] })
    const toasts: string[] = []
    on('ui.toast', (_, e) => {
      toasts.push(e.text)

      return { value: undefined }
    })
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)

    const ui = await $.ui.mount({ ...reply('Has $x^2$ and $y$.'), surface: 'terminal' })
    await clock.advance(100)
    expect(backend.batches).toHaveLength(0)
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(0)
    expect(await ui.find({ type: 'Text', text: /Has/ })).toBeDefined()
    expect(toasts).toHaveLength(1)
    expect(toasts[0]).toContain('latex not found')
    await ui.unmount()
  })
})

describe('edge cases', () => {
  test('math only in a table reaches the engine as Unicode', async ($, on) => {
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    const ui = await $.ui.mount({ ...reply('| sort | time |\n|---|---|\n| merge | $O(n \\log n)$ |'), surface: 'terminal' })
    expect((await ui.find({ type: 'Text' }))?.text).toBe('engine')
    expect(backend.engineTexts.at(-1)).toBe('| sort | time |\n|---|---|\n| merge | O(n log n) |')
    expect(backend.batches).toHaveLength(0)
    await ui.unmount()
  })

  test('a link too long for the terminal is drawn as plain text, not refused', async ($, on) => {
    mock.clock(on)
    host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    const ui = await $.ui.mount({ ...reply(`See [the docs](https://example.com/${'a'.repeat(2100)}) and $q_1$.`), surface: 'terminal' })
    expect(await ui.find({ type: 'Text', text: 'engine' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /the docs/ })).toBeDefined()
    expect(await ui.findAll({ type: 'Link' })).toHaveLength(0)
    await ui.unmount()
  })

  test('an answer too big for one call is fetched in halves', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on, HEALTHY, request =>
      request.items.length > 1 ? { value: { exitCode: 0, stdout: '{"cell":', stderr: '', isStdoutTruncated: true, isStderrTruncated: false } } : undefined,
    )
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply('Big $h_1$, $h_2$ and $h_3$.'), surface: 'terminal' })
    await clock.advance(100)
    expect(backend.batches.map(batch => batch.length)).toEqual([3, 2, 1, 1, 1])
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(3)
    await ui.unmount()
  })

  test('a call that times out is tried again, and the backend stays up', async ($, on) => {
    const clock = mock.clock(on)
    let calls = 0
    const backend = host(on, HEALTHY, () => (++calls === 1 ? { deny: 'timed out after 90000 ms' } : undefined))
    const toasts: string[] = []
    on('ui.toast', (_, e) => {
      toasts.push(e.text)

      return { value: undefined }
    })
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply('Slow $t_1$ here.'), surface: 'terminal' })
    await clock.advance(100)
    await clock.advance(100)
    expect(backend.batches).toHaveLength(2)
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(1)
    expect(toasts).toHaveLength(0)
    await ui.unmount()
  })

  test('a fixed colour is sent as set, and polling never redraws for it', { options: { foreground: '#ff0000' } }, async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on, HEALTHY, undefined, { cell: { width: 19, height: 42 }, fg: '#cdd6f4' })
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply('Red $r_1$ here.'), surface: 'terminal' })
    await clock.advance(100)
    expect(backend.requests[0]?.options.fg).toBe('#ff0000')
    await clock.advance(30_000)
    expect(backend.detects).toBeGreaterThan(0)
    expect(backend.batches).toHaveLength(1)
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(1)
    await ui.unmount()
  })

  test('a desktop switch to dark draws the formulas again within seconds', async ($, on) => {
    const clock = mock.clock(on)
    const detected = { cell: { width: 19, height: 42 }, fg: '#4c4f69' }
    const backend = host(on, HEALTHY, undefined, detected)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply('Theme $d_1$ here.'), surface: 'terminal' })
    await clock.advance(100)
    expect(backend.batches).toHaveLength(1)
    detected.fg = '#cdd6f4'
    await clock.advance(6000)
    await clock.advance(100)
    expect(backend.batches).toHaveLength(2)
    expect(backend.requests[1]?.options.fg).toBe('#cdd6f4')
    await ui.unmount()
  })
})

describe('cache bounds', () => {
  test('more formulas on screen than the cache holds do not loop', { options: { cacheEntries: 3 } }, async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await $.command.run({ ...RUN, command: 'inline-math', args: 'clear' })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply('Many $m_1$ $m_2$ $m_3$ $m_4$ $m_5$.'), surface: 'terminal' })
    const rounds = async () => {
      for (let i = 0; i < 12; i++) {
        await clock.advance(500)
        await ui.findAll({ type: 'Image' })
      }
    }
    await rounds()
    const settled = backend.batches.length
    expect(settled).toBeGreaterThan(1) // it did evict and ask again
    await rounds()
    expect(backend.batches.length).toBe(settled)
    expect(settled).toBeLessThan(12)
    await ui.unmount()
    await $.command.run({ ...RUN, command: 'inline-math', args: 'clear' })
  })
})

describe('commands and prompt', () => {
  test('off leaves replies to the engine, on draws them again', async ($, on) => {
    host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

    await $.command.run({ ...RUN, command: 'inline-math', args: 'off' })
    const off = await $.ui.mount({ ...reply('Has $x$.'), surface: 'terminal' })
    expect((await off.find({ type: 'Text' }))?.text).toBe('engine')
    await off.unmount()

    await $.command.run({ ...RUN, command: 'inline-math', args: 'on' })
    const back = await $.ui.mount({ ...reply('Has $x$.'), surface: 'terminal' })
    expect(await back.find({ type: 'Text', text: 'engine' })).toBeUndefined()
    await back.unmount()
  })

  test('status reports the backend', async ($, on) => {
    host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    const { text } = await $.command.run({ ...RUN, command: 'inline-math', args: 'status' })
    expect(text).toContain('backend: ready')
    expect(text).toContain('19x42')
  })

  test('the system prompt gets the hint on a terminal', async ($, on) => {
    host(on)
    on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'core', scope: 'shared' as const }] }))
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    const base = { model: 'm', promptModel: 'm', tools: [], outputStyle: null, traits: [] }

    const composed = await $.prompt.compose({ ...base, surfaces: ['terminal'] })
    expect(composed.sections.map(section => section.id)).toEqual(['intro', 'inline-math:hint'])
    const hint = composed.sections[1]?.text ?? ''
    expect(hint).toContain('$$...$$')
    expect(hint).toContain('mermaid')
    // Under 100 tokens: at most about 4 characters a token in English prose.
    expect(hint.length).toBeLessThan(400)
    const remote = await $.prompt.compose({ ...base, surfaces: ['desktop'] })
    expect(remote.sections.map(section => section.id)).toEqual(['intro'])
  })

  test('promptHint off adds nothing', { options: { promptHint: false } }, async ($, on) => {
    host(on)
    on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'core', scope: 'shared' as const }] }))
    const composed = await $.prompt.compose({ model: 'm', promptModel: 'm', tools: [], outputStyle: null, traits: [], surfaces: ['terminal'] })
    expect(composed.sections).toHaveLength(1)
  })
})

describe('backend', () => {
  test('runs client.py with -I -S and sends packages and macros, also to --check', { options: { packages: 'physics, siunitx;\n  mhchem', macros: '\\newcommand{\\RR}{\\mathbb{R}}' } }, async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply('Uses $\\RR^n$.'), surface: 'terminal' })
    await clock.advance(100)
    expect(backend.argv[0]?.slice(0, 3)).toEqual(['python3', '-I', '-S'])
    expect(backend.argv.every(argv => argv[3]?.endsWith('/bin/client.py'))).toBe(true)
    expect(backend.requests[0]?.options.packages).toEqual(['physics', 'siunitx', 'mhchem'])
    expect(backend.requests[0]?.options.macros).toBe('\\newcommand{\\RR}{\\mathbb{R}}')
    expect(JSON.parse(backend.checkStdin[0] ?? '{}').options.packages).toEqual(['physics', 'siunitx', 'mhchem'])
    await ui.unmount()
  })
})

describe('redraws', () => {
  test('a landed batch redraws only the replies that waited for it, never the whole transcript', async ($, on) => {
    const clock = mock.clock(on)
    const seen = redraws(on)
    host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const a = await $.ui.mount({ ...reply('First $a_1$ here.', true, 'msg-a'), surface: 'terminal' })
    await clock.advance(100)
    expect(seen.sites).toEqual(['msg-a'])
    expect(await a.findAll({ type: 'Image' })).toHaveLength(1)

    const b = await $.ui.mount({ ...reply('Second $b_1$ and $b_2$.', true, 'msg-b'), surface: 'terminal' })
    await clock.advance(100)
    expect(seen.sites).toEqual(['msg-a', 'msg-b'])
    expect(await b.findAll({ type: 'Image' })).toHaveLength(2)
    expect(seen.invalidates).toBe(0)

    // Everything ready: drawing both again asks for nothing.
    await clock.advance(1000)
    expect(seen.sites).toEqual(['msg-a', 'msg-b'])
    await a.unmount()
    await b.unmount()
  })

  test('a ready reply drawn again returns the same tree, and a new width lays it out again', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const input = { ...reply('Same $s_1$ and $s_2$.', true, 'msg-same'), surface: 'terminal' as const }
    await $.ui.render(input)
    await clock.advance(100)
    const first = await $.ui.render(input)
    const again = await $.ui.render(input)
    expect(JSON.stringify(again)).toBe(JSON.stringify(first))
    const wide = await $.ui.render({ ...input, viewport: { columns: 120, rows: 40 } })
    expect(JSON.stringify(wide)).not.toBe(JSON.stringify(first))
    expect(backend.batches).toHaveLength(1)
  })

  test('benchmark: 50 replies of 20 formulas, drawn again once ready', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const replies = Array.from({ length: 50 }, (_, r) => {
      const paragraphs = Array.from({ length: 4 }, (_, p) =>
        Array.from({ length: 5 }, (_, k) => `term ${k} is $x_{${r}}^{${p * 5 + k}} + \\frac{1}{${k + 2}}$`).join(', ') + '.',
      )

      return { ...reply(paragraphs.join('\n\n'), r % 2 === 0, `bench-${r}`), surface: 'terminal' as const }
    })
    const plain = { ...reply('No formulas in this reply at all, just some prose.', true, 'bench-plain'), surface: 'terminal' as const }
    for (const input of replies) await $.ui.render(input)
    for (let round = 0; round < 10 && backend.batches.flat().length < 1000; round++) await clock.advance(100)
    expect(backend.batches.flat()).toHaveLength(1000)
    // The engine's own floor: a reply the hook hands straight on.
    let t0 = performance.now()
    for (let round = 0; round < 10; round++) for (let r = 0; r < 50; r++) await $.ui.render(plain)
    const floor = (performance.now() - t0) / 500
    t0 = performance.now()
    for (let round = 0; round < 10; round++) for (const input of replies) await $.ui.render(input)
    const ready = (performance.now() - t0) / 500
    t0 = performance.now()
    for (let round = 0; round < 3; round++) for (const input of replies) await $.ui.render({ ...input, viewport: { columns: 100 + round, rows: 40 } })
    const relayout = (performance.now() - t0) / 150
    ;(globalThis as { console?: { log: (text: string) => void } }).console?.log(`bench: per reply drawn again ${ready.toFixed(3)} ms (engine floor ${floor.toFixed(3)} ms); laid out anew ${relayout.toFixed(3)} ms`)
    expect(ready - floor).toBeLessThan(5)
    expect(backend.batches.flat()).toHaveLength(1000 + 3 * 0)
  })
})

describe('streaming', () => {
  const CHUNKS = [
    { kind: 'thinking' as const, index: 0, text: 'hmm $x$' },
    { kind: 'text' as const, index: 1, text: 'Energy is $E' },
    { kind: 'text' as const, index: 1, text: '=mc^2$, and ' },
    { kind: 'text' as const, index: 1, text: 'price $5; then $\\alp' },
    { kind: 'text' as const, index: 1, text: 'ha$ ends.\n\n```\n$code$\n```\n' },
  ]

  function model(on: On, gate: Promise<void>) {
    on('turn.step', async function* () {
      yield CHUNKS[0] as (typeof CHUNKS)[number]
      yield CHUNKS[1] as (typeof CHUNKS)[number]
      yield CHUNKS[2] as (typeof CHUNKS)[number]
      await gate
      yield CHUNKS[3] as (typeof CHUNKS)[number]
      yield CHUNKS[4] as (typeof CHUNKS)[number]

      return { turnId: 'turn-1', index: 0, answer: 'Energy...', toolUses: [], stopReason: 'end_turn' as const, usage: null }
    })
  }

  test('every chunk passes unchanged and in order; a formula closed mid-stream is queued before the stream ends', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    let open = () => {}
    model(on, new Promise<void>(resolve => {
      open = resolve
    }))
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const stream = $.turn.step(STEP)
    const out: unknown[] = []
    for (let k = 0; k < 3; k++) out.push((await stream.next()).value)
    // The stream is still open: E=mc^2 closed in the third chunk, and waits
    // a while for more formulas to share its backend call.
    await clock.advance(150)
    expect(backend.batches).toHaveLength(0)
    await clock.advance(400)
    expect(backend.batches.flat().map(item => item.latex)).toEqual(['E=mc^2'])
    open()
    for (;;) {
      const next = await stream.next()
      if (next.done) {
        expect((next.value as { answer: string }).answer).toBe('Energy...')
        break
      }
      out.push(next.value)
    }
    expect(out).toEqual(CHUNKS)
    // The block has ended: what it closed last is sent at once, not after the gathering wait.
    await clock.advance(0)
    // The price is no formula, the thinking and the code are not read.
    expect(backend.batches.flat().map(item => item.latex)).toEqual(['E=mc^2', '\\alpha'])

    // The finished block draws with its pictures at once and asks for nothing.
    const ui = await $.ui.mount({ ...reply('Energy is $E=mc^2$, and price $5; then $\\alpha$ ends.'), surface: 'terminal' })
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(2)
    await clock.advance(200)
    expect(backend.batches).toHaveLength(2)
    await ui.unmount()
  })

  test('a block that ends at a tool call sends its last formulas at once, even during a scan cooldown', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    const chunks = [
      { kind: 'text' as const, index: 0, text: 'We have $a+b$ and ' },
      { kind: 'text' as const, index: 0, text: 'also $c+d$.' },
      { kind: 'tool' as const, index: 1, id: 'call-1', name: 'Read' },
    ]
    on('turn.step', async function* () {
      for (const chunk of chunks) yield chunk

      return { turnId: 'turn-1', index: 0, answer: '', toolUses: [], stopReason: 'tool_use' as const, usage: null }
    })
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const stream = $.turn.step(STEP)
    const out: unknown[] = []
    for (let k = 0; k < 3; k++) out.push((await stream.next()).value)
    // c+d closed while the block was cooling after the a+b scan; the tool chunk ended the block.
    await clock.advance(0)
    expect(backend.batches.flat().map(item => item.latex)).toEqual(['a+b', 'c+d'])
    expect(backend.batches).toHaveLength(1)
    expect(out).toEqual(chunks)
    await stream.next()
  })

  test('streamed formulas gather into few backend calls', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    const chunks = Array.from({ length: 40 }, (_, k) => ({ kind: 'text' as const, index: 0, text: `step ${k}: $x_{${k}}$ ` }))
    on('turn.step', async function* () {
      for (const chunk of chunks) {
        yield chunk
        await clock.advance(25)
      }

      return { turnId: 'turn-1', index: 0, answer: '', toolUses: [], stopReason: 'end_turn' as const, usage: null }
    })
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    for await (const _ of $.turn.step(STEP)) {
      // passed on
    }
    await clock.advance(0)
    expect(backend.batches.flat()).toHaveLength(40)
    // One second of stream: a call per PREFETCH_MS at most, plus the block's end, not one per formula.
    expect(backend.batches.length).toBeLessThanOrEqual(5)
  })

  test('formulas streamed while a call runs gather for the next call too', async ($, on) => {
    const clock = mock.clock(on)
    // A backend that answers 100 ms (four chunks) later, as a warm daemon does.
    let now = 0
    const calls: { due: number; answer: () => void }[] = []
    const backend = host(on, HEALTHY, () => new Promise(resolve => calls.push({ due: now + 100, answer: () => resolve(undefined) })))
    const answerDue = () => {
      for (const call of calls.filter(c => c.due <= now)) {
        calls.splice(calls.indexOf(call), 1)
        call.answer()
      }
    }
    const chunks = Array.from({ length: 40 }, (_, k) => ({ kind: 'text' as const, index: 0, text: `step ${k}: $x_{${k}}$ ` }))
    on('turn.step', async function* () {
      for (const chunk of chunks) {
        yield chunk
        now += 25
        answerDue()
        await clock.advance(25)
      }

      return { turnId: 'turn-1', index: 0, answer: '', toolUses: [], stopReason: 'end_turn' as const, usage: null }
    })
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    for await (const _ of $.turn.step(STEP)) {
      // passed on
    }
    for (let k = 0; k < 20; k++) {
      now += 25
      answerDue()
      await clock.advance(25)
    }
    expect(backend.batches.flat()).toHaveLength(40)
    // Without the second gathering each call took the 4 that came during the last one: 8 calls.
    expect(backend.batches.length).toBeLessThanOrEqual(4)
  })

  test('off: the stream is passed through and nothing is queued', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    model(on, Promise.resolve())
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await $.command.run({ ...RUN, command: 'inline-math', args: 'off' })
    const out: unknown[] = []
    for await (const chunk of $.turn.step(STEP)) out.push(chunk)
    await clock.advance(200)
    expect(out).toEqual(CHUNKS)
    expect(backend.batches).toHaveLength(0)
    await $.command.run({ ...RUN, command: 'inline-math', args: 'on' })
  })
})

describe('reply macros', () => {
  test('the renderer gets the expanded formula, and a definition-only formula draws nothing', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply('$$\\newcommand{\\norm}[1]{\\lVert #1 \\rVert}$$\n\nSo $\\norm{v} \\le 1$.'), surface: 'terminal' })
    await clock.advance(100)
    expect(backend.batches.flat()).toEqual([{ latex: '\\lVert v \\rVert \\le 1', display: false }])
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(1)
    await ui.unmount()
  })
})

describe('mermaid', () => {
  const FLOW = 'Here:\n\n```mermaid\nflowchart LR\n  A[开始] --> B[End]\n```\n\nDone.'

  test('a mermaid block draws as coloured box art, with no backend call', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply(FLOW), surface: 'terminal' })
    expect(await ui.find({ type: 'Text', text: 'engine' })).toBeUndefined()
    const texts = await ui.findAll({ type: 'Text' })
    const art = texts.map(text => text.text ?? '').join('\n')
    expect(art).toMatch(/[┌─│└]/)
    expect(art).toContain('开始')
    expect(await ui.findAll({ type: 'Markdown' })).toHaveLength(2) // the prose around it
    // Light text colour (Latte): borders in Latte blue.
    expect(JSON.stringify(await ui.drawn())).toContain('#1e66f5')
    await clock.advance(100)
    expect(backend.batches).toHaveLength(0)
    await ui.unmount()
  })

  test('diagramColor off draws it in the text colour alone', { options: { diagramColor: false } }, async ($, on) => {
    const clock = mock.clock(on)
    host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply(FLOW), surface: 'terminal' })
    const tree = JSON.stringify(await ui.drawn())
    expect(tree).not.toContain('#1e66f5')
    expect(tree).not.toContain('#7287fd')
    await ui.unmount()
  })

  test('a diagram it cannot draw shows its source and one dim reason line', async ($, on) => {
    const clock = mock.clock(on)
    host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const ui = await $.ui.mount({ ...reply('```mermaid\npie title Pets\n  "Dogs" : 386\n```'), surface: 'terminal' })
    const markdown = await ui.findAll({ type: 'Markdown' })
    expect(markdown.map(item => (item.props as { text?: string }).text)).toEqual(['```mermaid\npie title Pets\n  "Dogs" : 386\n```'])
    expect(await ui.find({ type: 'Text', text: /mermaid diagram shown as source/ })).toBeDefined()
    await ui.unmount()
  })
})

describe('status', () => {
  test('lists daemon, format, packages, macros and diagram colour', { options: { packages: 'physics', macros: '\\newcommand{\\a}{1}\n\\DeclareMathOperator{\\tr}{tr}' } }, async ($, on) => {
    host(on, { ...HEALTHY, daemon: { pid: 4242, uptime: 61.2, served: 7, warm: 1 }, format: { ok: true, path: '/c/fmt/x.fmt' } })
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    const { text } = await $.command.run({ ...RUN, command: 'inline-math', args: 'status' })
    expect(text).toContain('daemon: running (pid 4242, up 61 s, 7 requests served)')
    expect(text).toContain('format: precompiled (/c/fmt/x.fmt)')
    expect(text).toContain('packages: physics; macros: 2 definitions')
    expect(text).toContain('diagrams: colour')
  })

  test('no daemon, no format, or a backend that does not say', async ($, on) => {
    host(on, { ...HEALTHY, daemon: null, format: { ok: false } })
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    const { text } = await $.command.run({ ...RUN, command: 'inline-math', args: 'status' })
    expect(text).toContain('daemon: not reached')
    expect(text).toContain('format: not built')
    expect(text).toContain('packages: none extra; macros: 0 definitions')
  })

  test('a backend that does not say has unknown daemon and format', async ($, on) => {
    host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    const { text } = await $.command.run({ ...RUN, command: 'inline-math', args: 'status' })
    expect(text).toContain('daemon: unknown')
    expect(text).toContain('format: unknown')
  })
})

describe('demo', () => {
  test('the demo output draws formulas, a reply macro and a diagram', async ($, on) => {
    const clock = mock.clock(on)
    const backend = host(on)
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(0)
    const { text } = await $.command.run({ ...RUN, command: 'inline-math', args: 'demo' })
    const ui = await $.ui.mount({
      plugin: 'inline-math',
      component: 'CommandOutput',
      requestId: 'demo-1',
      viewport: { columns: 100, rows: 40 },
      props: { command: 'inline-math', args: 'demo', text: text ?? '', isErrored: false },
      surface: 'terminal',
    })
    await clock.advance(100)
    expect(await ui.findAll({ type: 'Image' })).not.toHaveLength(0)
    const latex = backend.batches.flat().map(item => item.latex)
    expect(latex).toContain('\\operatorname{tr} A \\in \\mathbb{R}')
    expect(latex.some(item => item.includes('newcommand'))).toBe(false)
    const art = (await ui.findAll({ type: 'Text' })).map(found => found.text ?? '').join('\n')
    expect(art).toContain('回复 reply')
    expect(art).toContain('picture')
    expect(art).toContain('text')
    expect(art).not.toContain('shown as source')
    await ui.unmount()
  })
})
