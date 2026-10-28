import { describe, expect, test } from 'claude-code/testing'
import { stringWidth } from './layout'
import { isMermaidInfo, renderMermaid, type DiagramResult, type DiagramRole, type DiagramSegment } from './mermaid'

type Drawn = Extract<DiagramResult, { ok: true }>

function drawn(source: string, maxColumns = 100): Drawn {
  const result = renderMermaid(source, maxColumns)
  if (!result.ok) {
    throw new Error(`expected a drawing, got: ${result.reason}`)
  }

  return result
}

function refused(source: string, maxColumns = 100): string {
  const result = renderMermaid(source, maxColumns)
  if (result.ok) {
    throw new Error(`expected a refusal, got:\n${lines(result).join('\n')}`)
  }

  return result.reason
}

function lines(result: Drawn): string[] {
  return result.rows.map(row => row.map(segment => segment.text).join(''))
}

function roles(result: Drawn): Set<DiagramRole> {
  return new Set(result.rows.flat().map(segment => segment.role))
}

function textOf(result: Drawn, role: DiagramRole): string {
  return result.rows.map(row => row.filter(segment => segment.role === role).map(segment => segment.text).join('')).join('\n')
}

function rowWidth(row: DiagramSegment[]): number {
  return row.reduce((sum, segment) => sum + stringWidth(segment.text), 0)
}

// The drawing as terminal cells: a wide character fills its cell and leaves
// the next one empty.
function cells(result: Drawn): string[][] {
  return result.rows.map(row => {
    const out: string[] = []
    for (const segment of row) {
      for (const char of segment.text) {
        if (stringWidth(char) === 0 && out.length > 0) {
          out[out.length - 1] += char
          continue
        }
        out.push(char)
        if (stringWidth(char) === 2) {
          out.push('')
        }
      }
    }

    return out
  })
}

// Every box (┌ ┐ / └ ┘, or the rounded ones) has its side lines in the same
// columns on every row in between: a label of the wrong width would push the
// right side out of line.
function expectBoxesAligned(result: Drawn): number {
  const grid = cells(result)
  let boxes = 0
  grid.forEach((row, r) => {
    row.forEach((cell, c) => {
      if (cell !== '┌' && cell !== '╭') {
        return
      }
      let right = c + 1
      while (/^[─┬┴┼]$/.test(row[right] ?? '')) {
        right++
      }
      if (!/^[┐╮]$/.test(row[right] ?? '')) {
        return
      }
      let bottom = r + 1
      while (bottom < grid.length && /^[│├┤┼]$/.test(grid[bottom]?.[c] ?? '')) {
        expect(/^[│├┤┼►◄▶◀]$/.test(grid[bottom]?.[right] ?? '')).toBe(true)
        bottom++
      }
      if (/^[└╰]$/.test(grid[bottom]?.[c] ?? '')) {
        expect(/^[┘╯]$/.test(grid[bottom]?.[right] ?? '')).toBe(true)
        boxes++
      }
    })
  })

  return boxes
}

function expectFits(result: Drawn, maxColumns: number): void {
  const widths = result.rows.map(rowWidth)
  expect(Math.max(...widths)).toBe(result.columns)
  expect(result.columns).toBeLessThanOrEqual(maxColumns)
  expect(result.rows.length).toBeLessThanOrEqual(80)
}

function tree(nodes: number, salt = ''): string {
  const out = ['flowchart TD']
  for (let i = 1; i < nodes; i++) {
    const parent = Math.floor((i - 1) / 2)
    out.push(`  N${parent}[Node ${parent}] --> N${i}[Node ${i}]`)
  }

  return out.join('\n') + salt
}

function chain(nodes: number, direction: string): string {
  return [`flowchart ${direction}`, ...Array.from({ length: nodes - 1 }, (_, i) => `  A${i} --> A${i + 1}`)].join('\n')
}

describe('isMermaidInfo', () => {
  test('the first word of the fence info, any case', () => {
    expect(isMermaidInfo('mermaid')).toBe(true)
    expect(isMermaidInfo('Mermaid')).toBe(true)
    expect(isMermaidInfo(' MERMAID title="x"')).toBe(true)
    expect(isMermaidInfo('mermaidjs')).toBe(false)
    expect(isMermaidInfo('ts mermaid')).toBe(false)
    expect(isMermaidInfo('')).toBe(false)
  })
})

describe('flowcharts', () => {
  test('top-down with CJK labels keeps every box aligned', () => {
    const result = drawn('flowchart TD\n  A[开始] --> B{是否继续?}\n  B -->|是| C[处理数据]\n  B -->|否| D[结束 🎉]', 80)
    expectFits(result, 80)
    expect(expectBoxesAligned(result)).toBeGreaterThanOrEqual(3)
    const labels = textOf(result, 'label')
    for (const word of ['开始', '是否继续?', '处理数据', '结束 🎉', '是', '否']) {
      expect(labels).toContain(word)
    }
    expect(roles(result)).toEqual(new Set<DiagramRole>(['text', 'border', 'edge', 'label']))
    expect(textOf(result, 'edge')).toContain('▼')
  })

  test('left-right with CJK labels', () => {
    const result = drawn('flowchart LR\n  输入[读取 文件] --> 解析[解析] --> 输出[写出 结果]', 80)
    expectFits(result, 80)
    expect(result.rows.length).toBeLessThanOrEqual(5)
    expect(expectBoxesAligned(result)).toBe(3)
    expect(textOf(result, 'edge')).toContain('►')
  })

  test('CJK node ids, bare or shaped; a CJK label between arrow halves stays a label', () => {
    const result = drawn('flowchart TD\n  开始 --> 检查{检查}\n  检查 -- 是 --> 结束\n  检查-->|否|重试\n  重试 --> 结束')
    expect(expectBoxesAligned(result)).toBeGreaterThanOrEqual(2)
    const text = lines(result).join('\n')
    for (const word of ['开始', '检查', '是', '否', '结束', '重试']) {
      expect(text).toContain(word)
    }
    expect(text).not.toContain('__n')
    expect(text.split('开始')).toHaveLength(2)
  })

  test('a diagram with a loop is refused before its layout, not shown wrong', () => {
    // The library draws every back edge wrongly: over another edge, with its
    // label moved, without heads, or through a box.
    const loop = 'has a loop'
    expect(refused('flowchart TD\n  开始 --> 检查{检查}\n  检查 -- 是 --> 结束\n  检查-->|否|开始')).toContain(loop)
    expect(refused('flowchart LR\n  A[公式 formula] --> B{closed?}\n  B -->|yes| C[picture]\n  B -->|no| A', 75)).toContain(loop)
    expect(refused('flowchart TD; A-->B{Valid?}; B-->|Yes|C; B-->|No|D; D-->B')).toContain(loop)
    expect(refused('flowchart TD\n  A[加载数据] --> B[前向传播]\n  B --> C[计算损失]\n  C --> D{收敛?}\n  D -->|否| B\n  D -->|是| E[保存模型]')).toContain(loop)
    expect(refused('graph LR\n  A --> B\n  B --> C\n  C --> A')).toContain(loop)
    expect(refused('flowchart TD\n  A --> A\n  A --> B')).toContain(loop)
    expect(refused('flowchart LR\n  Client -->|request| Server\n  Server -->|response| Client')).toContain(loop)
    expect(refused('flowchart TD\n  A -- 是 --> B\n  B ==> A')).toContain(loop)
    expect(refused('stateDiagram-v2\n  [*] --> Idle\n  Idle --> Running: start\n  Running --> Idle: stop\n  Running --> [*]')).toContain(loop)
    // Lines without a direction, a two-way arrow and [*] at both ends are no loop.
    drawn('flowchart TD\n  A -- yes --> B\n  B -.-> C\n  C --- A')
    drawn('flowchart LR\n  A <--> B\n  B --> C')
    drawn('stateDiagram-v2\n  [*] --> A\n  A --> B\n  B --> [*]')
  })

  test('a drawing that drops or cuts an edge is refused, not shown wrong', () => {
    const lost = 'dropped or cut an edge'
    // An edge into a node drawn on the bottom row leaves the canvas.
    expect(refused('graph TD\n Client --> API\n API --> Auth\n API --> DB[(Database)]\n Auth --> DB\n API --> Cache\n Cache --> DB')).toContain(lost)
    // Two edges into one node may share its arrowhead; a fan-in keeps its heads.
    expect(lines(drawn('flowchart TD\n A --> B\n A --> C\n B --> D\n C --> D')).join('\n')).toContain('▼')
    drawn('flowchart TD\n A & B --> C\n C --> D & E')
    drawn('flowchart LR\n A <--> B\n B --> C')
  })

  test("the demo's diagram keeps every edge, at the widths /inline-math demo is drawn in", () => {
    const source = 'flowchart LR\n  A[回复 reply] --> B{formula?}\n  B -->|yes| C[picture]\n  B -->|no| D[text]'
    for (const width of [60, 75, 95, 120]) {
      const text = lines(drawn(source, width)).join('\n')
      expect(text.match(/[►▼◄▲]/g)).toHaveLength(3)
      for (const word of ['回复 reply', 'formula?', 'picture', 'text', 'yes', 'no']) {
        expect(text).toContain(word)
      }
    }
  })

  test('styling and click statements draw nothing; entity codes and markdown strings read as text', () => {
    const text = lines(drawn('flowchart TD\n  A:::red --> B["`**bold** text`"]\n  classDef red fill:#f00\n  style B fill:#0f0\n  linkStyle 0 stroke:#f00\n  click A "http://x"\n  B --> C["a #quot;q#quot; #lt;b#gt; #35;1"]')).join('\n')
    expect(text).not.toContain('click')
    expect(text).not.toContain('`')
    expect(text).toContain('bold text')
    expect(text).toContain('a ”q” <b> #1')
  })

  test('a big flowchart is refused before its layout, and a width failure holds for narrower widths', () => {
    const nested = ['flowchart TD']
    for (let s = 0; s < 15; s++) {
      nested.push(`subgraph G${s}`)
      for (let i = 0; i < 4; i++) nested.push(`G${s}N${i} --> G${s}N${i + 1}`)
      nested.push('end')
    }
    let started = performance.now()
    expect(refused(nested.join('\n'))).toContain('nodes in subgraphs')
    expect(performance.now() - started).toBeLessThan(20)
    const fan = ['flowchart TD', ...Array.from({ length: 120 }, (_, i) => `R --> L${i}`)]
    expect(refused(fan.join('\n'))).toContain('more than 100')
    const wide = `flowchart TD\n  R --> ${Array.from({ length: 14 }, (_, i) => `N${i}[step number ${i}]`).join(' & ')}`
    expect(refused(wide, 60)).toContain('columns wide')
    started = performance.now()
    for (const width of [59, 50, 41, 30]) expect(refused(wide, width)).toContain('columns wide')
    expect(performance.now() - started).toBeLessThan(2)
  })

  test('a left-right chart too wide for the space is drawn top-down', () => {
    const source = 'flowchart LR\n  A[第一步] --> B[第二步] --> C[第三步] --> D[第四步] --> E[第五步]'
    const wide = drawn(source, 200)
    expect(wide.columns).toBeGreaterThan(40)
    const narrow = drawn(source, 40)
    expectFits(narrow, 40)
    expect(narrow.rows.length).toBeGreaterThan(wide.rows.length)
    expect(expectBoxesAligned(narrow)).toBe(5)
  })

  test('the compact forms Claude writes: one line, semicolons, glued arrows', () => {
    const result = drawn('graph TD; A-->B; B-->C;')
    expect(lines(result).join('\n')).not.toContain('--')
    expect(textOf(result, 'label').replace(/\s+/g, '')).toBe('ABC')
    expect(textOf(result, 'edge').replace(/[^▼]/g, '')).toBe('▼▼')
    expect(expectBoxesAligned(result)).toBe(3)
  })

  test('long, dotted, thick and labelled arrows; a bare header is top-down', () => {
    const result = drawn('flowchart\n  A---->B\n  B-.->C\n  C==>D\n  D-- yes -->E\n  E-->|no|F\n  F --o G')
    const labels = textOf(result, 'label')
    for (const word of ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'yes', 'no']) {
      expect(labels).toContain(word)
    }
    expect(lines(result).join('\n')).not.toMatch(/[A-G]-|-[A-G]/)
  })

  test('markdown emphasis and inline HTML are dropped from labels', () => {
    const text = lines(drawn('flowchart TD\n  A["**Bold** and <b>tag</b> and *it*"] --> B[x<br/>y]')).join('\n')
    expect(text).toContain('Bold and tag and it')
    expect(text).not.toMatch(/<\/?b>|\*/)
  })

  test('front matter and %% comments are skipped', () => {
    const result = drawn('---\ntitle: demo\n---\n%%{init: {"theme": "dark"}}%%\n%% a comment\nflowchart LR\n  A --> B')
    expect(textOf(result, 'label').replace(/\s+/g, '')).toBe('AB')
  })

  test('control and bidi characters never reach the drawing', () => {
    const text = lines(drawn('flowchart LR\n  A["red\x1b[31m text‮"] --> B')).join('\n')
    expect(text).not.toMatch(/[\x00-\x08\x0b-\x1f\x7f‮]/)
    expect(text).toContain('red[31m text')
  })
})

describe('other diagram types', () => {
  test('sequence', () => {
    const result = drawn('sequenceDiagram\n  participant 用户\n  participant Server\n  用户->>Server: 请求 data\n  Server-->>用户: reply\n  Note over 用户,Server: 备注')
    expectFits(result, 100)
    expect(expectBoxesAligned(result)).toBeGreaterThanOrEqual(4)
    const labels = textOf(result, 'label')
    for (const word of ['用户', 'Server', '请求 data', 'reply', '备注']) {
      expect(labels).toContain(word)
    }
    expect(textOf(result, 'edge')).toMatch(/[▶◀]/)
  })

  test('state, with CJK state names', () => {
    const result = drawn('stateDiagram-v2\n  [*] --> 空闲\n  空闲 --> 忙碌 : 开始\n  忙碌 --> [*]')
    expectFits(result, 100)
    expect(expectBoxesAligned(result)).toBeGreaterThanOrEqual(2)
    const labels = textOf(result, 'label')
    for (const word of ['空闲', '忙碌', '开始']) {
      expect(labels).toContain(word)
    }
  })

  test('class', () => {
    const result = drawn('classDiagram\n  class Animal {\n    +String name\n    +eat() void\n  }\n  Animal <|-- Duck\n  Animal <|-- 鱼')
    expectFits(result, 100)
    expect(expectBoxesAligned(result)).toBeGreaterThanOrEqual(3)
    const labels = textOf(result, 'label')
    for (const word of ['Animal', 'Duck', '鱼', 'name']) {
      expect(labels).toContain(word)
    }
    expect(lines(result).join('\n')).toContain('△')
  })

  test('entity relationship', () => {
    const result = drawn('erDiagram\n  CUSTOMER ||--o{ ORDER : places\n  CUSTOMER {\n    string name PK\n  }')
    expectFits(result, 100)
    const labels = textOf(result, 'label')
    for (const word of ['CUSTOMER', 'ORDER', 'places', 'name']) {
      expect(labels).toContain(word)
    }
    expect(roles(result).has('border')).toBe(true)
  })

  test('xychart-beta: bars and lines are edges, the grid is dim', () => {
    const result = drawn('xychart-beta\n  title "Sales"\n  x-axis [jan, feb, mar]\n  y-axis "Revenue" 0 --> 100\n  bar [10, 50, 90]\n  line [20, 40, 80]')
    expectFits(result, 100)
    expect(textOf(result, 'edge')).toContain('█')
    expect(textOf(result, 'dim')).toMatch(/^[·\n]+$/)
    expect(textOf(result, 'label')).toContain('Sales')
    expect(textOf(result, 'label')).toContain('feb')
  })
})

describe('the library draws only one-cell characters', () => {
  // `columns` counts every character the library draws as one cell; the
  // drawer measures with stringWidth. Labels here are ASCII, so every other
  // character in the drawing is the library's own.
  test('every shape, edge style and relation', () => {
    const sources = [
      'flowchart LR\n  a((c)) --> b(((dc))) --> c{{h}} --> d>f] --> e[(d)]\n  e --> f([s]) --> g[[sub]] --> h[/t\\] --> i[\\u/] --> j{dia}\n  a -.-> j\n  a ==> f\n  b --- g\n  c <--> h',
      'flowchart BT\n  subgraph S [box]\n    x --> y\n  end\n  y -.- z\n  x === z',
      'classDiagram\n  A <|-- B\n  A *-- C\n  A o-- D\n  A <.. E\n  A ..|> F\n  A --> G : uses\n  class H {\n    <<interface>>\n    +run() void\n  }',
      'erDiagram\n  A ||--o{ B : has\n  B }|..|{ C : links\n  C |o--|| D : one',
      'stateDiagram-v2\n  [*] --> a\n  a --> b : go\n  state b {\n    [*] --> c\n  }\n  b --> [*]',
      'sequenceDiagram\n  A->>B: x\n  B-->>A: y\n  A-)B: z\n  A-xB: w\n  loop l\n    B->>A: v\n  end\n  Note right of B: n',
      'xychart-beta\n  x-axis [a, b]\n  bar [1, 2]\n  line [2, 1]',
    ]
    const seen = new Set<string>()
    for (const source of sources) {
      for (const row of drawn(source, 200).rows) {
        for (const segment of row) {
          for (const char of segment.text) {
            seen.add(char)
            expect(`${char} ${stringWidth(char)}`).toBe(`${char} 1`)
          }
        }
      }
    }
    expect(seen.size).toBeGreaterThan(30)
  })
})

describe('refusals', () => {
  test('unsupported types name themselves', () => {
    expect(refused('pie\n  "a": 1\n  "b": 2')).toBe('pie diagrams are not supported')
    expect(refused('gantt\n  title x')).toBe('gantt diagrams are not supported')
    expect(refused('%% note\nmindmap\n  root')).toBe('mindmap diagrams are not supported')
    expect(refused('   \n\n')).toBe('diagram is empty')
  })

  test('parse errors', () => {
    expect(refused('flowchart XY\n  A --> B')).toStartWith('diagram could not be parsed: ')
    expect(refused('sequenceDiagram\n  A->>')).toBe('diagram is empty or could not be parsed')
  })

  test('a crash inside the library is a refusal, not an exception', () => {
    // beautiful-mermaid 1.1.3 can't lay out a cycle that enters a subgraph;
    // the loop check now refuses it before the library sees it.
    expect(refused('flowchart TD\n  subgraph S\n    x --> y\n  end\n  y --> z\n  z --> x')).toContain('has a loop')
  })

  test('too wide', () => {
    expect(refused(chain(12, 'LR').replace('flowchart LR', 'flowchart TD').replace(/A0 -->/, 'A0 & Z1 & Z2 & Z3 & Z4 & Z5 -->'), 20)).toMatch(/^diagram is \d+ columns wide, 20 available$/)
  })

  test('too tall', () => {
    expect(refused(chain(30, 'TD'), 40)).toMatch(/^diagram is \d+ (rows tall, limit 80|columns wide, 40 available)$/)
    expect(refused(chain(30, 'TD'), 400)).toMatch(/^diagram is \d+ rows tall, limit 80$/)
  })

  test('too long', () => {
    expect(refused(`flowchart TD\n${'  A --> B\n'.repeat(800)}`)).toBe('diagram source is over 8000 characters')
    expect(refused(`flowchart TD\n${'  A --> B\n'.repeat(150)}`)).toBe('diagram source is over 150 lines')
  })

  test('a deep chain ends (the vendored layout once looped forever past depth 24)', () => {
    const started = performance.now()
    const result = renderMermaid(chain(40, 'LR'), 100)
    expect(result.ok).toBe(false)
    expect(performance.now() - started).toBeLessThan(2000)
  })
})

describe('memo and speed', () => {
  test('the same source and width give the same result object', () => {
    const source = 'flowchart LR\n  A --> B'
    expect(renderMermaid(source, 50)).toBe(renderMermaid(source, 50))
    expect(renderMermaid(source, 50)).not.toBe(renderMermaid(source, 51))
  })

  test('a 30-node flowchart draws in under 20 ms after warmup', () => {
    for (let i = 0; i < 5; i++) {
      drawn(tree(30, `\n%% warm ${i}`), 200)
    }
    const times: number[] = []
    for (let i = 0; i < 7; i++) {
      const source = tree(30, `\n%% run ${i}`)
      const started = performance.now()
      const result = renderMermaid(source, 200)
      times.push(performance.now() - started)
      expect(result.ok).toBe(true)
    }
    times.sort((a, b) => a - b)
    expect(times[3] as number).toBeLessThan(20)
  })
})
