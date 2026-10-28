import { describe, expect, test } from 'claude-code/testing'
import {
  expandMacros,
  hasDiagram,
  hasMath,
  layoutParagraph,
  mathRefs,
  parseMessage,
  stringWidth,
  toUnicode,
  type Block,
  type Line,
  type MathRef,
  type Picture,
  type PictureState,
} from './layout'

type Paragraph = Extract<Block, { kind: 'paragraph' }>

function picture(columns: number, rows = 1, textRow = 0): Picture {
  return { png: `png-${columns}x${rows}@${textRow}`, columns, rows, textRow }
}

// A picture source that knows some formulas and leaves the rest pending.
function pictures(table: Record<string, PictureState>): (ref: MathRef) => PictureState {
  return ref => table[ref.latex] ?? 'pending'
}

// A line as the terminal shows it: a picture is drawn as '#' per column.
function lineText(line: Line): string {
  return line.runs.map(run => (run.kind === 'image' ? '#'.repeat(run.picture.columns) : run.text)).join('')
}

function lineWidth(line: Line): number {
  return line.runs.reduce((sum, run) => sum + (run.kind === 'image' ? run.picture.columns : stringWidth(run.text)), 0)
}

function onlyParagraph(markdown: string): Paragraph {
  const blocks = parseMessage(markdown)
  const found = blocks.filter((block): block is Paragraph => block.kind === 'paragraph')
  expect(found).toHaveLength(1)

  return found[0] as Paragraph
}

function paragraph(markdown: string, width: number, table: Record<string, PictureState> = {}): string[] {
  return layoutParagraph(onlyParagraph(markdown), pictures(table), width).map(lineText)
}

describe('stringWidth', () => {
  test('ASCII, CJK, fullwidth and Hangul', () => {
    expect(stringWidth('')).toBe(0)
    expect(stringWidth('abc def')).toBe(7)
    expect(stringWidth('中文abc')).toBe(7)
    expect(stringWidth('，。')).toBe(4)
    expect(stringWidth('Ａ１')).toBe(4)
    expect(stringWidth('한국어')).toBe(6)
    expect(stringWidth('ｱｲ')).toBe(2)
    expect(stringWidth('𠀀')).toBe(2)
  })

  test('emoji are wide, marks and joiners take no cells', () => {
    expect(stringWidth('😀')).toBe(2)
    expect(stringWidth('👨‍👩‍👧')).toBe(2)
    expect(stringWidth('❤️')).toBe(2)
    expect(stringWidth('🇨🇳')).toBe(2)
    expect(stringWidth('e\u0301')).toBe(1)
    expect(stringWidth('a\u200bb')).toBe(2)
    expect(stringWidth('x̂')).toBe(1)
    expect(stringWidth('v⃗')).toBe(1)
  })

  test('math symbols and Greek are narrow', () => {
    expect(stringWidth('α∈ℝ≤∞±×')).toBe(7)
  })
})

describe('toUnicode', () => {
  const cases: [string, string][] = [
    ['\\alpha + \\beta = \\Gamma', 'α + β = Γ'],
    ['a \\cdot b \\times c \\pm d', 'a ⋅ b × c ± d'],
    ['x \\le y \\ge z \\ne w \\approx v', 'x ≤ y ≥ z ≠ w ≈ v'],
    ['f: A \\to B', 'f: A → B'],
    ['x \\in A \\notin B \\subset C \\cup D \\cap E', 'x ∈ A ∉ B ⊂ C ∪ D ∩ E'],
    ['\\forall x \\exists y', '∀ x ∃ y'],
    ['\\partial f / \\partial x, \\nabla f', '∂ f / ∂ x, ∇ f'],
    ['\\infty', '∞'],
    ['x^2 + y^{10}', 'x² + y¹⁰'],
    ['x_i + a_{n+1}', 'xᵢ + aₙ₊₁'],
    ['A^{-1} A^T', 'A⁻¹ Aᵀ'],
    ['e^{i\\pi}', 'e^(iπ)'],
    ['x^*', 'x^*'],
    ['x_{\\text{max}}', 'xₘₐₓ'],
    ['x_{\\text{best}}', 'x_(best)'],
    ['\\frac{1}{2}', '½'],
    ['\\frac{a}{b}', 'a/b'],
    ['\\frac{a+b}{c}', '(a+b)/c'],
    ['\\dfrac{1}{n+1}', '1/(n+1)'],
    ['\\sqrt{2}', '√2'],
    ['\\sqrt{x+1}', '√(x+1)'],
    ['\\sqrt[3]{x}', '∛x'],
    ['\\mathbb{R}^n', 'ℝⁿ'],
    ['\\mathbb{N} \\mathbb{Z} \\mathbb{Q} \\mathbb{C}', 'ℕ ℤ ℚ ℂ'],
    ['\\mathcal{L}', 'ℒ'],
    ['\\hat{x}', 'x̂'],
    ['\\bar{x}', 'x̄'],
    ['\\vec{v}', 'v⃗'],
    ['\\left( \\frac{a}{b} \\right)', '( a/b )'],
    ['\\left. x \\right|', 'x |'],
    ['\\text{if } x > 0', 'if x > 0'],
    ['a\\,b\\;c\\quad d\\!e', 'a b c de'],
    ['\\sin\\theta + \\cos^2 x', 'sin θ + cos² x'],
    ['\\log_2 n', 'log₂ n'],
    ['O(n \\log n)', 'O(n log n)'],
    ['\\sum_{i=1}^{n} x_i', '∑ᵢ₌₁ⁿ xᵢ'],
    ['\\frac{1}{n}\\sum_i x_i', '1/n ∑ᵢ xᵢ'],
    ['\\int_0^1 f(x)\\,dx', '∫₀¹ f(x) dx'],
    ['\\prod_{k} a_k', '∏ₖ aₖ'],
    ['\\lim_{x \\to 0}', 'lim_(x→0)'],
    ['\\ce{H2O}', 'H₂O'],
    ['\\ce{SO4^2- + 2H2O -> X}', 'SO₄²⁻ + 2H₂O → X'],
    ['\\SI{3}{\\meter\\per\\second}', '3 m/s'],
    ['\\qty{9.8}{m/s^2}', '9.8 m/s²'],
    ['\\si{\\kilo\\gram}', 'kg'],
    ['\\num{1e6}', '1×10⁶'],
    ['a\\;b', 'a b'],
    ['\\binom{n}{k}', 'C(n, k)'],
    ['\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}', '(a b; c d)'],
    ['|x| = \\begin{cases} x & x \\ge 0 \\\\ -x & x < 0 \\end{cases}', '|x| = { x x ≥ 0; -x x < 0'],
    ['\\not= \\not\\in', '≠ ∉'],
    ['\\operatorname{argmax}_x f', 'argmaxₓ f'],
    ['\\foo{x}', 'foox'],
    ['\\{1, 2\\} \\% \\$', '{1, 2} % $'],
    ['f\'(x)', 'f′(x)'],
    ['\\color{red}{x} \\textcolor{blue}{y}', 'x y'],
    ['a \\pmod{n}', 'a (mod n)'],
    ['\\end{x} y', 'y'],
    // Spacing inside a text argument reads as a space, not as its source.
    ['\\operatorname*{arg\\,min}_x f(x)', 'arg minₓ f(x)'],
    ['\\text{a~b\\quad c, 50\\%}', 'a b c, 50%'],
  ]
  for (const [latex, expected] of cases) {
    test(`${latex} reads ${expected}`, () => {
      expect(toUnicode(latex)).toBe(expected)
    })
  }

  test('the stand-in is one line and never empty', () => {
    expect(toUnicode('\\begin{aligned}\na &= b \\\\\n&= c\n\\end{aligned}')).toBe('a = b; = c')
    expect(toUnicode('\\,')).toBe('\\,')
    expect(toUnicode('{')).toBe('{')
    expect(toUnicode('x^')).toBe('x')
  })
})

describe('hasMath', () => {
  const yes = [
    '$x$',
    'Let $x^2$ be.',
    'inline \\(a+b\\) here',
    '$$\\int f$$',
    '\\[\nx = 1\n\\]',
    '# Heading $\\alpha$',
    '| a |\n|---|\n| $x$ |',
    '> quote $y$',
    '- item $z$',
    'I have $5 and you have $x$',
  ]
  const no = [
    '',
    'plain text',
    'Prices are $5 and $10.',
    'between $10 and $20',
    'costs $ 5 $ total',
    'escaped \\$x\\$ dollars',
    'code `$x$` span',
    '```\n$x$\n```',
    '~~~tex\n$$x$$\n~~~',
    '    $x$ indented code',
    'stream $x^',
    'stream $$\\int_0',
    '$x $',
    '$x$5',
  ]
  for (const text of yes) {
    test(`finds math in ${JSON.stringify(text)}`, () => {
      expect(hasMath(text)).toBe(true)
    })
  }
  for (const text of no) {
    test(`finds no math in ${JSON.stringify(text)}`, () => {
      expect(hasMath(text)).toBe(false)
    })
  }

  test('agrees with parseMessage', () => {
    for (const text of [...yes, ...no]) {
      const blocks = parseMessage(text)
      const drawn = mathRefs(blocks).length > 0
      const converted = blocks.length !== 1 || blocks[0]?.kind !== 'markdown' || blocks[0].text !== text
      expect(drawn || converted).toBe(hasMath(text))
    }
  })
})

describe('parseMessage: blocks', () => {
  test('a reply without math is one verbatim block', () => {
    const text = '# Title\n\nSome *text* with $5.\n\n```js\nconst a = 1\n```\n'
    expect(parseMessage(text)).toEqual([{ kind: 'markdown', text }])
  })

  test('blocks without math merge, keeping their text and blank lines', () => {
    const text = 'First para.\n\n\nSecond   para *x*.\n\n```\n$code$\n```\n\n- a\n- b\n\nMath $y$ here.\n\nAfter one.\n\n---\n\nAfter two.'
    expect(parseMessage(text)).toEqual([
      { kind: 'markdown', text: 'First para.\n\n\nSecond   para *x*.\n\n```\n$code$\n```\n\n- a\n- b' },
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'Math ', style: {} }, { kind: 'math', latex: 'y' }, { kind: 'text', text: ' here.', style: {} }], indent: 0, gapBefore: true },
      { kind: 'markdown', text: 'After one.\n\n---\n\nAfter two.', gapBefore: true },
    ])
  })

  test('a heading with math becomes markdown with a Unicode stand-in', () => {
    expect(parseMessage('## The map $f: \\mathbb{R} \\to \\mathbb{R}$\n\nText.')).toEqual([
      { kind: 'markdown', text: '## The map f: ℝ → ℝ\n\nText.' },
    ])
  })

  test('a setext heading with math is converted too', () => {
    expect(parseMessage('Title $\\alpha$\n---\n\ntext')).toEqual([{ kind: 'markdown', text: 'Title α\n---\n\ntext' }])
  })

  test('a table with math becomes markdown; stand-ins are escaped', () => {
    const text = '| name | value |\n|---|:-:|\n| $x_1$ | $\\frac{1}{2}$ |\n| $a*b$ | `$c$` |'
    expect(parseMessage(text)).toEqual([
      { kind: 'markdown', text: '| name | value |\n|---|:-:|\n| x₁ | ½ |\n| a\\*b | `$c$` |' },
    ])
  })

  test('a blockquote with math keeps its markers', () => {
    expect(parseMessage('> Note: $\\beta$ is\n> small.\n\nafter')).toEqual([{ kind: 'markdown', text: '> Note: β is\n> small.\n\nafter' }])
  })

  test('math inside a fence or indented code is untouched', () => {
    const text = 'Use $x$:\n\n```tex\n$x$ and $$y$$\n```\n\n    $z$\n'
    const blocks = parseMessage(text)
    expect(blocks[0]?.kind).toBe('paragraph')
    expect(blocks[1]).toEqual({ kind: 'markdown', text: '```tex\n$x$ and $$y$$\n```\n\n    $z$', gapBefore: true })
    expect(mathRefs(blocks)).toEqual([{ latex: 'x', display: false }])
  })

  test('an unclosed fence (streaming) holds everything after it', () => {
    expect(parseMessage('```\n$x$ more')).toEqual([{ kind: 'markdown', text: '```\n$x$ more' }])
  })

  test('a display formula on its own lines is a display block', () => {
    expect(parseMessage('$$\nE = mc^2\n$$')).toEqual([{ kind: 'display', latex: 'E = mc^2', indent: 0 }])
    expect(parseMessage('\\[ a^2 + b^2 \\]')).toEqual([{ kind: 'display', latex: 'a^2 + b^2', indent: 0 }])
  })

  test('display content that looks like markdown stays one formula', () => {
    const latex = '\\begin{aligned}\n- x &= 1 \\\\\n# &= 2 \\\\\n> 0\n\\end{aligned}'
    expect(parseMessage(`So\n$$\n${latex}\n$$\nwhere $y$ holds.`)).toEqual([
      { kind: 'markdown', text: 'So' },
      { kind: 'display', latex, indent: 0 },
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'where ', style: {} }, { kind: 'math', latex: 'y' }, { kind: 'text', text: ' holds.', style: {} }], indent: 0 },
    ])
  })

  test('display math inside a paragraph splits it', () => {
    expect(parseMessage('We get $$x = 1$$ and so $y$.')).toEqual([
      { kind: 'markdown', text: 'We get' },
      { kind: 'display', latex: 'x = 1', indent: 0 },
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'and so ', style: {} }, { kind: 'math', latex: 'y' }, { kind: 'text', text: '.', style: {} }], indent: 0 },
    ])
  })

  test('streaming: an unclosed display formula stays literal', () => {
    expect(parseMessage('Here $a$ and $$\\int_0')).toEqual([
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'Here ', style: {} }, { kind: 'math', latex: 'a' }, { kind: 'text', text: ' and $$\\int_0', style: {} }], indent: 0 },
    ])
  })

  test('mathRefs lists each formula once', () => {
    const blocks = parseMessage('$x$ and $x$ and $y$\n\n$$x$$')
    expect(mathRefs(blocks)).toEqual([
      { latex: 'x', display: false },
      { latex: 'y', display: false },
      { latex: 'x', display: true },
    ])
  })
})

describe('parseMessage: lists', () => {
  test('a nested list with math: every item drawn, markers kept, nesting indents', () => {
    const blocks = parseMessage('- item $a$\n  - nested $b$\n    more text\n- plain\n\nafter')
    expect(blocks).toEqual([
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'item ', style: {} }, { kind: 'math', latex: 'a' }], marker: '- ', indent: 0 },
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'nested ', style: {} }, { kind: 'math', latex: 'b' }, { kind: 'text', text: ' more text', style: {} }], marker: '- ', indent: 2 },
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'plain', style: {} }], marker: '- ', indent: 0 },
      { kind: 'markdown', text: 'after', gapBefore: true },
    ])
  })

  test('ordered markers count up from the first number', () => {
    const blocks = parseMessage('3. three $x$\n3. four\n3) other list')
    expect(blocks.map(block => (block.kind === 'paragraph' ? block.marker : block.kind))).toEqual(['3. ', '4. ', 'markdown'])
  })

  test('a lazy continuation line joins the item', () => {
    const block = onlyParagraph('- the value $v$\ncontinues here')
    expect(block.inlines.at(-1)).toEqual({ kind: 'text', text: ' continues here', style: {} })
  })

  test('a second paragraph in an item hangs under the marker', () => {
    const blocks = parseMessage('1. first $a$\n\n   second $b$\n2. next')
    expect(blocks.map(block => [block.kind, block.kind === 'paragraph' ? (block.marker ?? null) : null, 'indent' in block ? block.indent : null])).toEqual([
      ['paragraph', '1. ', 0],
      ['paragraph', null, 3],
      ['paragraph', '2. ', 0],
    ])
  })

  test('display math and code inside a math list', () => {
    const blocks = parseMessage('- see\n\n  $$x^2$$\n- code:\n\n  ```\n  $raw$\n  ```\n- and $y$')
    expect(blocks).toEqual([
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'see', style: {} }], marker: '- ', indent: 0 },
      { kind: 'display', latex: 'x^2', indent: 2, gapBefore: true },
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'code:', style: {} }], marker: '- ', indent: 0 },
      { kind: 'markdown', text: '```\n$raw$\n```', indent: 2, gapBefore: true },
      { kind: 'paragraph', inlines: [{ kind: 'text', text: 'and ', style: {} }, { kind: 'math', latex: 'y' }], marker: '- ', indent: 0 },
    ])
  })

  test('a list without math stays verbatim markdown', () => {
    const text = '* a\n* b\n\n  c\n\n$x$'
    expect(parseMessage(text)[0]).toEqual({ kind: 'markdown', text: '* a\n* b\n\n  c' })
  })

  test('nested ordered lists are numbered as the engine numbers them', () => {
    const blocks = parseMessage('1) one $x$\n   1. nested $y$\n   2. nested two\n      1. deep\n      2. deeper\n2) two')
    expect(blocks.map(block => (block.kind === 'paragraph' ? block.marker : block.kind))).toEqual(['1. ', 'a. ', 'b. ', 'i. ', 'ii. ', '2. '])
    expect(blocks.map(block => ('indent' in block ? block.indent : null))).toEqual([0, 3, 3, 6, 6, 0])
  })

  test('code inside a list item is drawn under the item text', () => {
    const blocks = parseMessage('- step $x$:\n\n  ```bash\n  make\n  ```\n- next')
    expect(blocks[1]).toEqual({ kind: 'markdown', text: '```bash\nmake\n```', indent: 2, gapBefore: true })
    expect(blocks[2]).toMatchObject({ kind: 'paragraph', marker: '- ' })
    expect(blocks[2]?.gapBefore).toBeUndefined()
  })

  test('a list right under a paragraph has no blank row; a blank line gives one', () => {
    const tight = parseMessage('Steps:\n1. a $x$\n2. b')
    expect(tight.map(block => block.gapBefore ?? false)).toEqual([false, false, false])
    const loose = parseMessage('Steps:\n\n1. a $x$\n\n2. b')
    expect(loose.map(block => block.gapBefore ?? false)).toEqual([false, true, true])
  })

  test('a second paragraph of an item keeps its blank line', () => {
    const blocks = parseMessage('- a $x$\n\n  b second para\n- c')
    expect(blocks.map(block => block.gapBefore ?? false)).toEqual([false, true, false])
  })

  test('a thematic break is not a list', () => {
    expect(parseMessage('* * *\n\n$x$')[0]).toEqual({ kind: 'markdown', text: '* * *' })
  })
})

describe('parseMessage: edge cases', () => {
  test('a $$ that opens a line but is prose does not swallow a code fence', () => {
    const text = '$$ is the shell PID; for example:\n\n```bash\necho $$\n```\n\nThat prints it. Also $x$.'
    const blocks = parseMessage(text)
    expect(blocks.some(block => block.kind === 'display')).toBe(false)
    expect(blocks.find(block => block.kind === 'markdown' && block.text.includes('```bash'))).toBeDefined()
    expect(mathRefs(blocks)).toEqual([{ latex: 'x', display: false }])
  })

  test('a display formula does not run across a blank line', () => {
    expect(parseMessage('Text\n\n$$x\n\ny$$').some(block => block.kind === 'display')).toBe(false)
  })

  test('punctuation after a display formula joins it', () => {
    const blocks = parseMessage('The result is\n$$\nx^2 + y^2 = 1\n$$.\nwhich is a circle.')
    expect(blocks[1]).toEqual({ kind: 'display', latex: 'x^2 + y^2 = 1\\,{.}', indent: 0 })
    expect(blocks[2]).toEqual({ kind: 'markdown', text: 'which is a circle.' })
    expect(parseMessage('So $$x^2$$。下一句')[1]).toEqual({ kind: 'display', latex: 'x^2\\text{。}', indent: 0 })
  })

  test('shell variables in prose are not formulas', () => {
    for (const text of ['echo $HOME/$USER and done', 'Set $PATH:$HOME/bin in your shell.', 'Use ${HOME}/${USER} here.', 'Run $HOME$USER now']) {
      expect(hasMath(text)).toBe(false)
    }
    for (const text of ['the $n$th term', 'segment $AB$ here', 'so $N$ items', 'with $x/2$ and', 'ratio $a:b$ is']) {
      expect(hasMath(text)).toBe(true)
    }
  })

  test('pathological input stays fast', () => {
    const started = Date.now()
    parseMessage(`$x$ ${'[a]('.repeat(5000)}`)
    toUnicode(`${'x^{'.repeat(5000)}1${'}'.repeat(5000)}`)
    expect(Date.now() - started).toBeLessThan(500)
  })
})

describe('parseMessage: inline', () => {
  test('currency is not math', () => {
    expect(onlyParagraph('It costs $5 and $10, so $x$ wins.').inlines).toEqual([
      { kind: 'text', text: 'It costs $5 and $10, so ', style: {} },
      { kind: 'math', latex: 'x' },
      { kind: 'text', text: ' wins.', style: {} },
    ])
  })

  test('a price before a formula does not swallow it', () => {
    expect(onlyParagraph('I have $5 and you have $x$').inlines).toEqual([
      { kind: 'text', text: 'I have $5 and you have ', style: {} },
      { kind: 'math', latex: 'x' },
    ])
  })

  test('escaped dollars are literal', () => {
    expect(onlyParagraph('Pay \\$3 for $y$, \\$4 for \\$z\\$.').inlines).toEqual([
      { kind: 'text', text: 'Pay $3 for ', style: {} },
      { kind: 'math', latex: 'y' },
      { kind: 'text', text: ', $4 for $z$.', style: {} },
    ])
  })

  test('code spans never hold math', () => {
    expect(onlyParagraph('Run `echo $x$` then $y$ and ``a ` $b$``.').inlines).toEqual([
      { kind: 'text', text: 'Run ', style: {} },
      { kind: 'text', text: 'echo $x$', style: { code: true } },
      { kind: 'text', text: ' then ', style: {} },
      { kind: 'math', latex: 'y' },
      { kind: 'text', text: ' and ', style: {} },
      { kind: 'text', text: 'a ` $b$', style: { code: true } },
      { kind: 'text', text: '.', style: {} },
    ])
  })

  test('streaming text ending in "$x^" keeps it literal', () => {
    expect(onlyParagraph('Let $a$ be such that $x^').inlines).toEqual([
      { kind: 'text', text: 'Let ', style: {} },
      { kind: 'math', latex: 'a' },
      { kind: 'text', text: ' be such that $x^', style: {} },
    ])
  })

  test('\\( \\) is inline math', () => {
    expect(onlyParagraph('Take \\(\\alpha_1\\) now.').inlines).toEqual([
      { kind: 'text', text: 'Take ', style: {} },
      { kind: 'math', latex: '\\alpha_1' },
      { kind: 'text', text: ' now.', style: {} },
    ])
  })

  test('bold around math: **设 $x$**', () => {
    expect(onlyParagraph('**设 $x$**是实数.').inlines).toEqual([
      { kind: 'text', text: '设 ', style: { bold: true } },
      { kind: 'math', latex: 'x' },
      { kind: 'text', text: '是实数.', style: {} },
    ])
  })

  test('emphasis kinds', () => {
    expect(onlyParagraph('**b** __b__ *i* _i_ ~~s~~ ***bi*** $x$').inlines).toEqual([
      { kind: 'text', text: 'b', style: { bold: true } },
      { kind: 'text', text: ' ', style: {} },
      { kind: 'text', text: 'b', style: { bold: true } },
      { kind: 'text', text: ' ', style: {} },
      { kind: 'text', text: 'i', style: { italic: true } },
      { kind: 'text', text: ' ', style: {} },
      { kind: 'text', text: 'i', style: { italic: true } },
      { kind: 'text', text: ' ', style: {} },
      { kind: 'text', text: 's', style: { strike: true } },
      { kind: 'text', text: ' ', style: {} },
      { kind: 'text', text: 'bi', style: { bold: true, italic: true } },
      { kind: 'text', text: ' ', style: {} },
      { kind: 'math', latex: 'x' },
    ])
  })

  test('underscores inside words are not emphasis', () => {
    expect(onlyParagraph('snake_case_name and $x$ and 2*3*4').inlines).toEqual([
      { kind: 'text', text: 'snake_case_name and ', style: {} },
      { kind: 'math', latex: 'x' },
      { kind: 'text', text: ' and 2', style: {} },
      { kind: 'text', text: '3', style: { italic: true } },
      { kind: 'text', text: '4', style: {} },
    ])
  })

  test('unmatched delimiters stay literal', () => {
    expect(onlyParagraph('a ** b and $x$ *c').inlines).toEqual([
      { kind: 'text', text: 'a ** b and ', style: {} },
      { kind: 'math', latex: 'x' },
      { kind: 'text', text: ' *c', style: {} },
    ])
  })

  test('links, autolinks and entities', () => {
    expect(onlyParagraph('See [the $x$ docs](https://a.b/c_(d)) or <https://e.f> &amp; $y$').inlines).toEqual([
      { kind: 'text', text: 'See ', style: {} },
      { kind: 'text', text: 'the ', style: { link: 'https://a.b/c_(d)' } },
      { kind: 'math', latex: 'x' },
      { kind: 'text', text: ' docs', style: { link: 'https://a.b/c_(d)' } },
      { kind: 'text', text: ' or ', style: {} },
      { kind: 'text', text: 'https://e.f', style: { link: 'https://e.f' } },
      { kind: 'text', text: ' & ', style: {} },
      { kind: 'math', latex: 'y' },
    ])
  })

  test('hard and soft breaks', () => {
    expect(onlyParagraph('one $a$  \ntwo\\\nthree\nfour').inlines).toEqual([
      { kind: 'text', text: 'one ', style: {} },
      { kind: 'math', latex: 'a' },
      { kind: 'text', text: '\ntwo\nthree four', style: {} },
    ])
  })

  test('a soft break between Chinese characters vanishes', () => {
    expect(onlyParagraph('设 $x$ 为实数,\n且大于零\n继续').inlines.at(-1)).toEqual({ kind: 'text', text: ' 为实数, 且大于零继续', style: {} })
  })

  test('the closing $ must not be followed by a digit or preceded by space', () => {
    expect(hasMath('$x $ and $y$1')).toBe(false)
  })
})

describe('layoutParagraph', () => {
  test('greedy wrap between words', () => {
    expect(paragraph('aaa bbb ccc ddd $x$', 8, { x: picture(1) })).toEqual(['aaa bbb', 'ccc ddd', '#'])
  })

  test('text exactly as wide as the line fits', () => {
    expect(paragraph('aaaa bbb $x$', 10, { x: picture(1) })).toEqual(['aaaa bbb #'])
    expect(paragraph('aaaa bbb $x$', 9, { x: picture(1) })).toEqual(['aaaa bbb', '#'])
  })

  test('a formula stays glued to its punctuation', () => {
    expect(paragraph('aaaa ($x$), bb', 8, { x: picture(3) })).toEqual(['aaaa', '(###),', 'bb'])
    expect(paragraph('a $x$, b', 3, { x: picture(1) })).toEqual(['a', '#,', 'b'])
  })

  test('CJK breaks anywhere, but no line starts with closing punctuation', () => {
    expect(paragraph('设$x$为实数，且大于零。', 8, { x: picture(1) })).toEqual(['设#为实', '数，且大', '于零。'])
    // '，' may not start a line, so it pulls 中 down with it.
    const lines = paragraph('中文中文中，文字 $x$', 10, { x: picture(1) })
    expect(lines).toEqual(['中文中文', '中，文字 #'])
  })

  test('no line ends with an opening bracket', () => {
    const lines = paragraph('文字文（注意$x$）', 8, { x: picture(1) })
    for (const line of lines) {
      expect(line.endsWith('（')).toBe(false)
    }
    expect(lines).toEqual(['文字文', '（注意', '#）'])
  })

  test('a formula next to CJK may break on either side', () => {
    expect(paragraph('设$x$为', 3, { x: picture(1) })).toEqual(['设#', '为'])
    expect(paragraph('设$x$，为', 3, { x: picture(1) })).toEqual(['设', '#，', '为'])
  })

  test('a word wider than the line is cut', () => {
    expect(paragraph('$x$ abcdefghij', 4, { x: picture(1) })).toEqual(['#', 'abcd', 'efgh', 'ij'])
  })

  test('a picture wider than the line stands alone', () => {
    expect(paragraph('ab $x$ cd', 4, { x: picture(9) })).toEqual(['ab', '#########', 'cd'])
  })

  test('a list marker starts the first line; the rest hang under it', () => {
    const block: Paragraph = { kind: 'paragraph', inlines: [{ kind: 'text', text: 'aaa bbb ccc', style: {} }, { kind: 'math', latex: 'x' }], marker: '1. ', indent: 2 }
    expect(layoutParagraph(block, pictures({ x: picture(1) }), 10).map(lineText)).toEqual(['  1. aaa', '     bbb', '     ccc#'])
  })

  test('an indent without a marker applies to every line', () => {
    const block: Paragraph = { kind: 'paragraph', inlines: [{ kind: 'text', text: 'aa bb', style: {} }], indent: 3 }
    expect(layoutParagraph(block, pictures({}), 6).map(lineText)).toEqual(['   aa', '   bb'])
  })

  test('hard breaks start new lines; leading spaces are dropped', () => {
    expect(paragraph('a $x$  \n   b', 20, { x: picture(1) })).toEqual(['a #', 'b'])
  })

  test('R and marginTop with mixed text rows', () => {
    const tall = picture(4, 3, 2)
    const low = picture(2, 2, 0)
    const [line] = layoutParagraph(onlyParagraph('see $a$ and $b$ ok'), pictures({ a: tall, b: low }), 40)
    expect(line?.runs).toEqual([
      { kind: 'text', text: 'see ', style: {}, marginTop: 2 },
      { kind: 'image', picture: tall, latex: 'a', marginTop: 0 },
      { kind: 'text', text: ' and ', style: {}, marginTop: 2 },
      { kind: 'image', picture: low, latex: 'b', marginTop: 2 },
      { kind: 'text', text: ' ok', style: {}, marginTop: 2 },
    ])
    // The low picture hangs one row below the text row.
    expect(line?.height).toBe(4)
  })

  test('a line without pictures is one row; each line has its own R', () => {
    const lines = layoutParagraph(onlyParagraph('aaa bbb $t$'), pictures({ t: picture(3, 2, 1) }), 7)
    expect(lines.map(line => [line.height, line.runs.map(run => run.marginTop)])).toEqual([
      [1, [0]],
      [2, [0]],
    ])
  })

  test('a pending formula draws Unicode and a failed one its source', () => {
    const [line] = layoutParagraph(onlyParagraph('a $\\alpha^2$ b $\\bad$'), pictures({ '\\bad': { error: 'boom' } }), 40)
    expect(line?.runs).toEqual([
      { kind: 'text', text: 'a ', style: {}, marginTop: 0 },
      { kind: 'fallback', text: 'α²', latex: '\\alpha^2', marginTop: 0, isError: false },
      { kind: 'text', text: ' b ', style: {}, marginTop: 0 },
      { kind: 'fallback', text: '$\\bad$', latex: '\\bad', marginTop: 0, isError: true },
    ])
  })

  test('with the renderer down a formula draws Unicode', () => {
    const [line] = layoutParagraph(onlyParagraph('$\\alpha^2$'), () => ({ error: 'no latex', isBackend: true }), 40)
    expect(line?.runs).toEqual([{ kind: 'fallback', text: 'α²', latex: '\\alpha^2', marginTop: 0, isError: true }])
  })

  test('a long stand-in is cut like text', () => {
    const lines = layoutParagraph(onlyParagraph('$\\alpha\\beta\\gamma\\delta\\epsilon\\zeta$'), pictures({}), 4)
    expect(lines.map(lineText)).toEqual(['αβγδ', 'ϵζ'])
    expect(lines.every(line => line.runs.every(run => run.kind === 'fallback'))).toBe(true)
  })

  test('runs of the same style merge; styles stay apart', () => {
    const [line] = layoutParagraph(onlyParagraph('plain **bold** more **b2** $x$'), pictures({ x: picture(1) }), 80)
    expect(line?.runs.map(run => (run.kind === 'text' ? [run.text, run.style] : run.kind))).toEqual([
      ['plain ', {}],
      ['bold', { bold: true }],
      [' more ', {}],
      ['b2', { bold: true }],
      [' ', {}],
      'image',
    ])
  })

  test('no line is wider than the width, but for a lone picture', () => {
    const texts = [
      'Euler 公式 $e^{i\\pi} + 1 = 0$ 把 $e$, $i$, $\\pi$ 连在一起. 设 $x_i \\in \\mathbb{R}$, 则均值 $\\bar{x}$.',
      'A long English sentence wraps at the pane width, with $\\alpha_i$, $\\nabla f(x)$ and $O(n \\log n)$ inside it.',
      '- **bold $a$ text** with `code spans` and [links](https://x.y) and verylongwordwithoutanyspaces $b$',
    ]
    const table: Record<string, PictureState> = {
      'e^{i\\pi} + 1 = 0': picture(9, 2, 1),
      e: picture(1),
      i: picture(1),
      '\\pi': picture(1),
      'x_i \\in \\mathbb{R}': picture(6, 2, 0),
      '\\alpha_i': picture(2, 2, 0),
      a: picture(1),
      b: 'pending',
    }
    for (const text of texts) {
      for (const width of [1, 2, 3, 4, 5, 8, 13, 21, 34, 80]) {
        for (const line of layoutParagraph(onlyParagraph(text), pictures(table), width)) {
          const images = line.runs.filter(run => run.kind === 'image')
          const isLonePicture = images.length === 1 && lineText(line).trim() === '#'.repeat(images[0]?.kind === 'image' ? images[0].picture.columns : 0)
          // Below 5 cells a wide character or the list marker alone may overflow.
          if (width >= 5 && !isLonePicture) {
            expect(lineWidth(line)).toBeLessThanOrEqual(width)
          }
          expect(line.height).toBeGreaterThan(0)
          expect(lineText(line)).not.toMatch(/^\s*$|\s$/)
        }
      }
    }
  })
})

describe('reply macros', () => {
  test('\\newcommand with arguments and an optional first argument', () => {
    expect(expandMacros('\\R^n', ['\\newcommand{\\R}{\\mathbb{R}}'])).toBe('\\mathbb{R}^n')
    expect(expandMacros('\\norm{x}', ['\\newcommand{\\norm}[1]{\\left\\lVert #1 \\right\\rVert}'])).toBe('\\left\\lVert x \\right\\rVert')
    expect(expandMacros('\\pair{a}{b} + \\pair a b', ['\\newcommand\\pair[2]{(#1, #2)}'])).toBe('(a, b) + (a, b)')
    const opt = '\\newcommand{\\seq}[2][n]{#2_1, \\dots, #2_{#1}}'
    expect(expandMacros('\\seq{x}', [opt])).toBe('x_1, \\dots, x_{n}')
    expect(expandMacros('\\seq[k]{y}', [opt])).toBe('y_1, \\dots, y_{k}')
  })

  test('\\def, \\let, \\DeclareMathOperator and \\renewcommand', () => {
    expect(expandMacros('\\sq{a+b}', ['\\def\\sq#1{(#1)^2}'])).toBe('(a+b)^2')
    expect(expandMacros('\\eps', ['\\let\\eps=\\varepsilon'])).toBe('\\varepsilon')
    expect(expandMacros('\\tr A', ['\\DeclareMathOperator{\\tr}{tr}'])).toBe('\\operatorname{tr} A')
    expect(expandMacros('\\argmax_x f', ['\\DeclareMathOperator*{\\argmax}{arg\\,max}'])).toBe('\\operatorname*{arg\\,max}_x f')
    expect(expandMacros('\\v', ['\\newcommand{\\v}{1}', '\\renewcommand{\\v}{2}'])).toBe('2')
    expect(expandMacros('\\v', ['\\newcommand{\\v}{1}', '\\providecommand{\\v}{2}'])).toBe('1')
  })

  test('a longer name is not a use, nested macros expand, a control word keeps its space', () => {
    expect(expandMacros('\\Rightarrow \\R', ['\\newcommand{\\R}{\\mathbb{R}}'])).toBe('\\Rightarrow \\mathbb{R}')
    expect(expandMacros('\\E[X]', ['\\newcommand{\\P}{\\mathbb{P}}', '\\newcommand{\\E}{\\operatorname{\\P}}'])).toBe('\\operatorname{\\mathbb{P}}[X]')
    expect(expandMacros('\\a b', ['\\newcommand{\\a}{\\alpha}'])).toBe('\\alpha b')
  })

  test('an argument pasted beside a control word stays apart from it', () => {
    expect(expandMacros('\\norm{x}', ['\\newcommand{\\norm}[1]{\\lVert#1\\rVert}'])).toBe('\\lVert x\\rVert')
    expect(expandMacros('\\f{b}', ['\\def\\f#1{\\alpha#1}'])).toBe('\\alpha b')
    expect(expandMacros('\\g{\\alpha}', ['\\def\\g#1{#1x}'])).toBe('\\alpha x')
    expect(expandMacros('\\norm{\\alpha}', ['\\newcommand{\\norm}[1]{\\lVert#1\\rVert}'])).toBe('\\lVert\\alpha\\rVert')
    expect(expandMacros('\\abs{x}', ['\\newcommand{\\abs}[1]{\\left|#1\\right|}'])).toBe('\\left|x\\right|')
  })

  test('\\providecommand defines only what is not defined yet', () => {
    expect(expandMacros('\\P(A)', ['\\providecommand{\\P}{\\mathbb{P}}'])).toBe('\\mathbb{P}(A)')
    expect(expandMacros('\\P', ['\\newcommand{\\P}{1}', '\\providecommand{\\P}{2}'])).toBe('1')
    const blocks = parseMessage('Let $\\providecommand{\\P}{\\mathbb{P}}$ and $\\P(A)$.')
    expect(mathRefs(blocks)).toEqual([{ latex: '\\mathbb{P}(A)', display: false }])
  })

  test('\\global before a definition is dropped, and an empty macro draws nothing', () => {
    expect(expandMacros('\\R', ['\\global\\def\\R{\\mathbb{R}}'])).toBe('\\mathbb{R}')
    expect(expandMacros('\\global\\let\\eps\\varepsilon \\eps', [])).toBe('\\varepsilon')
    expect(expandMacros('\\x', ['\\newcommand{\\x}{}'])).toBe('')
    expect(expandMacros('\\newcommand{\\x}{}\\x', [])).toBe('')
  })

  test('a formula of definitions only leaves one space in the sentence', () => {
    const blocks = parseMessage('Define $\\newcommand{\\R}{\\mathbb{R}}$ then $x \\in \\R$ holds.')
    const first = blocks[0]
    expect(first?.kind === 'paragraph' ? first.inlines[0] : undefined).toEqual({ kind: 'text', text: 'Define then ', style: {} })
  })

  test('recursion, blow-up and a missing argument leave the formula as written', () => {
    expect(expandMacros('\\loop x', ['\\def\\loop{\\loop}'])).toBe('\\loop x')
    const bomb = ['\\def\\a{\\b\\b}', '\\def\\b{\\c\\c}', '\\def\\c{\\d\\d}', '\\def\\d{\\e\\e}', '\\def\\e{xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx}']
    expect(expandMacros('\\a\\a\\a', bomb)).toBe('\\a\\a\\a')
    expect(expandMacros('\\f', ['\\newcommand{\\f}[1]{#1^2}'])).toBe('\\f')
  })

  test('definitions apply to every formula of the reply; a formula of definitions only draws nothing', () => {
    const text = 'Let $\\newcommand{\\R}{\\mathbb{R}}$ the reals.\n\n$$\n\\DeclareMathOperator{\\tr}{tr}\n\\newcommand{\\norm}[1]{\\|#1\\|}\n$$\n\nThen $x \\in \\R$ and\n\n$$\\norm{A} \\ge \\tr A$$'
    const blocks = parseMessage(text)
    expect(mathRefs(blocks)).toEqual([
      { latex: 'x \\in \\mathbb{R}', display: false },
      { latex: '\\|A\\| \\ge \\operatorname{tr} A', display: true },
    ])
    expect(blocks[0]).toEqual({ kind: 'markdown', text: 'Let the reals.' })
    expect(blocks.some(block => block.kind === 'display' && block.latex.includes('newcommand'))).toBe(false)
  })

  test('a definition used before it is defined still expands, and in tables and headings too', () => {
    const text = '## On $\\R$\n\n| set | |\n|---|---|\n| $\\R^2$ | plane |\n\nWhere $\\def\\R{\\mathbb{R}}$.'
    expect(parseMessage(text)).toEqual([{ kind: 'markdown', text: '## On ℝ\n\n| set | |\n|---|---|\n| ℝ² | plane |\n\nWhere .' }])
  })

  test('code is not a definition', () => {
    const blocks = parseMessage('`$\\newcommand{\\R}{X}$` and $\\R$')
    expect(mathRefs(blocks)).toEqual([{ latex: '\\R', display: false }])
  })

  test('a reply that defines nothing reads as before, and a cap of 8000 holds', () => {
    expect(mathRefs(parseMessage('$\\R$'))).toEqual([{ latex: '\\R', display: false }])
    const long = `\\newcommand{\\w}{${'y'.repeat(900)}}`
    expect(expandMacros('\\w'.repeat(8), [long])).toHaveLength(8 * 900)
    expect(expandMacros('\\w'.repeat(10), [long])).toBe('\\w'.repeat(10))
  })
})

describe('mermaid blocks', () => {
  test('a closed mermaid fence at the top level is a mermaid block, even without math', () => {
    const text = 'Flow:\n\n```mermaid\ngraph TD\n  A --> B\n```\n\nDone.'
    expect(hasDiagram(text)).toBe(true)
    expect(parseMessage(text)).toEqual([
      { kind: 'markdown', text: 'Flow:' },
      { kind: 'mermaid', source: 'graph TD\n  A --> B', indent: 0, gapBefore: true },
      { kind: 'markdown', text: 'Done.', gapBefore: true },
    ])
  })

  test('the info string: any case, extra words, ~~~ fences, indented fences', () => {
    expect(parseMessage('~~~ Mermaid title\nsequenceDiagram\n~~~')).toEqual([{ kind: 'mermaid', source: 'sequenceDiagram', indent: 0 }])
    expect(parseMessage('  ```mermaid\n  graph LR\n    A-->B\n  ```')).toEqual([{ kind: 'mermaid', source: 'graph LR\n  A-->B', indent: 0 }])
    expect(hasDiagram('```mermaidx\ngraph\n```')).toBe(false)
  })

  test('in a list item it hangs under the item text', () => {
    const blocks = parseMessage('1. Steps:\n\n   ```mermaid\n   graph TD\n   A-->B\n   ```\n2. End')
    expect(blocks.find(block => block.kind === 'mermaid')).toEqual({ kind: 'mermaid', source: 'graph TD\nA-->B', indent: 3, gapBefore: true })
  })

  test('unclosed, in a quote, or another language: left to the engine', () => {
    for (const text of ['```mermaid\ngraph TD\nA-->B', '> ```mermaid\n> graph TD\n> ```', '```js\nmermaid()\n```']) {
      expect(hasDiagram(text)).toBe(false)
      expect(parseMessage(text)).toEqual([{ kind: 'markdown', text }])
    }
  })

  test('a long run of backticks or tildes is read in linear time', () => {
    for (const text of ['`'.repeat(60_000), `${'`'.repeat(60_000)} mermaid`, `x ${'~'.repeat(60_000)}mermaid`]) {
      const started = performance.now()
      parseMessage(text)
      hasDiagram(text)
      expect(performance.now() - started).toBeLessThan(40)
    }
  })

  test('code in other fences next to a diagram stays untouched', () => {
    const blocks = parseMessage('```mermaid\ngraph TD\n```\n\n```tex\n$x$\n```')
    expect(blocks).toEqual([
      { kind: 'mermaid', source: 'graph TD', indent: 0 },
      { kind: 'markdown', text: '```tex\n$x$\n```', gapBefore: true },
    ])
  })
})

describe('fuzz', () => {
  test('random markdown never throws, and hasMath agrees with parseMessage', () => {
    const alphabet = ['```mermaid\n', '```\n', 'graph TD\n', '\\newcommand{\\R}{\\mathbb{R}}', '\\def\\f#1{#1^2}', '\\R', '\\f', '#1', '$', '$$', '\\', '\\(', '\\)', '\\[', '\\]', '`', '```', '*', '_', '~~', '[', ']', '(', ')', '<', '>', '\n', '\n\n', ' ', '  ', '- ', '1. ', '# ', '|', '---', '中', '，', 'x', 'ab', '{', '}', '^', '\\frac', '\\begin{pmatrix}', '\\end{pmatrix}', '&', '\t', '    ', '😀', '\\left', '\\not']
    let seed = 7
    const random = (n: number) => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed % n
    }
    for (let k = 0; k < 600; k++) {
      let text = ''
      const length = random(50)
      for (let i = 0; i < length; i++) {
        text += alphabet[random(alphabet.length)]
      }
      const blocks = parseMessage(text)
      if (!hasMath(text) && !hasDiagram(text)) {
        expect(blocks).toEqual([{ kind: 'markdown', text }])
      }
      for (const block of blocks) {
        if (block.kind === 'paragraph') {
          for (const width of [1, 7, 40]) {
            const lines = layoutParagraph(block, () => (random(2) ? 'pending' : picture(1 + random(9), 1 + random(3), random(2))), width)
            expect(lines.length).toBeGreaterThan(0)
          }
        }
      }
    }
  })
})
