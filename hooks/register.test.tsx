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
type Request = { items: Item[]; options: { fg: string; cell: string } }
type Backend = { batches: Item[][]; checks: number; detects: number; engineTexts: string[]; requests: Request[] }
// What a test's render.py answers instead of the usual: a result for the
// call, or undefined for the usual one.
type Respond = (request: Request) => { value: unknown } | { deny: string } | undefined

const RUN = { origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 80 } }

function ran(stdout: string) {
  return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
}

// Stands for the host: render.py (healthy unless told), uname, the command
// registry, settings and the store.
function host(on: On, health: object = HEALTHY, respond?: Respond, detected: object = { cell: { width: 19, height: 42 }, fg: '#4c4f69' }): Backend {
  const backend: Backend = { batches: [], checks: 0, detects: 0, engineTexts: [], requests: [] }
  mock.store(on)
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('process.run', (_, e) => {
    if (e.argv[0] === 'uname') return ran('Linux\n')
    if (e.argv.includes('--check')) {
      backend.checks += 1

      return ran(JSON.stringify(health))
    }
    if (e.argv.includes('--detect')) {
      backend.detects += 1

      return ran(JSON.stringify(detected))
    }
    const request = JSON.parse(e.init?.stdin ?? '{}') as Request
    backend.batches.push(request.items)
    backend.requests.push(request)
    const answer = respond?.(request)
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

const reply = (text: string, isFirstOfReply = true) => ({
  plugin: 'inline-math',
  component: 'AssistantMessage' as const,
  requestId: 'msg-1',
  viewport: { columns: 80, rows: 40 },
  props: { text, isFirstOfReply },
})

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
