// Layout engine for inline math: reads a reply's markdown, finds the LaTeX in
// it, and lays paragraphs that hold formulas out as lines of text runs and
// pictures, each line baseline-aligned. Pure functions: no `$`, no I/O.
//
// Conventions the drawer relies on:
// - An Inline text holding '\n' is a hard line break.
// - layoutParagraph draws the indent and the list marker itself: the first
//   run of every line is the indent (plus the marker on the first line).
// - Every Run is drawn in order, left to right, each with its marginTop.
// - A Block's gapBefore: the source had a blank line between it and the
//   block before, so the drawer leaves a blank row there (as the engine's
//   markdown does); none where the source had none ("Steps:\n1. ...").
// - A 'markdown' Block with an indent (code, a table or a quote inside a
//   list item) is drawn that many cells in, under the item's text.
// - Formulas reach the blocks with the reply's own macros expanded (see
//   "Reply-scoped macros"); one that held only definitions is left out.

export type Picture = { png: string; columns: number; rows: number; textRow: number }
// `isBackend`: the failure is not the formula's own (the renderer could not
// run, the cache overflowed), so the stand-in is the readable Unicode form;
// any other error is TeX refusing the formula, shown as its source.
export type PictureState = Picture | { error: string; isBackend?: boolean } | 'pending'
export type Style = { bold?: boolean; italic?: boolean; code?: boolean; strike?: boolean; link?: string }
export type Inline = { kind: 'text'; text: string; style: Style } | { kind: 'math'; latex: string }
export type Block =
  | { kind: 'markdown'; text: string; indent?: number; gapBefore?: boolean }
  | { kind: 'paragraph'; inlines: Inline[]; marker?: string; indent: number; gapBefore?: boolean }
  | { kind: 'display'; latex: string; indent: number; gapBefore?: boolean }
  // A closed ```mermaid fence at the top level or in a list item: `source`
  // is its body, without the fences and the fence's own indentation.
  | { kind: 'mermaid'; source: string; indent: number; gapBefore?: boolean }
export type MathRef = { latex: string; display: boolean }
export type Run =
  | { kind: 'text'; text: string; style: Style; marginTop: number }
  | { kind: 'image'; picture: Picture; latex: string; marginTop: number }
  | { kind: 'fallback'; text: string; latex: string; marginTop: number; isError: boolean }
export type Line = { runs: Run[]; height: number }

// ---------------------------------------------------------------------------
// Character width

// East Asian Wide (W) and Fullwidth (F) ranges that are not emoji; emoji are
// found by the Emoji_Presentation property. Sorted, inclusive.
const WIDE_RANGES: [number, number][] = [
  [0x1100, 0x115f], [0x2329, 0x232a], [0x2e80, 0x2e99], [0x2e9b, 0x2ef3],
  [0x2f00, 0x2fd5], [0x2ff0, 0x2fff], [0x3000, 0x303e], [0x3041, 0x3096],
  [0x3099, 0x30ff], [0x3105, 0x312f], [0x3131, 0x318e], [0x3190, 0x31e5],
  [0x31ef, 0x321e], [0x3220, 0x3247], [0x3250, 0xa48c], [0xa490, 0xa4c6],
  [0xa960, 0xa97c], [0xac00, 0xd7a3], [0xf900, 0xfaff], [0xfe10, 0xfe19],
  [0xfe30, 0xfe52], [0xfe54, 0xfe66], [0xfe68, 0xfe6b], [0xff01, 0xff60],
  [0xffe0, 0xffe6], [0x16fe0, 0x16fe4], [0x16ff0, 0x16ff1], [0x17000, 0x187f7],
  [0x18800, 0x18cd5], [0x18d00, 0x18d08], [0x1aff0, 0x1b2fb], [0x1f200, 0x1f202],
  [0x1f210, 0x1f23b], [0x1f240, 0x1f248], [0x1f250, 0x1f251], [0x1f260, 0x1f265],
  [0x20000, 0x2fffd], [0x30000, 0x3fffd],
]

const ZERO_WIDTH = /^[\p{Mn}\p{Me}\p{Cf}\p{Cc}\u1160-\u11ff\ufe00-\ufe0f\u{e0100}-\u{e01ef}]$/u
const EMOJI_WIDE = /\p{Emoji_Presentation}|\p{Extended_Pictographic}\ufe0f|\p{Regional_Indicator}{2}|\u20e3/u
const ASCII_PRINTABLE = /^[\x20-\x7e]*$/

function isWideCodePoint(code: number): boolean {
  if (code < 0x1100) {
    return false
  }
  let low = 0
  let high = WIDE_RANGES.length - 1
  while (low <= high) {
    const mid = (low + high) >> 1
    const [start, end] = WIDE_RANGES[mid] as [number, number]
    if (code < start) {
      high = mid - 1
    } else if (code > end) {
      low = mid + 1
    } else {
      return true
    }
  }

  return false
}

const segmenter = typeof Intl !== 'undefined' && 'Segmenter' in Intl ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : undefined

// Split text into grapheme clusters (user-perceived characters).
function graphemes(text: string): string[] {
  if (ASCII_PRINTABLE.test(text)) {
    return text.split('')
  }
  if (segmenter) {
    return Array.from(segmenter.segment(text), part => part.segment)
  }
  const out: string[] = []
  for (const char of text) {
    if (out.length > 0 && (ZERO_WIDTH.test(char) || char === '\u200d')) {
      out[out.length - 1] += char
    } else {
      out.push(char)
    }
  }

  return out
}

function graphemeWidth(cluster: string): number {
  if (EMOJI_WIDE.test(cluster)) {
    return 2
  }
  for (const char of cluster) {
    if (ZERO_WIDTH.test(char)) {
      continue
    }

    return isWideCodePoint(char.codePointAt(0) ?? 0) ? 2 : 1
  }

  return 0
}

/** Terminal cells the text takes: wide and fullwidth 2, emoji 2, combining marks 0. */
export function stringWidth(text: string): number {
  if (ASCII_PRINTABLE.test(text)) {
    return text.length
  }
  let width = 0
  for (const cluster of graphemes(text)) {
    width += graphemeWidth(cluster)
  }

  return width
}

function isWide(cluster: string): boolean {
  return graphemeWidth(cluster) === 2
}

// ---------------------------------------------------------------------------
// LaTeX to Unicode

const GREEK: Record<string, string> = {
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ϵ', varepsilon: 'ε', zeta: 'ζ', eta: 'η',
  theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ', varkappa: 'ϰ', lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ',
  omicron: 'ο', pi: 'π', varpi: 'ϖ', rho: 'ρ', varrho: 'ϱ', sigma: 'σ', varsigma: 'ς', tau: 'τ',
  upsilon: 'υ', phi: 'ϕ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω', digamma: 'ϝ',
  Alpha: 'Α', Beta: 'Β', Gamma: 'Γ', Delta: 'Δ', Epsilon: 'Ε', Zeta: 'Ζ', Eta: 'Η', Theta: 'Θ',
  Iota: 'Ι', Kappa: 'Κ', Lambda: 'Λ', Mu: 'Μ', Nu: 'Ν', Xi: 'Ξ', Omicron: 'Ο', Pi: 'Π', Rho: 'Ρ',
  Sigma: 'Σ', Tau: 'Τ', Upsilon: 'Υ', Phi: 'Φ', Chi: 'Χ', Psi: 'Ψ', Omega: 'Ω',
  varGamma: 'Γ', varDelta: 'Δ', varTheta: 'Θ', varLambda: 'Λ', varXi: 'Ξ', varPi: 'Π', varSigma: 'Σ',
  varUpsilon: 'Υ', varPhi: 'Φ', varPsi: 'Ψ', varOmega: 'Ω',
}

const SYMBOLS: Record<string, string> = {
  ...GREEK,
  // binary operators
  cdot: '⋅', times: '×', div: '÷', pm: '±', mp: '∓', ast: '∗', star: '⋆', circ: '∘', bullet: '∙',
  oplus: '⊕', ominus: '⊖', otimes: '⊗', oslash: '⊘', odot: '⊙', wedge: '∧', land: '∧', vee: '∨',
  lor: '∨', cap: '∩', cup: '∪', sqcap: '⊓', sqcup: '⊔', uplus: '⊎', setminus: '∖', smallsetminus: '∖',
  wr: '≀', amalg: '⨿', dagger: '†', ddagger: '‡', diamond: '⋄', triangle: '△', bigtriangleup: '△',
  bigtriangledown: '▽', triangleleft: '◁', triangleright: '▷', lhd: '⊲', rhd: '⊳', cdotp: '·',
  // relations
  le: '≤', leq: '≤', ge: '≥', geq: '≥', leqslant: '⩽', geqslant: '⩾', ne: '≠', neq: '≠', ll: '≪', gg: '≫',
  lll: '⋘', ggg: '⋙', approx: '≈', approxeq: '≊', sim: '∼', simeq: '≃', cong: '≅', equiv: '≡', propto: '∝',
  prec: '≺', succ: '≻', preceq: '⪯', succeq: '⪰', asymp: '≍', doteq: '≐', triangleq: '≜', coloneqq: '≔',
  coloneq: '≔', eqqcolon: '≕', lesssim: '≲', gtrsim: '≳', lessgtr: '≶', nless: '≮', ngtr: '≯',
  nleq: '≰', ngeq: '≱', nsim: '≁', ncong: '≇', models: '⊨', vdash: '⊢', dashv: '⊣', vDash: '⊨',
  Vdash: '⊩', perp: '⊥', parallel: '∥', nparallel: '∦', mid: '∣', nmid: '∤', bowtie: '⋈', Join: '⨝',
  smile: '⌣', frown: '⌢',
  // sets and logic
  in: '∈', notin: '∉', ni: '∋', owns: '∋', subset: '⊂', supset: '⊃', subseteq: '⊆', supseteq: '⊇',
  subsetneq: '⊊', supsetneq: '⊋', nsubseteq: '⊈', nsupseteq: '⊉', sqsubset: '⊏', sqsupset: '⊐',
  sqsubseteq: '⊑', sqsupseteq: '⊒', emptyset: '∅', varnothing: '∅', forall: '∀', exists: '∃',
  nexists: '∄', neg: '¬', lnot: '¬', top: '⊤', bot: '⊥', therefore: '∴', because: '∵',
  // arrows
  to: '→', gets: '←', rightarrow: '→', leftarrow: '←', leftrightarrow: '↔', Rightarrow: '⇒',
  Leftarrow: '⇐', Leftrightarrow: '⇔', implies: '⟹', impliedby: '⟸', iff: '⟺', mapsto: '↦',
  longmapsto: '⟼', longrightarrow: '⟶', longleftarrow: '⟵', longleftrightarrow: '⟷',
  Longrightarrow: '⟹', Longleftarrow: '⟸', Longleftrightarrow: '⟺', uparrow: '↑', downarrow: '↓',
  updownarrow: '↕', Uparrow: '⇑', Downarrow: '⇓', Updownarrow: '⇕', nearrow: '↗', searrow: '↘',
  swarrow: '↙', nwarrow: '↖', hookrightarrow: '↪', hookleftarrow: '↩', rightharpoonup: '⇀',
  rightharpoondown: '⇁', leftharpoonup: '↼', leftharpoondown: '↽', rightleftharpoons: '⇌',
  leftrightharpoons: '⇋', rightrightarrows: '⇉', leftleftarrows: '⇇', twoheadrightarrow: '↠',
  rightsquigarrow: '⇝', leadsto: '⇝', circlearrowleft: '↺', circlearrowright: '↻', nrightarrow: '↛',
  nleftarrow: '↚', nRightarrow: '⇏', nLeftrightarrow: '⇎', uparrows: '⇈', downdownarrows: '⇊',
  // big operators
  sum: '∑', prod: '∏', coprod: '∐', int: '∫', iint: '∬', iiint: '∭', oint: '∮', oiint: '∯',
  bigcup: '⋃', bigcap: '⋂', bigvee: '⋁', bigwedge: '⋀', bigoplus: '⨁', bigotimes: '⨂', bigodot: '⨀',
  biguplus: '⨄', bigsqcup: '⨆',
  // misc
  infty: '∞', partial: '∂', nabla: '∇', hbar: 'ℏ', hslash: 'ℏ', ell: 'ℓ', Re: 'ℜ', Im: 'ℑ', wp: '℘',
  aleph: 'ℵ', beth: 'ℶ', gimel: 'ℷ', imath: 'ı', jmath: 'ȷ', prime: '′', backprime: '‵', angle: '∠',
  measuredangle: '∡', sphericalangle: '∢', degree: '°', surd: '√', checkmark: '✓', square: '□',
  Box: '□', blacksquare: '■', lozenge: '◊', blacklozenge: '⧫', clubsuit: '♣', diamondsuit: '♢',
  heartsuit: '♡', spadesuit: '♠', flat: '♭', natural: '♮', sharp: '♯', complement: '∁', mho: '℧',
  eth: 'ð', S: '§', P: '¶', copyright: '©', pounds: '£', yen: '¥', euro: '€', dag: '†', ddag: '‡',
  ldots: '…', dots: '…', dotsc: '…', dotsb: '⋯', dotsm: '⋯', dotso: '…', cdots: '⋯', vdots: '⋮',
  ddots: '⋱', iddots: '⋰', colon: ':', vert: '|', Vert: '‖', lvert: '|', rvert: '|', lVert: '‖',
  rVert: '‖', langle: '⟨', rangle: '⟩', lceil: '⌈', rceil: '⌉', lfloor: '⌊', rfloor: '⌋',
  lbrace: '{', rbrace: '}', lbrack: '[', rbrack: ']', backslash: '\\', lgroup: '(', rgroup: ')',
  ulcorner: '⌜', urcorner: '⌝', llcorner: '⌞', lrcorner: '⌟', llbracket: '⟦', rrbracket: '⟧',
  textbackslash: '\\', textasciitilde: '~', textasciicircum: '^', textunderscore: '_',
  // escaped characters and spacing
  '{': '{', '}': '}', '%': '%', $: '$', '&': '&', '#': '#', _: '_', '|': '‖', '\\': '; ',
  ',': ' ', ':': ' ', ';': ' ', '>': ' ', ' ': ' ', '!': '', '/': '', '-': '',
  quad: ' ', qquad: '  ', enspace: ' ', thinspace: ' ', medspace: ' ', thickspace: ' ', space: ' ',
  enskip: ' ', nobreakspace: ' ', negthinspace: '', negmedspace: '', negthickspace: '', nobreak: '',
  allowbreak: '', newline: '; ', cr: '; ',
  bmod: ' mod ', mod: ' mod ',
}

const FUNCTIONS = new Set([
  'sin', 'cos', 'tan', 'cot', 'sec', 'csc', 'arcsin', 'arccos', 'arctan', 'arccot', 'sinh', 'cosh',
  'tanh', 'coth', 'sech', 'csch', 'log', 'ln', 'lg', 'exp', 'lim', 'liminf', 'limsup', 'max', 'min',
  'sup', 'inf', 'det', 'dim', 'ker', 'deg', 'gcd', 'lcm', 'arg', 'Pr', 'hom', 'tr', 'Tr', 'rank',
  'span', 'diag', 'sgn', 'argmax', 'argmin', 'var', 'Var', 'Cov', 'cov', 'erf', 'id', 'im',
])

// Commands that draw nothing and take no argument.
const IGNORED = new Set([
  'left', 'right', 'middle', 'big', 'Big', 'bigg', 'Bigg', 'bigl', 'bigr', 'Bigl', 'Bigr', 'biggl',
  'biggr', 'Biggl', 'Biggr', 'bigm', 'Bigm', 'displaystyle', 'textstyle', 'scriptstyle',
  'scriptscriptstyle', 'limits', 'nolimits', 'nonumber', 'notag', 'mathstrut', 'strut', 'relax',
  'normalsize', 'small', 'large', 'Large', 'LARGE', 'huge', 'Huge', 'tiny', 'footnotesize', 'bf', 'it',
  'rm', 'sf', 'tt', 'cal', 'centering', 'protect', 'hfill', 'qedhere', 'boldmath', 'unboldmath',
])

// Commands whose single argument is drawn as is (font and box changes).
const PLAIN_ARGUMENT = new Set([
  'mathrm', 'mathit', 'mathbf', 'mathsf', 'mathtt', 'mathnormal', 'boldsymbol', 'bm', 'pmb', 'mathbfit',
  'operatorname', 'boxed', 'fbox', 'underbrace', 'overbrace', 'underline', 'cancel', 'bcancel',
  'xcancel', 'sout', 'emph', 'textbf', 'textit', 'textsf', 'texttt', 'textup', 'textmd', 'textnormal',
  'mathop', 'mathbin', 'mathrel', 'mathord', 'mathopen', 'mathclose', 'mathpunct', 'mathinner',
  'smash', 'hbox', 'mbox', 'text', 'textrm', 'displaylines', 'ensuremath',
])

// Commands whose argument is plain text, not math.
const TEXT_ARGUMENT = new Set(['text', 'textrm', 'textbf', 'textit', 'textsf', 'texttt', 'textup', 'textmd', 'textnormal', 'mbox', 'hbox', 'emph', 'operatorname'])

// Commands that take an argument and draw nothing.
const DROP_ARGUMENT = new Set(['label', 'phantom', 'vphantom', 'hphantom', 'color', 'hspace', 'vspace', 'kern', 'mkern', 'mspace', 'hskip', 'vskip'])

const ACCENTS: Record<string, string> = {
  hat: '\u0302', widehat: '\u0302', check: '\u030c', widecheck: '\u030c', tilde: '\u0303', widetilde: '\u0303',
  acute: '\u0301', grave: '\u0300', dot: '\u0307', ddot: '\u0308', dddot: '\u20db', breve: '\u0306',
  bar: '\u0304', vec: '\u20d7', overrightarrow: '\u20d7', overleftarrow: '\u20d6', mathring: '\u030a',
  overline: '\u0305', utilde: '\u0330', underbar: '\u0332',
}

// Accents drawn over every character, as a line is.
const SPREAD_ACCENTS = new Set(['overline', 'underbar', 'bar'])

const NEGATED: Record<string, string> = {
  '=': '≠', '<': '≮', '>': '≯', '∈': '∉', '∋': '∌', '⊂': '⊄', '⊃': '⊅', '⊆': '⊈', '⊇': '⊉', '≤': '≰',
  '≥': '≱', '≡': '≢', '∼': '≁', '≃': '≄', '≅': '≇', '≈': '≉', '∣': '∤', '∥': '∦', '∃': '∄', '→': '↛',
  '←': '↚', '↔': '↮', '⇒': '⇏', '⇐': '⇍', '⇔': '⇎', '⊢': '⊬', '⊨': '⊭', '≺': '⊀', '≻': '⊁',
}

const SUPERSCRIPT: Record<string, string> = {
  '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
  '+': '⁺', '-': '⁻', '−': '⁻', '=': '⁼', '(': '⁽', ')': '⁾', a: 'ᵃ', b: 'ᵇ', c: 'ᶜ', d: 'ᵈ', e: 'ᵉ',
  f: 'ᶠ', g: 'ᵍ', h: 'ʰ', i: 'ⁱ', j: 'ʲ', k: 'ᵏ', l: 'ˡ', m: 'ᵐ', n: 'ⁿ', o: 'ᵒ', p: 'ᵖ', r: 'ʳ',
  s: 'ˢ', t: 'ᵗ', u: 'ᵘ', v: 'ᵛ', w: 'ʷ', x: 'ˣ', y: 'ʸ', z: 'ᶻ', A: 'ᴬ', B: 'ᴮ', D: 'ᴰ', E: 'ᴱ',
  G: 'ᴳ', H: 'ᴴ', I: 'ᴵ', J: 'ᴶ', K: 'ᴷ', L: 'ᴸ', M: 'ᴹ', N: 'ᴺ', O: 'ᴼ', P: 'ᴾ', R: 'ᴿ', T: 'ᵀ',
  U: 'ᵁ', V: 'ⱽ', W: 'ᵂ', 'α': 'ᵅ', 'β': 'ᵝ', 'γ': 'ᵞ', 'δ': 'ᵟ', 'ε': 'ᵋ', 'ϵ': 'ᵋ', 'θ': 'ᶿ',
  'ι': 'ᶥ', 'ϕ': 'ᵠ', 'φ': 'ᵠ', 'χ': 'ᵡ', '′': '′', '″': '″', '∘': '°', '°': '°',
  '†': '†', '⊤': 'ᵀ',
}

const SUBSCRIPT: Record<string, string> = {
  '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄', '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
  '+': '₊', '-': '₋', '−': '₋', '=': '₌', '(': '₍', ')': '₎', a: 'ₐ', e: 'ₑ', h: 'ₕ', i: 'ᵢ', j: 'ⱼ',
  k: 'ₖ', l: 'ₗ', m: 'ₘ', n: 'ₙ', o: 'ₒ', p: 'ₚ', r: 'ᵣ', s: 'ₛ', t: 'ₜ', u: 'ᵤ', v: 'ᵥ', x: 'ₓ',
  'β': 'ᵦ', 'γ': 'ᵧ', 'ρ': 'ᵨ', 'ϕ': 'ᵩ', 'φ': 'ᵩ', 'χ': 'ᵪ',
}

const VULGAR: Record<string, string> = {
  '1/2': '½', '1/3': '⅓', '2/3': '⅔', '1/4': '¼', '3/4': '¾', '1/5': '⅕', '2/5': '⅖', '3/5': '⅗',
  '4/5': '⅘', '1/6': '⅙', '5/6': '⅚', '1/7': '⅐', '1/8': '⅛', '3/8': '⅜', '5/8': '⅝', '7/8': '⅞',
  '1/9': '⅑', '1/10': '⅒',
}

const DOUBLE_STRUCK: Record<string, string> = { C: 'ℂ', H: 'ℍ', N: 'ℕ', P: 'ℙ', Q: 'ℚ', R: 'ℝ', Z: 'ℤ' }
const SCRIPT: Record<string, string> = {
  B: 'ℬ', E: 'ℰ', F: 'ℱ', H: 'ℋ', I: 'ℐ', L: 'ℒ', M: 'ℳ', R: 'ℛ', e: 'ℯ', g: 'ℊ', o: 'ℴ',
}
const FRAKTUR: Record<string, string> = { C: 'ℭ', H: 'ℌ', I: 'ℑ', R: 'ℜ', Z: 'ℨ' }

// Map letters (and digits, where the alphabet has them) to a math alphabet.
function mathAlphabet(text: string, special: Record<string, string>, upper: number, lower: number | null, digit: number | null): string {
  let out = ''
  for (const char of text) {
    const code = char.charCodeAt(0)
    if (special[char]) {
      out += special[char]
    } else if (code >= 65 && code <= 90) {
      out += String.fromCodePoint(upper + code - 65)
    } else if (lower !== null && code >= 97 && code <= 122) {
      out += String.fromCodePoint(lower + code - 97)
    } else if (digit !== null && code >= 48 && code <= 57) {
      out += String.fromCodePoint(digit + code - 48)
    } else {
      out += char
    }
  }

  return out
}

const FONTS: Record<string, (text: string) => string> = {
  mathbb: text => mathAlphabet(text, DOUBLE_STRUCK, 0x1d538, 0x1d552, 0x1d7d8),
  Bbb: text => mathAlphabet(text, DOUBLE_STRUCK, 0x1d538, 0x1d552, 0x1d7d8),
  mathds: text => mathAlphabet(text, DOUBLE_STRUCK, 0x1d538, 0x1d552, 0x1d7d8),
  mathcal: text => mathAlphabet(text, SCRIPT, 0x1d49c, 0x1d4b6, null),
  mathscr: text => mathAlphabet(text, SCRIPT, 0x1d49c, 0x1d4b6, null),
  mathfrak: text => mathAlphabet(text, FRAKTUR, 0x1d504, 0x1d51e, null),
}

const MATRIX_BRACKETS: Record<string, [string, string]> = {
  pmatrix: ['(', ')'], bmatrix: ['[', ']'], Bmatrix: ['{', '}'], vmatrix: ['|', '|'], Vmatrix: ['‖', '‖'],
  smallmatrix: ['', ''], matrix: ['', ''], array: ['', ''],
}

// Is this text one symbol, so a fraction or root needs no parentheses?
function isSimple(text: string): boolean {
  return /^[\p{L}\p{N}.′]*$/u.test(text) && (graphemes(text).length <= 1 || /^[\p{N}.]+$/u.test(text) || /^\p{L}+$/u.test(text))
}

function wrapIfNeeded(text: string): string {
  return isSimple(text) ? text : `(${text})`
}

function script(text: string, table: Record<string, string>, mark: string): string {
  const compact = text.replace(/\s+/g, '')
  if (compact === '') {
    return ''
  }
  let mapped = ''
  for (const char of compact) {
    const found = table[char]
    if (found === undefined) {
      mapped = ''
      break
    }
    mapped += found
  }
  if (mapped) {
    return mapped
  }

  // TeX ignores spaces in math; keep them only between words ("h→0", "max value").
  const spaced = text.trim().replace(/\s*([^\p{L}\p{N}\s])\s*/gu, '$1')

  return graphemes(compact).length === 1 ? `${mark}${compact}` : `${mark}(${spaced})`
}

const SI_UNITS: Record<string, string> = {
  meter: 'm', metre: 'm', second: 's', kilogram: 'kg', gram: 'g', kelvin: 'K', ampere: 'A', mole: 'mol',
  candela: 'cd', hertz: 'Hz', newton: 'N', pascal: 'Pa', joule: 'J', watt: 'W', volt: 'V', ohm: 'Ω',
  coulomb: 'C', farad: 'F', tesla: 'T', henry: 'H', siemens: 'S', weber: 'Wb', becquerel: 'Bq',
  celsius: '°C', degreeCelsius: '°C', degree: '°', liter: 'L', litre: 'L', minute: 'min', hour: 'h',
  day: 'd', electronvolt: 'eV', angstrom: 'Å', percent: '%', bit: 'bit', byte: 'B', radian: 'rad',
  steradian: 'sr', lux: 'lx', lumen: 'lm', bar: 'bar', dalton: 'Da', arcminute: '′', arcsecond: '″',
  yocto: 'y', zepto: 'z', atto: 'a', femto: 'f', pico: 'p', nano: 'n', micro: 'µ', milli: 'm', centi: 'c',
  deci: 'd', deca: 'da', hecto: 'h', kilo: 'k', mega: 'M', giga: 'G', tera: 'T', peta: 'P', exa: 'E',
  per: '/', squared: '²', cubed: '³', square: '', cubic: '', of: '', cancel: '', highlight: '',
}

/** An siunitx unit (`\kilo\meter\per\second`, or `km/s`) as text. */
function siUnit(raw: string): string {
  return raw
    .replace(/\\([a-zA-Z]+)\s*/g, (_, name: string) => SI_UNITS[name] ?? name)
    .replace(/\^\{?(-?\d+)\}?/g, (_, power: string) => script(power, SUPERSCRIPT, '^'))
    .replace(/[{}~]/g, '')
    .replace(/\s*\.\s*/g, '·')
    .trim()
}

/** An mhchem formula or equation (`\ce{SO4^2- + H2O -> ...}`) as text. */
function chemistry(raw: string): string {
  return raw
    .replace(/<=>|<->/g, ' ⇌ ')
    .replace(/->/g, ' → ')
    .replace(/<-/g, ' ← ')
    .replace(/\^\{?([0-9]*[+-])\}?/g, (_, charge: string) => script(charge, SUPERSCRIPT, '^'))
    .replace(/([\p{L})\]])(\d+)/gu, (_, before: string, count: string) => before + script(count, SUBSCRIPT, '_'))
    .replace(/[{}]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function accent(text: string, mark: string, spread: boolean): string {
  const parts = graphemes(text)
  if (parts.length === 0) {
    return mark
  }
  if (spread) {
    return parts.map(part => (part.trim() ? part + mark : part)).join('')
  }
  parts[parts.length - 1] += mark

  return parts.join('')
}

const BIG_OPERATORS = new Set(['∑', '∏', '∐', '∫', '∬', '∭', '∮', '∯', '⋃', '⋂', '⋁', '⋀', '⨁', '⨂', '⨀', '⨄', '⨆'])

// Nesting deeper than this is not a real formula; the stand-in is then the source.
const MAX_DEPTH = 64

class TexReader {
  private at = 0
  private depth = 0
  // Set after a named function, so the next letter gets a space before it.
  private spaceNext = false

  constructor(private readonly source: string) {}

  /** Read the whole source; a stray `\end{..}` is skipped. */
  readAll(): string {
    let out = ''
    while (this.at < this.source.length) {
      out += this.readUntil(null)
      if (this.source.startsWith('\\end', this.at)) {
        this.at += 4
        this.readName()
      }
    }

    return out
  }

  /** Read until the end, the closer, or an `\end`. */
  readUntil(closer: '}' | ']' | null): string {
    this.depth++
    try {
      return this.readUntilInner(closer)
    } finally {
      this.depth--
    }
  }

  private readUntilInner(closer: '}' | ']' | null): string {
    if (this.depth > MAX_DEPTH) {
      throw new Error('nested too deep')
    }
    let out = ''
    while (this.at < this.source.length) {
      const char = this.source[this.at] as string
      if (char === closer) {
        this.at++
        return out
      }
      if (closer === null && char === '}') {
        this.at++
        continue
      }
      if (char === '\\' && this.source.startsWith('\\end', this.at) && !/[a-zA-Z]/.test(this.source[this.at + 4] ?? '')) {
        return out
      }
      if (char === '^' || char === '_') {
        this.at++
        const arg = this.readArgument()
        out += script(arg, char === '^' ? SUPERSCRIPT : SUBSCRIPT, char)
        continue
      }
      if (char === "'") {
        this.at++
        out += '′'
        continue
      }
      const pending = this.spaceNext
      this.spaceNext = false
      const piece = this.readAtom()
      out = this.join(out, piece, pending)
    }

    return out
  }

  // Space a named function from the letter after it, and a big operator
  // from what comes before it.
  private join(out: string, piece: string, pending: boolean): string {
    if (pending && (piece === '' || piece === ' ')) {
      this.spaceNext = this.spaceNext || piece === ''
    }
    const needsSpace =
      (pending && /^[\p{L}\p{N}]/u.test(piece)) || (BIG_OPERATORS.has(piece) && /[\p{L}\p{N}\p{No})′]$/u.test(out))
    if (needsSpace && !out.endsWith(' ')) {
      return `${out} ${piece}`
    }

    return out + piece
  }

  private skipSpaces(): void {
    while (/\s/.test(this.source[this.at] ?? '')) {
      this.at++
    }
  }

  /** One argument: a braced group, a command, or one character. */
  readArgument(): string {
    this.skipSpaces()
    const char = this.source[this.at]
    if (char === undefined) {
      return ''
    }
    if (char === '{') {
      this.at++
      const saved = this.spaceNext
      this.spaceNext = false
      const out = this.readUntil('}')
      this.spaceNext = saved
      return out
    }
    if (char === '\\') {
      return this.readAtom()
    }
    const whole = String.fromCodePoint(this.source.codePointAt(this.at) ?? 32)
    this.at += whole.length

    return whole === '~' ? ' ' : whole
  }

  /** The raw text of a braced argument, for text-mode commands. */
  private readRaw(): string {
    this.skipSpaces()
    if (this.source[this.at] !== '{') {
      return this.readArgument()
    }
    let depth = 0
    const start = this.at
    while (this.at < this.source.length) {
      const char = this.source[this.at]
      if (char === '\\') {
        this.at += 2
        continue
      }
      if (char === '{') {
        depth++
      } else if (char === '}') {
        depth--
        if (depth === 0) {
          this.at++
          break
        }
      }
      this.at++
    }
    const inner = this.source.slice(start + 1, this.at - 1)

    return inner.replace(/\\([{}%$&#_ ])/g, '$1').replace(/\\(?:textbackslash|backslash)\b/g, '\\').replace(/[{}]/g, '').replace(/~/g, ' ')
  }

  /** A braced argument kept as source (its commands unread), for siUnit. */
  private readRawUnit(): string {
    this.skipSpaces()
    if (this.source[this.at] !== '{') {
      return this.readArgument()
    }
    const start = this.at
    let depth = 0
    while (this.at < this.source.length) {
      const char = this.source[this.at]
      if (char === '\\') {
        this.at += 2
        continue
      }
      if (char === '{') {
        depth++
      } else if (char === '}') {
        depth--
        if (depth === 0) {
          this.at++
          break
        }
      }
      this.at++
    }

    return this.source.slice(start + 1, this.at - 1)
  }

  private readOptional(): string | null {
    this.skipSpaces()
    if (this.source[this.at] !== '[') {
      return null
    }
    this.at++

    return this.readUntil(']')
  }

  private readName(): string {
    this.skipSpaces()
    if (this.source[this.at] !== '{') {
      return ''
    }
    const end = this.source.indexOf('}', this.at)
    if (end < 0) {
      this.at = this.source.length
      return ''
    }
    const name = this.source.slice(this.at + 1, end).trim()
    this.at = end + 1

    return name
  }

  private readAtom(): string {
    const char = this.source[this.at] as string
    if (char !== '\\') {
      this.at++
      if (/\s/.test(char)) {
        while (/\s/.test(this.source[this.at] ?? '')) {
          this.at++
        }
        return ' '
      }
      if (char === '{') {
        return this.readUntil('}')
      }
      if (char === '~') {
        return ' '
      }
      if (char === '&') {
        return ' '
      }
      if (char === '%') {
        const end = this.source.indexOf('\n', this.at)
        this.at = end < 0 ? this.source.length : end + 1
        return ''
      }
      const code = char.charCodeAt(0)
      if (code >= 0xd800 && code <= 0xdbff) {
        const low = this.source[this.at] ?? ''
        this.at++
        return char + low
      }

      return char
    }
    const match = /^\\([a-zA-Z]+\*?|[^a-zA-Z])/.exec(this.source.slice(this.at, this.at + 40))
    if (!match) {
      this.at++
      return ''
    }
    this.at += match[0].length
    const name = (match[1] as string).replace(/\*$/, '')

    return this.command(name)
  }

  private command(name: string): string {
    if (name === 'frac' || name === 'dfrac' || name === 'tfrac' || name === 'cfrac') {
      const top = this.readArgument().trim()
      const bottom = this.readArgument().trim()
      const vulgar = VULGAR[`${top}/${bottom}`]
      if (vulgar) {
        return vulgar
      }

      return `${wrapIfNeeded(top)}/${wrapIfNeeded(bottom)}`
    }
    if (name === 'binom' || name === 'dbinom' || name === 'tbinom') {
      const top = this.readArgument().trim()
      const bottom = this.readArgument().trim()

      return `C(${top}, ${bottom})`
    }
    if (name === 'sqrt') {
      const degree = this.readOptional()
      const body = this.readArgument().trim()
      const sign = degree === '3' ? '∛' : degree === '4' ? '∜' : degree ? `${script(degree, SUPERSCRIPT, '^')}√` : '√'

      return sign + wrapIfNeeded(body)
    }
    if (name === 'not') {
      this.skipSpaces()
      const next = this.readAtom()
      return NEGATED[next] ?? `${next}\u0338`
    }
    if (name === 'begin') {
      return this.environment(this.readName())
    }
    if (name === 'end') {
      this.readName()
      return ''
    }
    if (name === 'tag') {
      return ` (${this.readRaw()})`
    }
    if (name === 'pmod') {
      return ` (mod ${this.readArgument().trim()})`
    }
    if (name === 'pod') {
      return ` (${this.readArgument().trim()})`
    }
    if (name === 'ce' || name === 'pu') {
      return chemistry(this.readRaw())
    }
    if (name === 'SI' || name === 'qty') {
      this.readOptional()
      const value = this.readRaw()
      return `${value} ${siUnit(this.readRawUnit())}`
    }
    if (name === 'si' || name === 'unit') {
      this.readOptional()
      return siUnit(this.readRawUnit())
    }
    if (name === 'num') {
      this.readOptional()
      return this.readRaw().replace(/^(-?[\d.]+)[eE]([+-]?\d+)$/, (_, mantissa: string, power: string) => `${mantissa}×10${script(power, SUPERSCRIPT, '^')}`)
    }
    if (name === 'ang') {
      this.readOptional()
      return `${this.readRaw()}°`
    }
    if (name === 'SIrange' || name === 'qtyrange' || name === 'numrange') {
      this.readOptional()
      const low = this.readRaw()
      const high = this.readRaw()
      const unit = name === 'numrange' ? '' : ` ${siUnit(this.readRawUnit())}`
      return `${low}–${high}${unit}`
    }
    if (name === 'textcolor' || name === 'colorbox') {
      this.readRaw()
      return this.readArgument()
    }
    if (name === 'overset' || name === 'underset' || name === 'stackrel') {
      this.readArgument()
      return this.readArgument()
    }
    if (name === 'xrightarrow' || name === 'xleftarrow' || name === 'xRightarrow' || name === 'xLeftarrow') {
      this.readOptional()
      this.readArgument()
      return name === 'xrightarrow' ? '→' : name === 'xleftarrow' ? '←' : name === 'xRightarrow' ? '⇒' : '⇐'
    }
    if (name === 'operatorname' || name === 'mathop') {
      const text = TEXT_ARGUMENT.has(name) ? this.readRaw() : this.readArgument()
      this.spaceNext = true
      return text
    }
    if (FONTS[name]) {
      return FONTS[name](this.readArgument())
    }
    if (ACCENTS[name]) {
      return accent(this.readArgument(), ACCENTS[name], SPREAD_ACCENTS.has(name))
    }
    if (TEXT_ARGUMENT.has(name)) {
      return this.readRaw()
    }
    if (PLAIN_ARGUMENT.has(name)) {
      return this.readArgument()
    }
    if (DROP_ARGUMENT.has(name)) {
      this.readRaw()
      return name === 'hspace' || name === 'mspace' || name === 'kern' || name === 'hskip' ? ' ' : ''
    }
    if (IGNORED.has(name)) {
      if (/^(left|right|middle|[bB]igg?[lrm]?)$/.test(name)) {
        this.skipSpaces()
        if (this.source[this.at] === '.') {
          this.at++
          return ''
        }
      }
      return ''
    }
    if (FUNCTIONS.has(name)) {
      this.spaceNext = true
      return name
    }
    const symbol = SYMBOLS[name]
    if (symbol !== undefined) {
      return symbol
    }

    return name
  }

  private environment(name: string): string {
    const kind = name.replace(/\*$/, '')
    if (kind === 'array' || kind === 'tabular') {
      this.readRaw()
    }
    const body = this.readUntil(null)
    // The reader stops at \end; consume it.
    if (this.source.startsWith('\\end', this.at)) {
      this.at += 4
      this.readName()
    }
    const rows = body
      .split(';')
      .map(row => row.replace(/\s+/g, ' ').trim())
      .filter(row => row !== '')
    if (kind === 'cases' || kind === 'dcases' || kind === 'rcases') {
      return `{ ${rows.join('; ')}`
    }
    const brackets = MATRIX_BRACKETS[kind]
    if (brackets) {
      return `${brackets[0]}${rows.join('; ')}${brackets[1]}`
    }

    return rows.join('; ')
  }
}

/** A readable one-line Unicode stand-in for a LaTeX formula. */
export function toUnicode(latex: string): string {
  let out: string
  try {
    out = new TexReader(latex).readAll()
  } catch {
    out = latex
  }
  out = out.replace(/\s+/g, ' ').replace(/\s*;\s*$/, '').trim()

  return out || latex.trim() || '?'
}

// ---------------------------------------------------------------------------
// Reply-scoped macros
//
// A reply may define its own commands in its math (`\newcommand`,
// `\renewcommand`, `\providecommand`, `\def`, `\let`,
// `\DeclareMathOperator`). They are expanded here, as text, in every formula
// of the reply, so TeX never sees a definition and the expanded formula
// still passes the renderer's safety check. A formula that held only
// definitions draws as nothing. One table per reply; a name defined twice
// takes its last definition. A use whose expansion nests deeper than
// MACRO_DEPTH or grows past MACRO_LENGTH leaves that formula as written.

type Macro = { arity: number; optional?: string; body: string; provide?: boolean }
type MacroTable = Map<string, Macro>

const MACRO_DEPTH = 32
const MACRO_LENGTH = 8000
const MACRO_EXPANSIONS = 4000
// Cheap test: can this text define a macro at all?
const DEFINES = /\\(?:(?:re)?newcommand|providecommand|[gex]?def|let|DeclareMathOperator)(?![A-Za-z])/
// `\global` before a definition is read and dropped (a reply's table is global anyway).
const DEFINITION = /\\(?:global\s*\\)?(?:((?:re)?newcommand|providecommand)|([gex]?def)|(let)|(DeclareMathOperator))(?![A-Za-z])(\*?)/y

class MacroError extends Error {}

function skipSpace(s: string, i: number): number {
  let j = i
  while (j < s.length && /\s/.test(s[j] as string)) {
    j++
  }

  return j
}

/** A control sequence at i (`\name` or `\x`), or null. */
function controlAt(s: string, i: number): { name: string; end: number } | null {
  if (s[i] !== '\\' || i + 1 >= s.length) {
    return null
  }
  const word = /^[A-Za-z]+/.exec(s.slice(i + 1, i + 80))
  if (word) {
    return { name: `\\${word[0]}`, end: i + 1 + word[0].length }
  }
  const code = s.codePointAt(i + 1) ?? 0
  const size = code > 0xffff ? 2 : 1

  return { name: s.slice(i, i + 1 + size), end: i + 1 + size }
}

/** The end of the balanced `{...}` group opening at i (just past its `}`), or -1. */
function groupEnd(s: string, i: number): number {
  let depth = 0
  for (let j = i; j < s.length; j++) {
    const c = s[j]
    if (c === '\\') {
      j++
    } else if (c === '{') {
      depth++
    } else if (c === '}') {
      depth--
      if (depth === 0) {
        return j + 1
      }
    }
  }

  return -1
}

/** One undelimited argument at i, as TeX reads it: a group's content, a control sequence or one character. */
function readArgument(s: string, i: number): { text: string; end: number } | null {
  const j = skipSpace(s, i)
  const c = s[j]
  if (c === undefined || c === '}' || c === '#') {
    return null
  }
  if (c === '{') {
    const end = groupEnd(s, j)
    return end < 0 ? null : { text: s.slice(j + 1, end - 1), end }
  }
  const control = controlAt(s, j)
  if (control) {
    return { text: control.name, end: control.end }
  }
  const code = s.codePointAt(j) ?? 0
  const size = code > 0xffff ? 2 : 1

  return { text: s.slice(j, j + size), end: j + size }
}

/** An optional `[...]` argument at i (brackets inside braces do not close it), or null. */
function readOptional(s: string, i: number): { text: string; end: number } | null {
  const j = skipSpace(s, i)
  if (s[j] !== '[') {
    return null
  }
  let depth = 0
  for (let k = j + 1; k < s.length; k++) {
    const c = s[k]
    if (c === '\\') {
      k++
    } else if (c === '{') {
      depth++
    } else if (c === '}') {
      depth--
    } else if (c === ']' && depth === 0) {
      return { text: s.slice(j + 1, k), end: k + 1 }
    }
  }

  return null
}

/** The name a definition defines: `{\name}` or `\name`. */
function readName(s: string, i: number): { name: string; end: number } | null {
  const j = skipSpace(s, i)
  if (s[j] === '{') {
    const end = groupEnd(s, j)
    if (end < 0) {
      return null
    }
    const inner = s.slice(j + 1, end - 1).trim()
    const control = controlAt(inner, 0)
    return control && control.end === inner.length ? { name: control.name, end } : null
  }
  const control = controlAt(s, j)

  return control ? { name: control.name, end: control.end } : null
}

/** The definition starting at i: what it defines and where it ends; null when it is not one this reads. */
function readDefinition(s: string, i: number): { name: string; macro: Macro; end: number } | null {
  DEFINITION.lastIndex = i
  const m = DEFINITION.exec(s)
  if (!m) {
    return null
  }
  const star = m[5] === '*'
  const at = m.index + m[0].length
  if (m[1]) {
    const name = readName(s, at)
    if (!name) {
      return null
    }
    let end = name.end
    let arity = 0
    let optional: string | undefined
    const count = readOptional(s, end)
    if (count) {
      if (!/^\s*[0-9]\s*$/.test(count.text)) {
        return null
      }
      arity = Number(count.text.trim())
      end = count.end
      const fallback = readOptional(s, end)
      if (fallback && arity > 0) {
        optional = fallback.text
        end = fallback.end
      }
    }
    const j = skipSpace(s, end)
    if (s[j] !== '{') {
      return null
    }
    const close = groupEnd(s, j)
    if (close < 0) {
      return null
    }
    return { name: name.name, macro: { arity, optional, body: s.slice(j + 1, close - 1), provide: m[1] === 'providecommand' }, end: close }
  }
  if (m[2]) {
    if (star) {
      return null
    }
    const name = controlAt(s, skipSpace(s, at))
    if (!name) {
      return null
    }
    const open = s.indexOf('{', name.end)
    if (open < 0) {
      return null
    }
    // Only undelimited parameters, #1#2... in order.
    const params = s.slice(name.end, open).replace(/\s+/g, '')
    if (!/^(?:#[1-9])*$/.test(params) || params !== Array.from({ length: params.length / 2 }, (_, k) => `#${k + 1}`).join('')) {
      return null
    }
    const close = groupEnd(s, open)
    if (close < 0) {
      return null
    }
    return { name: name.name, macro: { arity: params.length / 2, body: s.slice(open + 1, close - 1) }, end: close }
  }
  if (m[3]) {
    if (star) {
      return null
    }
    const name = controlAt(s, skipSpace(s, at))
    if (!name) {
      return null
    }
    let j = skipSpace(s, name.end)
    if (s[j] === '=') {
      j = skipSpace(s, j + 1)
    }
    const target = controlAt(s, j)
    if (!target) {
      return null
    }
    return { name: name.name, macro: { arity: 0, body: target.name }, end: target.end }
  }
  const name = readName(s, at)
  if (!name) {
    return null
  }
  const text = readArgument(s, name.end)
  if (!text) {
    return null
  }

  return { name: name.name, macro: { arity: 0, body: `\\operatorname${star ? '*' : ''}{${text.text}}` }, end: text.end }
}

/** A formula with its definitions taken out, and the definitions (in order). */
function splitDefinitions(latex: string): { rest: string; found: { name: string; macro: Macro }[] } {
  const found: { name: string; macro: Macro }[] = []
  if (!DEFINES.test(latex)) {
    return { rest: latex, found }
  }
  let rest = ''
  let i = 0
  while (i < latex.length) {
    const c = latex[i]
    if (c === '\\') {
      const definition = readDefinition(latex, i)
      if (definition) {
        found.push({ name: definition.name, macro: definition.macro })
        i = skipSpace(latex, definition.end)
        continue
      }
      rest += latex.slice(i, i + 2)
      i += 2
      continue
    }
    rest += c
    i++
  }

  return { rest, found }
}

const ENDS_IN_WORD = /\\[A-Za-z]+$/
const STARTS_WITH_LETTER = /^[A-Za-z]/

/**
 * The body with `#n` replaced by the arguments. Where pasting would glue a
 * control word to a letter (`\lVert#1` with `x`, or `#1x` with `\alpha`), a
 * space keeps them apart, as TeX's own tokens are: `\lVert x`, not `\lVertx`.
 */
function substitute(body: string, args: string[]): string {
  return body.replace(/#(#|[1-9])/g, (all, which: string, at: number) => {
    if (which === '#') {
      return '#'
    }
    const arg = args[Number(which) - 1]
    if (arg === undefined) {
      return all
    }
    const before = ENDS_IN_WORD.test(body.slice(Math.max(0, at - 80), at)) && STARTS_WITH_LETTER.test(arg) ? ' ' : ''
    const after = ENDS_IN_WORD.test(arg) && STARTS_WITH_LETTER.test(body.slice(at + all.length, at + all.length + 1)) ? ' ' : ''

    return before + arg + after
  })
}

/** Expand every use of the table's macros in `latex`. Throws MacroError past the caps. */
function expandUses(latex: string, table: MacroTable, depth: number, budget: { left: number }): string {
  if (depth > MACRO_DEPTH) {
    throw new MacroError('macros nested too deep')
  }
  let out = ''
  let i = 0
  while (i < latex.length) {
    const c = latex[i] as string
    if (c !== '\\') {
      out += c
      i++
      continue
    }
    const control = controlAt(latex, i)
    const macro = control ? table.get(control.name) : undefined
    if (!control || !macro) {
      const end = control ? control.end : i + 1
      out += latex.slice(i, end)
      i = end
      continue
    }
    budget.left--
    if (budget.left < 0) {
      throw new MacroError('too many macro expansions')
    }
    const args: string[] = []
    let end = control.end
    if (macro.optional !== undefined) {
      const given = readOptional(latex, end)
      args.push(given ? given.text : macro.optional)
      end = given ? given.end : end
    }
    while (args.length < macro.arity) {
      const arg = readArgument(latex, end)
      if (!arg) {
        throw new MacroError(`${control.name} is missing an argument`)
      }
      args.push(arg.text)
      end = arg.end
    }
    let expanded = expandUses(substitute(macro.body, args), table, depth + 1, budget)
    // `\x` defined as `\alpha` and used as `\x b`: keep a space so the
    // result does not read as `\alphab`.
    if (ENDS_IN_WORD.test(expanded) && STARTS_WITH_LETTER.test(latex.slice(end, end + 1))) {
      expanded += ' '
    }
    out += expanded
    if (out.length > MACRO_LENGTH) {
      throw new MacroError('macro expansion too long')
    }
    i = end
  }

  return out
}

// The reply being parsed: its macros and the expansions made so far. Set by
// parseMessage for the length of one call, so every formula of the reply is
// read through it; null outside one, or for a reply that defines nothing.
let activeMacros: { table: MacroTable; done: Map<string, string> } | null = null

/**
 * A formula as it is drawn: its definitions taken out and the reply's
 * macros expanded. '' when the formula held definitions only; the formula as
 * written when an expansion breaks a cap or misses an argument.
 */
function expandMath(latex: string): string {
  const macros = activeMacros
  if (macros === null) {
    return latex
  }
  const done = macros.done.get(latex)
  if (done !== undefined) {
    return done
  }
  const { rest, found } = splitDefinitions(latex)
  let out: string
  if (found.length > 0 && rest.trim() === '') {
    out = ''
  } else {
    try {
      // '' when every use expanded to nothing (`\newcommand{\x}{}\x`): drawn as nothing.
      out = expandUses(rest, macros.table, 0, { left: MACRO_EXPANSIONS }).trim()
    } catch (err) {
      if (!(err instanceof MacroError)) {
        throw err
      }
      out = latex
    }
  }
  macros.done.set(latex, out)

  return out
}

/** Every definition in a list of formulas, the later winning. */
function macroTable(formulas: string[]): MacroTable {
  const table: MacroTable = new Map()
  for (const latex of formulas) {
    for (const { name, macro } of splitDefinitions(latex).found) {
      // \providecommand defines only what is not defined yet.
      if (!(macro.provide && table.has(name))) {
        table.set(name, macro)
      }
    }
  }

  return table
}

/** Expand the macros `definitions` defines in `latex` (for tests and callers outside a reply). */
export function expandMacros(latex: string, definitions: string[]): string {
  const saved = activeMacros
  activeMacros = { table: macroTable([...definitions, latex]), done: new Map() }
  try {
    return expandMath(latex)
  } finally {
    activeMacros = saved
  }
}

// ---------------------------------------------------------------------------
// Inline lexer (shared by hasMath, markdown conversion and paragraph parsing)

type Delim = { t: 'delim'; ch: '*' | '_' | '~'; n: number; origN: number; canOpen: boolean; canClose: boolean }
type Tok =
  | { t: 'text'; s: string }
  | { t: 'code'; s: string }
  | { t: 'math'; latex: string; display: boolean }
  | { t: 'br' }
  | { t: 'soft' }
  | { t: 'link'; url: string; children: Tok[] }
  | Delim

type Spanned = Tok & { start: number; end: number }

const ASCII_PUNCT = /[!-/:-@[-`{-~]/
const PUNCT = /[\p{P}\p{S}]/u
const SPACE = /\s/

/** A code span starting at i: its end and text, or null. */
function matchCode(text: string, i: number): { end: number; s: string } | null {
  let n = 0
  while (text[i + n] === '`') {
    n++
  }
  let j = i + n
  while (j < text.length) {
    const found = text.indexOf('`', j)
    if (found < 0) {
      return null
    }
    let m = 0
    while (text[found + m] === '`') {
      m++
    }
    if (m === n) {
      let s = text.slice(i + n, found).replace(/\n/g, ' ')
      if (s.length >= 2 && s.startsWith(' ') && s.endsWith(' ') && s.trim() !== '') {
        s = s.slice(1, -1)
      }
      return { end: found + m, s }
    }
    j = found + m
  }

  return null
}

/**
 * Shell or PHP variables in prose, which the pandoc rule alone takes for a
 * formula: `$HOME/$USER` (body "HOME/"), `$PATH:$HOME`, `${HOME}/${USER}`,
 * `$HOME$USER`. A formula seldom ends in '/', ':' or '"', and an upper-case
 * name of two or more characters directly followed by another name is a
 * variable; `$N$`, `$AB$`, `$n$th` stay formulas.
 */
function looksLikeShell(latex: string, after: string): boolean {
  if (/[/:"]$/.test(latex) && !latex.includes('\\')) {
    return true
  }

  return /^\{?[A-Z_][A-Z0-9_]+\}?$/.test(latex) && /[A-Za-z_{]/.test(after)
}

/** A formula starting at i: its end, LaTeX and kind, or null. */
function matchMath(text: string, i: number): { end: number; latex: string; display: boolean } | null {
  const char = text[i]
  if (char === '\\') {
    const next = text[i + 1]
    if (next !== '(' && next !== '[') {
      return null
    }
    const closer = next === '(' ? '\\)' : '\\]'
    let j = i + 2
    while (j < text.length) {
      const found = text.indexOf(closer, j)
      if (found < 0) {
        return null
      }
      // An escaped backslash before the closer (`\\)`) is not a closer.
      let slashes = 0
      while (text[found - 1 - slashes] === '\\' && found - 1 - slashes >= i + 2) {
        slashes++
      }
      if (slashes % 2 === 1) {
        j = found + 2
        continue
      }
      const latex = text.slice(i + 2, found).trim()
      if (latex === '' || /\n[ \t]*\n/.test(latex)) {
        return null
      }
      return { end: found + 2, latex, display: next === '[' }
    }
    return null
  }
  if (char !== '$') {
    return null
  }
  if (text[i + 1] === '$') {
    let j = i + 2
    while (j < text.length) {
      const found = text.indexOf('$$', j)
      if (found < 0) {
        return null
      }
      if (text[found - 1] === '\\') {
        j = found + 1
        continue
      }
      const latex = text.slice(i + 2, found).trim()
      if (latex === '' || /\n[ \t]*\n/.test(text.slice(i + 2, found))) {
        return null
      }
      return { end: found + 2, latex, display: true }
    }
    return null
  }
  const first = text[i + 1]
  if (first === undefined || SPACE.test(first)) {
    return null
  }
  for (let j = i + 1; j < text.length; j++) {
    const c = text[j]
    if (c === '\\') {
      j++
      continue
    }
    if (c === '`') {
      return null
    }
    if (c === '\n' && /^\n[ \t]*\n/.test(text.slice(j))) {
      return null
    }
    if (c === '$') {
      const before = text[j - 1] ?? ''
      const after = text[j + 1] ?? ''
      // The first bare `$` is the only candidate: a formula cannot hold one,
      // so "$5 and $x$" is a price and a formula, not one formula.
      if (j === i + 1 || SPACE.test(before) || /[0-9]/.test(after)) {
        return null
      }
      const latex = text.slice(i + 1, j).trim()
      if (looksLikeShell(latex, after)) {
        return null
      }
      return { end: j + 1, latex, display: false }
    }
  }

  return null
}

function isWideChar(char: string): boolean {
  return char !== '' && isWideCodePoint(char.codePointAt(0) ?? 0)
}

// Flanking rules, with a formula counted as a letter and a wide character
// next to punctuation counted as space (so `**“引用”**后` works).
function flanking(before: string, after: string): { left: boolean; right: boolean } {
  const beforeSpace = before === '' || SPACE.test(before)
  const afterSpace = after === '' || SPACE.test(after)
  const beforePunct = PUNCT.test(before)
  const afterPunct = PUNCT.test(after)
  const left = !afterSpace && (!afterPunct || beforeSpace || beforePunct || isWideChar(before))
  const right = !beforeSpace && (!beforePunct || afterSpace || afterPunct || isWideChar(after))

  return { left, right }
}

const ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0', copy: '©', mdash: '—', ndash: '–', hellip: '…', times: '×' }

type LexOptions = { links: boolean }

/** Lex inline markdown into tokens with source ranges. */
function lexInline(text: string, options: LexOptions): Spanned[] {
  const out: Spanned[] = []
  let buffer = ''
  let bufferStart = 0
  const flush = (at: number) => {
    if (buffer) {
      out.push({ t: 'text', s: buffer, start: bufferStart, end: at })
      buffer = ''
    }
    bufferStart = at
  }
  const push = (tok: Tok, start: number, end: number) => {
    flush(start)
    out.push({ ...tok, start, end } as Spanned)
    bufferStart = end
  }
  let i = 0
  while (i < text.length) {
    const char = text[i] as string
    if (char === '\\') {
      const math = matchMath(text, i)
      if (math) {
        push({ t: 'math', latex: expandMath(math.latex), display: math.display }, i, math.end)
        i = math.end
        continue
      }
      const next = text[i + 1] ?? ''
      if (next === '\n') {
        push({ t: 'br' }, i, i + 2)
        i += 2
        while (text[i] === ' ' || text[i] === '\t') {
          i++
        }
        bufferStart = i
        continue
      }
      if (ASCII_PUNCT.test(next)) {
        if (!buffer) {
          bufferStart = i
        }
        buffer += next
        i += 2
        continue
      }
    } else if (char === '$') {
      const math = matchMath(text, i)
      if (math) {
        push({ t: 'math', latex: expandMath(math.latex), display: math.display }, i, math.end)
        i = math.end
        continue
      }
      // A run of dollars that opens nothing is literal.
    } else if (char === '`') {
      const code = matchCode(text, i)
      let n = 0
      while (text[i + n] === '`') {
        n++
      }
      if (code) {
        push({ t: 'code', s: code.s }, i, code.end)
        i = code.end
      } else {
        if (!buffer) {
          bufferStart = i
        }
        buffer += text.slice(i, i + n)
        i += n
      }
      continue
    } else if (char === '*' || char === '_' || char === '~') {
      let n = 0
      while (text[i + n] === char) {
        n++
      }
      const prev = out.length > 0 && buffer === '' && out[out.length - 1]?.t === 'math' && out[out.length - 1]?.end === i ? 'a' : previousChar(text, i)
      const after = text[i + n] === '$' || text[i + n] === '\\' ? (matchMath(text, i + n) ? 'a' : text[i + n] ?? '') : nextChar(text, i + n)
      const { left, right } = flanking(prev, after)
      let canOpen = left
      let canClose = right
      if (char === '_') {
        canOpen = left && (!right || PUNCT.test(prev))
        canClose = right && (!left || PUNCT.test(after))
      }
      if (char === '~' && n > 2) {
        canOpen = false
        canClose = false
      }
      push({ t: 'delim', ch: char, n, origN: n, canOpen, canClose }, i, i + n)
      i += n
      continue
    } else if (char === '\n') {
      let hard = false
      let trim = buffer.length
      while (trim > 0 && buffer[trim - 1] === ' ') {
        trim--
      }
      hard = buffer.length - trim >= 2
      buffer = buffer.slice(0, trim)
      push(hard ? { t: 'br' } : { t: 'soft' }, i, i + 1)
      i++
      while (text[i] === ' ' || text[i] === '\t') {
        i++
      }
      bufferStart = i
      continue
    } else if (char === '[' && options.links) {
      const link = matchLink(text, i)
      if (link) {
        push({ t: 'link', url: link.url, children: lexInline(text.slice(link.textStart, link.textEnd), options) }, i, link.end)
        i = link.end
        continue
      }
    } else if (char === '!' && options.links && text[i + 1] === '[') {
      const link = matchLink(text, i + 1)
      if (link) {
        push({ t: 'link', url: link.url, children: lexInline(text.slice(link.textStart, link.textEnd), options) }, i, link.end)
        i = link.end
        continue
      }
    } else if (char === '<' && options.links) {
      const auto = /^<([a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^\s<>]*|[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*)>/.exec(text.slice(i, i + 2048))
      if (auto) {
        const target = auto[1] as string
        const url = /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(target) ? target : `mailto:${target}`
        push({ t: 'link', url, children: [{ t: 'text', s: target }] }, i, i + auto[0].length)
        i += auto[0].length
        continue
      }
    } else if (char === '&' && options.links) {
      const entity = /^&(?:#(\d{1,7})|#[xX]([0-9a-fA-F]{1,6})|([a-zA-Z]{2,8}));/.exec(text.slice(i, i + 12))
      if (entity) {
        const code = entity[1] ? Number(entity[1]) : entity[2] ? parseInt(entity[2], 16) : -1
        const decoded = code >= 0 ? (code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : '\ufffd') : ENTITIES[entity[3] ?? '']
        if (decoded !== undefined) {
          if (!buffer) {
            bufferStart = i
          }
          buffer += decoded
          i += entity[0].length
          continue
        }
      }
    }
    if (!buffer) {
      bufferStart = i
    }
    const code = text.charCodeAt(i)
    if (code >= 0xd800 && code <= 0xdbff && i + 1 < text.length) {
      buffer += text.slice(i, i + 2)
      i += 2
    } else {
      buffer += char
      i++
    }
  }
  flush(text.length)

  return out
}

function previousChar(text: string, i: number): string {
  if (i <= 0) {
    return ''
  }
  const code = text.charCodeAt(i - 1)
  if (code >= 0xdc00 && code <= 0xdfff && i >= 2) {
    return text.slice(i - 2, i)
  }

  return text[i - 1] as string
}

function nextChar(text: string, i: number): string {
  const code = text.codePointAt(i)

  return code === undefined ? '' : String.fromCodePoint(code)
}

/** `[text](url "title")` starting at the bracket: text range, url and end. */
function matchLink(text: string, i: number): { textStart: number; textEnd: number; url: string; end: number } | null {
  let depth = 0
  let j = i
  let close = -1
  // Link text ends at a blank line, and real link text is short: a bound
  // keeps text full of '[' from costing a scan to the end for each one.
  const limit = Math.min(text.length, i + 2048)
  while (j < limit) {
    const c = text[j]
    if (c === '\n' && /^\n[ \t]*\n/.test(text.slice(j, j + 64))) {
      return null
    }
    if (c === '\\') {
      const math = matchMath(text, j)
      j = math ? math.end : j + 2
      continue
    }
    if (c === '`') {
      const code = matchCode(text, j)
      if (code) {
        j = code.end
        continue
      }
    }
    if (c === '$') {
      const math = matchMath(text, j)
      if (math) {
        j = math.end
        continue
      }
    }
    if (c === '[') {
      depth++
    } else if (c === ']') {
      depth--
      if (depth === 0) {
        close = j
        break
      }
    }
    j++
  }
  if (close < 0 || text[close + 1] !== '(') {
    return null
  }
  let k = close + 2
  while (text[k] === ' ' || text[k] === '\n') {
    k++
  }
  let url = ''
  if (text[k] === '<') {
    const end = text.indexOf('>', k)
    if (end < 0 || text.slice(k, end).includes('\n')) {
      return null
    }
    url = text.slice(k + 1, end)
    k = end + 1
  } else {
    let parens = 0
    const start = k
    while (k < text.length) {
      if (k - start > 8192) {
        return null
      }
      const c = text[k] as string
      if (c === '\\' && k + 1 < text.length) {
        k += 2
        continue
      }
      if (SPACE.test(c)) {
        break
      }
      if (c === '(') {
        parens++
        if (parens > 32) {
          return null
        }
      } else if (c === ')') {
        if (parens === 0) {
          break
        }
        parens--
      }
      k++
    }
    url = text.slice(start, k).replace(/\\([!-/:-@[-`{-~])/g, '$1')
  }
  while (text[k] === ' ' || text[k] === '\n') {
    k++
  }
  const quote = text[k]
  if (quote === '"' || quote === "'" || quote === '(') {
    const closer = quote === '(' ? ')' : quote
    const end = text.indexOf(closer, k + 1)
    if (end < 0) {
      return null
    }
    k = end + 1
    while (text[k] === ' ' || text[k] === '\n') {
      k++
    }
  }
  if (text[k] !== ')') {
    return null
  }

  return { textStart: i + 1, textEnd: close, url, end: k + 1 }
}

/** Is there a formula in this inline text (code spans excluded)? */
function inlineHasMath(text: string): boolean {
  return /\$\S|\\[([]/.test(text) && lexInline(text, { links: false }).some(tok => tok.t === 'math')
}

// Characters that would change what markdown does with the stand-in text.
function escapeMarkdown(text: string): string {
  return text.replace(/[\\`*_[\]<>~$|#]/g, '\\$&')
}

/** Replace each formula in inline markdown with its Unicode stand-in; the line count stays. */
function convertInline(text: string): string {
  if (!/[$\\]/.test(text)) {
    return text
  }
  const tokens = lexInline(text, { links: false })
  if (!tokens.some(tok => tok.t === 'math')) {
    return text
  }
  let out = ''
  let at = 0
  for (const tok of tokens) {
    if (tok.t !== 'math') {
      continue
    }
    out += text.slice(at, tok.start)
    const source = text.slice(tok.start, tok.end)
    out += (tok.latex === '' ? '' : escapeMarkdown(toUnicode(tok.latex))) + '\n'.repeat((source.match(/\n/g) ?? []).length)
    at = tok.end
    // A formula of definitions only leaves no second space behind.
    if (tok.latex === '' && /[ \t]$/.test(out)) {
      while (text[at] === ' ' || text[at] === '\t') {
        at++
      }
    }
  }

  return out + text.slice(at)
}

// ---------------------------------------------------------------------------
// Block scanner

type Node =
  | { type: 'blank' | 'fence' | 'code' | 'html' | 'hr' | 'heading' | 'table' | 'paragraph' | 'math'; start: number; end: number }
  | { type: 'quote'; start: number; end: number; prefixes: string[]; inner: string[]; children: Node[] }
  | { type: 'list'; start: number; end: number; ordered: boolean; items: ListItem[] }

type ListItem = {
  start: number
  end: number
  marker: string
  number: number
  // Each line split into the part the item's indentation takes and the rest.
  prefixes: string[]
  inner: string[]
  children: Node[]
}

const BLANK = /^[ \t]*$/
const FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/
const ATX = /^ {0,3}#{1,6}(?:[ \t]|$)/
const HR = /^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/
const SETEXT = /^ {0,3}(?:=+|-+)[ \t]*$/
const QUOTE = /^ {0,3}>/
const LIST_ITEM = /^( {0,3})([-+*]|\d{1,9}[.)])(?=[ \t]|$)/
const HTML_BLOCK_TAGS = new Set(('address article aside base basefont blockquote body caption center col colgroup dd details dialog dir div dl dt fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr html iframe legend li link main menu menuitem nav noframes ol optgroup option p param search section summary table tbody td tfoot th thead title tr track ul').split(' '))
const MATH_OPEN = /^ {0,3}(\$\$|\\\[)/

function indentOf(line: string): number {
  let cols = 0
  for (const char of line) {
    if (char === ' ') {
      cols++
    } else if (char === '\t') {
      cols += 4 - (cols % 4)
    } else {
      break
    }
  }

  return cols
}

// Split off `cols` columns of leading indentation; a tab split in two leaves
// its remaining columns as spaces.
function stripColumns(line: string, cols: number): [string, string] {
  let used = 0
  let i = 0
  while (i < line.length && used < cols) {
    const char = line[i]
    if (char === ' ') {
      used++
      i++
    } else if (char === '\t') {
      const width = 4 - (used % 4)
      if (used + width > cols) {
        return [line.slice(0, i + 1), ' '.repeat(used + width - cols) + line.slice(i + 1)]
      }
      used += width
      i++
    } else {
      break
    }
  }

  return [line.slice(0, i), line.slice(i)]
}

function isTableDelimiter(line: string): boolean {
  return /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/.test(line) && (line.includes('|') || /-/.test(line))
}

function tableCells(line: string): number {
  const trimmed = line.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '')
  return trimmed.split(/(?<!\\)\|/).length
}

function isTableStart(lines: string[], i: number): boolean {
  const head = lines[i] ?? ''
  const delimiter = lines[i + 1]
  if (delimiter === undefined || !head.includes('|') || !isTableDelimiter(delimiter) || indentOf(head) >= 4) {
    return false
  }
  if (!delimiter.includes('|') && !head.trim().startsWith('|')) {
    return false
  }

  return tableCells(head) === tableCells(delimiter)
}

function htmlBlockKind(line: string): 'until-blank' | 'comment' | 'raw' | 'loose' | null {
  const m = /^ {0,3}<(\/?)([a-zA-Z][a-zA-Z0-9-]*)(?=[\s/>]|$)/.exec(line)
  if (/^ {0,3}<!--/.test(line)) {
    return 'comment'
  }
  if (!m) {
    return null
  }
  const tag = (m[2] as string).toLowerCase()
  if (!m[1] && ['script', 'pre', 'style', 'textarea'].includes(tag)) {
    return 'raw'
  }
  if (HTML_BLOCK_TAGS.has(tag)) {
    return 'until-blank'
  }
  if (/^ {0,3}<\/?[a-zA-Z][a-zA-Z0-9-]*(?:\s+[a-zA-Z_:][a-zA-Z0-9_.:-]*(?:\s*=\s*(?:[^\s"'=<>`]+|'[^']*'|"[^"]*"))?)*\s*\/?>[ \t]*$/.test(line)) {
    return 'loose'
  }

  return null
}

type ListMarker = { indent: number; marker: string; ordered: boolean; number: number; bullet: string; contentOffset: number; isEmpty: boolean }

function listMarker(line: string): ListMarker | null {
  if (indentOf(line) >= 4) {
    return null
  }
  const expanded = line.replace(/^[ \t]+/, space => ' '.repeat(indentOf(space)))
  const m = LIST_ITEM.exec(expanded)
  if (!m) {
    return null
  }
  const indent = (m[1] as string).length
  const marker = m[2] as string
  const rest = expanded.slice(indent + marker.length)
  const isEmpty = BLANK.test(rest)
  const spaces = indentOf(rest)
  const gap = isEmpty || spaces > 4 ? 1 : spaces
  const ordered = /\d/.test(marker)

  return {
    indent,
    marker,
    ordered,
    number: ordered ? Number(marker.slice(0, -1)) : 0,
    bullet: ordered ? marker.slice(-1) : marker,
    contentOffset: indent + marker.length + gap,
    isEmpty,
  }
}

// Find the end of a block formula that opens on line i, or -1.
function mathBlockEnd(lines: string[], i: number): number {
  const first = lines[i] as string
  const open = MATH_OPEN.exec(first)
  if (!open) {
    return -1
  }
  const closer = open[1] === '$$' ? '$$' : '\\]'
  const after = first.slice(open[0].length)
  const sameLine = after.indexOf(closer)
  if (sameLine >= 0) {
    return BLANK.test(after.slice(sameLine + 2)) && after.slice(0, sameLine).trim() !== '' ? i + 1 : -1
  }
  for (let j = i + 1; j < lines.length && j < i + 200; j++) {
    const line = lines[j] as string
    // A formula holds no blank line and no code fence: "$$ is the PID" is prose.
    if (BLANK.test(line) || FENCE.test(line)) {
      return -1
    }
    const at = line.indexOf(closer)
    if (at >= 0) {
      return BLANK.test(line.slice(at + 2)) ? j + 1 : -1
    }
  }

  return -1
}

// Can this line interrupt a paragraph?
function interruptsParagraph(lines: string[], i: number): boolean {
  const line = lines[i] as string
  if (BLANK.test(line)) {
    return true
  }
  if (indentOf(line) >= 4) {
    return false
  }
  if (FENCE.test(line) || ATX.test(line) || HR.test(line) || QUOTE.test(line)) {
    return true
  }
  if (mathBlockEnd(lines, i) >= 0) {
    return true
  }
  const kind = htmlBlockKind(line)
  if (kind && kind !== 'loose') {
    return true
  }
  const marker = listMarker(line)
  if (marker && !marker.isEmpty && (!marker.ordered || marker.number === 1)) {
    return true
  }

  return isTableStart(lines, i)
}

/** Scan lines into block nodes; nested containers are scanned recursively. */
function scanBlocks(lines: string[]): Node[] {
  const nodes: Node[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i] as string
    if (BLANK.test(line)) {
      nodes.push({ type: 'blank', start: i, end: i + 1 })
      i++
      continue
    }
    const indent = indentOf(line)
    if (indent >= 4) {
      let j = i + 1
      let last = i
      while (j < lines.length && (BLANK.test(lines[j] as string) || indentOf(lines[j] as string) >= 4)) {
        if (!BLANK.test(lines[j] as string)) {
          last = j
        }
        j++
      }
      nodes.push({ type: 'code', start: i, end: last + 1 })
      i = last + 1
      continue
    }
    const fence = FENCE.exec(line)
    if (fence && !((fence[2] as string)[0] === '`' && (fence[3] as string).includes('`'))) {
      const char = (fence[2] as string)[0] as string
      const length = (fence[2] as string).length
      let j = i + 1
      while (j < lines.length) {
        const close = /^ {0,3}(`+|~+)[ \t]*$/.exec(lines[j] as string)
        if (close && (close[1] as string)[0] === char && (close[1] as string).length >= length) {
          j++
          break
        }
        j++
      }
      nodes.push({ type: 'fence', start: i, end: j })
      i = j
      continue
    }
    const mathEnd = mathBlockEnd(lines, i)
    if (mathEnd > 0) {
      nodes.push({ type: 'math', start: i, end: mathEnd })
      i = mathEnd
      continue
    }
    if (ATX.test(line)) {
      nodes.push({ type: 'heading', start: i, end: i + 1 })
      i++
      continue
    }
    if (HR.test(line)) {
      nodes.push({ type: 'hr', start: i, end: i + 1 })
      i++
      continue
    }
    if (QUOTE.test(line)) {
      let j = i
      const prefixes: string[] = []
      const inner: string[] = []
      while (j < lines.length) {
        const current = lines[j] as string
        const m = /^ {0,3}> ?/.exec(current)
        if (m) {
          prefixes.push(m[0])
          inner.push(current.slice(m[0].length))
        } else if (j > i && !BLANK.test(current) && !BLANK.test(inner[inner.length - 1] ?? '') && !interruptsParagraph(lines, j)) {
          // A lazy continuation line.
          prefixes.push('')
          inner.push(current)
        } else {
          break
        }
        j++
      }
      nodes.push({ type: 'quote', start: i, end: j, prefixes, inner, children: scanBlocks(inner) })
      i = j
      continue
    }
    const html = htmlBlockKind(line)
    if (html) {
      let j = i + 1
      if (html === 'comment' || html === 'raw') {
        const closer = html === 'comment' ? /-->/ : /<\/(script|pre|style|textarea)>/i
        j = i
        while (j < lines.length && !closer.test(lines[j] as string)) {
          j++
        }
        j = Math.min(j + 1, lines.length)
      } else {
        while (j < lines.length && !BLANK.test(lines[j] as string)) {
          j++
        }
      }
      nodes.push({ type: 'html', start: i, end: j })
      i = j
      continue
    }
    const marker = listMarker(line)
    if (marker) {
      const list = scanList(lines, i, marker)
      nodes.push(list)
      i = list.end
      continue
    }
    if (isTableStart(lines, i)) {
      let j = i + 2
      while (j < lines.length && !BLANK.test(lines[j] as string) && !(interruptsParagraph(lines, j) && !(lines[j] as string).includes('|'))) {
        j++
      }
      nodes.push({ type: 'table', start: i, end: j })
      i = j
      continue
    }
    // Paragraph: runs until a blank line or a block that interrupts it.
    let j = i + 1
    let isHeading = false
    while (j < lines.length) {
      const next = lines[j] as string
      if (SETEXT.test(next) && !BLANK.test(next)) {
        isHeading = true
        j++
        break
      }
      if (interruptsParagraph(lines, j)) {
        break
      }
      j++
    }
    nodes.push({ type: isHeading ? 'heading' : 'paragraph', start: i, end: j })
    i = j
  }

  return nodes
}

// Split an item's first line into indentation, marker and gap, and content.
function splitMarker(line: string, marker: ListMarker): [string, string] {
  const [lead, afterLead] = stripColumns(line, marker.indent)
  const afterMarker = afterLead.slice(marker.marker.length)
  const gap = marker.contentOffset - marker.indent - marker.marker.length
  const [space, content] = stripColumns(afterMarker, gap)

  return [lead + marker.marker + space, content]
}

function scanList(lines: string[], start: number, first: ListMarker): Extract<Node, { type: 'list' }> {
  const items: ListItem[] = []
  let i = start
  let marker: ListMarker | null = first
  while (marker && i < lines.length) {
    const prefixes: string[] = []
    const inner: string[] = []
    const [head, rest] = splitMarker(lines[i] as string, marker)
    prefixes.push(head)
    inner.push(rest)
    let j = i + 1
    let lastContent = i
    let lastWasText = !marker.isEmpty
    while (j < lines.length) {
      const line = lines[j] as string
      if (BLANK.test(line)) {
        // A blank line may sit inside an item if more content follows.
        prefixes.push('')
        inner.push('')
        j++
        continue
      }
      if (indentOf(line) >= marker.contentOffset) {
        const [p, r] = stripColumns(line, marker.contentOffset)
        prefixes.push(p)
        inner.push(r)
        lastContent = j
        lastWasText = true
        j++
        continue
      }
      // A lazy continuation line of a paragraph.
      if (j === lastContent + 1 && lastWasText && !interruptsParagraph(lines, j) && !listMarker(line)) {
        prefixes.push('')
        inner.push(line)
        lastContent = j
        j++
        continue
      }
      break
    }
    const end = lastContent + 1
    prefixes.length = end - i
    inner.length = end - i
    // Content that was in an open fence or paragraph ends at the item's end.
    items.push({ start: i, end, marker: marker.marker, number: marker.number, prefixes, inner, children: scanBlocks(inner) })
    // The next sibling: same kind of marker, after at most blank lines.
    let k = end
    while (k < lines.length && BLANK.test(lines[k] as string)) {
      k++
    }
    const next = k < lines.length ? listMarker(lines[k] as string) : null
    if (next && next.ordered === first.ordered && next.bullet === first.bullet && next.indent < marker.contentOffset && !HR.test(lines[k] as string)) {
      i = k
      marker = next
    } else {
      marker = null
    }
  }
  const last = items[items.length - 1] as ListItem

  return { type: 'list', start, end: last.end, ordered: first.ordered, items }
}

// ---------------------------------------------------------------------------
// Does a block hold math?

function nodeHasMath(node: Node, lines: string[]): boolean {
  switch (node.type) {
    case 'blank':
    case 'fence':
    case 'code':
    case 'hr':
      return false
    case 'math':
      return true
    case 'quote':
      return node.children.some(child => nodeHasMath(child, node.inner))
    case 'list':
      return node.items.some(item => item.children.some(child => nodeHasMath(child, item.inner)))
    default:
      return inlineHasMath(lines.slice(node.start, node.end).join('\n'))
  }
}

// The info string of a fence that draws as a diagram: 'mermaid', any case,
// words after it ignored (as hooks/mermaid.ts's isMermaidInfo reads it).
function isDiagramInfo(info: string): boolean {
  return /^mermaid(?:\s|$)/i.test(info.trim())
}

/** A closed mermaid fence's body (the fence's indentation taken off its lines), or null. */
function diagramSource(lines: string[], node: Node): string | null {
  if (node.type !== 'fence' || node.end - node.start < 2) {
    return null
  }
  const open = FENCE.exec(lines[node.start] as string)
  if (!open || !isDiagramInfo(open[3] as string)) {
    return null
  }
  const fence = open[2] as string
  const close = /^ {0,3}(`+|~+)[ \t]*$/.exec(lines[node.end - 1] as string)
  if (!close || (close[1] as string)[0] !== fence[0] || (close[1] as string).length < fence.length) {
    return null
  }
  const indent = (open[1] as string).length

  return lines
    .slice(node.start + 1, node.end - 1)
    .map(line => stripColumns(line, Math.min(indent, indentOf(line)))[1])
    .join('\n')
}

function nodeHasDiagram(node: Node, lines: string[]): boolean {
  if (node.type === 'fence') {
    return diagramSource(lines, node) !== null
  }
  if (node.type === 'list') {
    return node.items.some(item => item.children.some(child => nodeHasDiagram(child, item.inner)))
  }

  return false
}

// A fence line that opens a mermaid block (the scanner decides whether it
// does). Anchored to a line start: unanchored, a long run of backticks made
// every position a start, and the test took time growing with its square.
const DIAGRAM_HINT = /^[ \t>*+\-\d.)]*(?:`{3,}|~{3,})[ \t]*mermaid/im

/** Can the markdown hold a mermaid fence at all? A scan, no parse. */
function mayHaveDiagram(markdown: string): boolean {
  return /mermaid/i.test(markdown) && DIAGRAM_HINT.test(markdown)
}

/** True if the markdown holds a mermaid fence this mod draws (top level or in a list item). */
export function hasDiagram(markdown: string): boolean {
  if (!mayHaveDiagram(markdown)) {
    return false
  }
  const lines = markdown.split(/\r?\n/)

  return scanBlocks(lines).some(node => nodeHasDiagram(node, lines))
}

/** True if the markdown holds a formula outside code. */
export function hasMath(markdown: string): boolean {
  if (!/[$\\]/.test(markdown) || !/\$\S|\\[([]/.test(markdown)) {
    return false
  }
  const lines = markdown.split(/\r?\n/)

  return scanBlocks(lines).some(node => nodeHasMath(node, lines))
}

// ---------------------------------------------------------------------------
// Markdown conversion: math to Unicode, line count kept

function convertNodes(nodes: Node[], lines: string[]): string[] {
  const out = lines.slice()
  for (const node of nodes) {
    switch (node.type) {
      case 'blank':
      case 'fence':
      case 'code':
      case 'hr':
        break
      case 'quote': {
        const inner = convertNodes(node.children, node.inner)
        inner.forEach((text, k) => {
          out[node.start + k] = (node.prefixes[k] ?? '') + text
        })
        break
      }
      case 'list':
        for (const item of node.items) {
          const inner = convertNodes(item.children, item.inner)
          inner.forEach((text, k) => {
            out[item.start + k] = (item.prefixes[k] ?? '') + text
          })
        }
        break
      case 'math': {
        const text = lines.slice(node.start, node.end).join('\n')
        const latex = mathBlockLatex(lines, node)
        const stand = latex === '' ? '' : escapeMarkdown(toUnicode(latex))
        const indent = /^[ \t]*/.exec(text)?.[0] ?? ''
        out[node.start] = indent + stand
        for (let k = node.start + 1; k < node.end; k++) {
          out[k] = ''
        }
        break
      }
      case 'table':
      case 'heading':
        for (let k = node.start; k < node.end; k++) {
          out[k] = convertInline(lines[k] as string)
        }
        break
      default: {
        const converted = convertInline(lines.slice(node.start, node.end).join('\n')).split('\n')
        converted.forEach((text, k) => {
          out[node.start + k] = text
        })
      }
    }
  }

  return out
}

// ---------------------------------------------------------------------------
// Paragraph parsing: inline tokens to Inline[]

type Tree = { t: 'text'; s: string } | { t: 'code'; s: string } | { t: 'math'; latex: string } | { t: 'br' } | { t: 'soft' } | { t: 'span'; style: Style; children: Tree[] } | Delim

// Resolve emphasis over a token list (CommonMark's delimiter algorithm).
function resolveEmphasis(tokens: Tok[]): Tree[] {
  const items: Tree[] = tokens.map(tok => {
    if (tok.t === 'link') {
      return { t: 'span', style: { link: tok.url }, children: resolveEmphasis(tok.children) }
    }
    if (tok.t === 'delim') {
      return { ...tok }
    }
    if (tok.t === 'math') {
      return { t: 'math', latex: tok.latex }
    }

    return tok
  })
  let c = 0
  while (c < items.length) {
    const closer = items[c] as Tree
    if (closer.t !== 'delim' || !closer.canClose || closer.n === 0) {
      c++
      continue
    }
    let o = c - 1
    let opener: Delim | null = null
    for (; o >= 0; o--) {
      const candidate = items[o] as Tree
      if (candidate.t !== 'delim' || candidate.ch !== closer.ch || !candidate.canOpen || candidate.n === 0) {
        continue
      }
      if (closer.ch === '~') {
        if (candidate.n !== closer.n) {
          continue
        }
      } else if ((candidate.canClose || closer.canOpen) && (candidate.origN + closer.origN) % 3 === 0 && !(candidate.origN % 3 === 0 && closer.origN % 3 === 0)) {
        continue
      }
      opener = candidate
      break
    }
    if (!opener) {
      c++
      continue
    }
    const use = closer.ch === '~' ? closer.n : opener.n >= 2 && closer.n >= 2 ? 2 : 1
    const style: Style = closer.ch === '~' ? { strike: true } : use === 2 ? { bold: true } : { italic: true }
    const span: Tree = { t: 'span', style, children: items.slice(o + 1, c) }
    opener.n -= use
    closer.n -= use
    items.splice(o + 1, c - o - 1, span)
    c = o + 2
    if (opener.n === 0) {
      items.splice(o, 1)
      c--
    }
    if (closer.n === 0) {
      items.splice(c, 1)
    }
  }

  return items
}

function sameStyle(a: Style, b: Style): boolean {
  return !!a.bold === !!b.bold && !!a.italic === !!b.italic && !!a.code === !!b.code && !!a.strike === !!b.strike && a.link === b.link
}

function cleanStyle(style: Style): Style {
  const out: Style = {}
  if (style.bold) {
    out.bold = true
  }
  if (style.italic) {
    out.italic = true
  }
  if (style.code) {
    out.code = true
  }
  if (style.strike) {
    out.strike = true
  }
  if (style.link !== undefined) {
    out.link = style.link
  }

  return out
}

// Set where a formula of definitions only was left out, so the text after it
// does not keep a second space ("define $\newcommand...$ then" reads
// "define then").
let afterDropped = false

function flatten(trees: Tree[], style: Style, out: Inline[]): void {
  const pushText = (given: string, at: Style) => {
    let text = given
    const last = out[out.length - 1]
    if (afterDropped && text !== '') {
      afterDropped = false
      if (last?.kind === 'text' && /[ \t]$/.test(last.text)) {
        text = text.replace(/^[ \t]+/, '')
      }
    }
    if (text === '') {
      return
    }
    if (last?.kind === 'text' && sameStyle(last.style, at)) {
      last.text += text
    } else {
      out.push({ kind: 'text', text, style: cleanStyle(at) })
    }
  }
  for (const tree of trees) {
    switch (tree.t) {
      case 'text':
        pushText(tree.s, style)
        break
      case 'code':
        pushText(tree.s, { ...style, code: true })
        break
      case 'math':
        // A formula of macro definitions only draws as nothing.
        if (tree.latex !== '') {
          out.push({ kind: 'math', latex: tree.latex })
          afterDropped = false
        } else {
          afterDropped = true
        }
        break
      case 'br':
        pushText('\n', style)
        break
      case 'soft':
        pushText('\u0000', style)
        break
      case 'delim':
        pushText(tree.ch.repeat(tree.n), style)
        break
      case 'span':
        flatten(tree.children, { ...style, ...tree.style }, out)
        break
    }
  }
}

// A soft break between two wide characters vanishes; elsewhere it is a space.
function resolveSoftBreaks(inlines: Inline[]): Inline[] {
  const out: Inline[] = []
  inlines.forEach((inline, index) => {
    if (inline.kind !== 'text' || !inline.text.includes('\u0000')) {
      out.push(inline)
      return
    }
    let text = ''
    const chars = Array.from(inline.text)
    chars.forEach((char, k) => {
      if (char !== '\u0000') {
        text += char
        return
      }
      const before = k > 0 ? chars[k - 1] : lastChar(inlines[index - 1])
      const after = k + 1 < chars.length ? chars[k + 1] : firstChar(inlines[index + 1])
      if (!(before && after && isWideChar(before) && isWideChar(after))) {
        text += ' '
      }
    })
    if (text !== '') {
      out.push({ ...inline, text })
    }
  })

  return out
}

function lastChar(inline: Inline | undefined): string | undefined {
  return inline?.kind === 'text' ? Array.from(inline.text).pop() : undefined
}

function firstChar(inline: Inline | undefined): string | undefined {
  return inline?.kind === 'text' ? Array.from(inline.text)[0] : undefined
}

function toInlines(tokens: Tok[]): Inline[] {
  const out: Inline[] = []
  afterDropped = false
  flatten(resolveEmphasis(tokens), {}, out)
  const resolved = resolveSoftBreaks(out)
  // Trim a leading or trailing hard break and surrounding spaces.
  const first = resolved[0]
  if (first?.kind === 'text') {
    first.text = first.text.replace(/^[ \n]+/, '')
  }
  const last = resolved[resolved.length - 1]
  if (last?.kind === 'text') {
    last.text = last.text.replace(/[ \n]+$/, '')
  }

  return resolved.filter(inline => inline.kind === 'math' || inline.text !== '')
}

type Piece = { kind: 'inline'; inlines: Inline[]; source: string } | { kind: 'display'; latex: string }

// Split a paragraph's text at its display formulas.
function splitParagraph(text: string): Piece[] {
  const tokens = lexInline(text, { links: true })
  const pieces: Piece[] = []
  let current: Spanned[] = []
  let currentStart = 0
  const close = (end: number) => {
    const source = text.slice(currentStart, end).trim()
    const inlines = toInlines(current)
    if (inlines.length > 0) {
      pieces.push({ kind: 'inline', inlines, source })
    }
    current = []
  }
  for (let k = 0; k < tokens.length; k++) {
    const tok = tokens[k] as Spanned
    if (tok.t === 'math' && tok.display && tok.latex === '') {
      continue
    }
    if (tok.t === 'math' && tok.display) {
      close(tok.start)
      let latex = tok.latex
      currentStart = tok.end
      // Sentence punctuation right after a display formula belongs to it
      // ("$$x^2$$." is not a paragraph that starts with ".").
      const next = tokens[k + 1]
      const punct = next?.t === 'text' ? /^[.,;:!?。，；：！？、]+/u.exec(next.s)?.[0] : undefined
      if (next?.t === 'text' && punct) {
        latex += /^[\x20-\x7e]+$/.test(punct) ? `\\,{${punct}}` : `\\text{${punct}}`
        tokens[k + 1] = { ...next, s: next.s.slice(punct.length), start: next.start + punct.length }
        currentStart = next.start + punct.length
      }
      pieces.push({ kind: 'display', latex })
    } else {
      current.push(tok)
    }
  }
  close(text.length)

  return pieces
}

function inlinesHaveMath(inlines: Inline[]): boolean {
  return inlines.some(inline => inline.kind === 'math')
}

// ---------------------------------------------------------------------------
// parseMessage

// `gap`: a blank line was passed since the last block went out.
type Emit = { blocks: Block[]; gap: boolean }

function push(emit: Emit, block: Block): void {
  if (emit.gap) {
    block.gapBefore = true
  }
  emit.gap = false
  emit.blocks.push(block)
}

// Join a paragraph's lines, dropping each line's leading indentation.
function paragraphText(lines: string[], node: Node): string {
  return lines
    .slice(node.start, node.end)
    .map(line => line.replace(/^[ \t]+/, ''))
    .join('\n')
    .replace(/[ \t]+$/, '')
}

function mathBlockSource(lines: string[], node: Node): string {
  const text = lines.slice(node.start, node.end).join('\n').trim()
  const open = text.startsWith('$$') ? '$$' : '\\['
  const close = open === '$$' ? '$$' : '\\]'
  const end = text.lastIndexOf(close)

  return text.slice(open.length, end >= open.length ? end : text.length).trim()
}

// A block formula as drawn: '' when it held only macro definitions.
function mathBlockLatex(lines: string[], node: Node): string {
  return expandMath(mathBlockSource(lines, node))
}

// Blocks for the content of a list item, or a paragraph that holds math.
function emitParagraph(lines: string[], node: Node, marker: string | undefined, indent: number, inList: boolean, emit: Emit): void {
  const pieces = splitParagraph(paragraphText(lines, node))
  let first = true
  const hang = indent + (marker ? stringWidth(marker) : 0)
  for (const piece of pieces) {
    if (piece.kind === 'display') {
      if (first && marker) {
        push(emit, { kind: 'paragraph', inlines: [], marker, indent })
      }
      push(emit, { kind: 'display', latex: piece.latex, indent: marker || !first ? hang : indent })
      first = false
      continue
    }
    if (!inList && !inlinesHaveMath(piece.inlines)) {
      // Formulas of definitions only, if any, come out; the source is
      // otherwise kept as written (a link's address included).
      push(emit, { kind: 'markdown', text: activeMacros !== null ? convertInline(piece.source) : piece.source })
    } else if (first && marker) {
      push(emit, { kind: 'paragraph', inlines: piece.inlines, marker, indent })
    } else {
      push(emit, { kind: 'paragraph', inlines: piece.inlines, indent: inList ? hang : indent })
    }
    first = false
  }
  if (first && marker) {
    push(emit, { kind: 'paragraph', inlines: [], marker, indent })
  }
}

function letters(n: number): string {
  let out = ''
  for (let rest = n; rest > 0; rest = Math.floor(rest / 26)) {
    rest--
    out = String.fromCharCode(97 + (rest % 26)) + out
  }

  return out
}

const ROMAN: [number, string][] = [
  [1000, 'm'], [900, 'cm'], [500, 'd'], [400, 'cd'], [100, 'c'], [90, 'xc'],
  [50, 'l'], [40, 'xl'], [10, 'x'], [9, 'ix'], [5, 'v'], [4, 'iv'], [1, 'i'],
]

function roman(n: number): string {
  let out = ''
  let rest = n
  for (const [value, digits] of ROMAN) {
    while (rest >= value) {
      out += digits
      rest -= value
    }
  }

  return out
}

// The engine's list markers: '-' for every bullet; ordered items numbered
// from the first, as 1. at the top level, a. one level in, i. two levels in
// (where the numbers allow), always with '.'.
function orderedLabel(depth: number, n: number, first: number, last: number): string {
  if (depth === 1 && first >= 1) {
    return letters(n)
  }
  if (depth === 2 && first >= 1 && last <= 3999) {
    return roman(n)
  }

  return String(n)
}

function emitList(list: Extract<Node, { type: 'list' }>, indent: number, depth: number, emit: Emit): void {
  const first = list.items[0]?.number ?? 1
  const last = first + list.items.length - 1
  let number = first
  let previous: ListItem | undefined
  for (const item of list.items) {
    const marker = list.ordered ? `${orderedLabel(depth, number, first, last)}. ` : '- '
    number++
    if (previous !== undefined && item.start > previous.end) {
      emit.gap = true // a loose list: blank lines between its items
    }
    previous = item
    const hang = indent + marker.length
    let markerLeft: string | undefined = marker
    const markerOnly = () => {
      if (markerLeft) {
        push(emit, { kind: 'paragraph', inlines: [], marker: markerLeft, indent })
      }
    }
    for (const child of item.children) {
      if (child.type === 'blank') {
        if (!markerLeft) {
          emit.gap = true
        }
        continue
      }
      if (child.type === 'paragraph') {
        emitParagraph(item.inner, child, markerLeft, markerLeft ? indent : hang, true, emit)
      } else if (child.type === 'list') {
        markerOnly()
        emitList(child, hang, depth + 1, emit)
      } else if (diagramSource(item.inner, child) !== null) {
        markerOnly()
        push(emit, { kind: 'mermaid', source: diagramSource(item.inner, child) as string, indent: hang })
      } else if (child.type === 'math') {
        const latex = mathBlockLatex(item.inner, child)
        if (latex === '') {
          continue
        }
        markerOnly()
        push(emit, { kind: 'display', latex, indent: hang })
      } else {
        markerOnly()
        const converted = convertNodes([child], item.inner).slice(child.start, child.end)
        push(emit, { kind: 'markdown', text: converted.join('\n'), indent: hang })
      }
      markerLeft = undefined
    }
    markerOnly()
  }
}

function mergeMarkdown(blocks: Block[]): Block[] {
  const out: Block[] = []
  for (const block of blocks) {
    const last = out[out.length - 1]
    if (block.kind === 'markdown' && last?.kind === 'markdown' && (last.indent ?? 0) === (block.indent ?? 0)) {
      last.text = `${last.text}${block.gapBefore ? '\n\n' : '\n'}${block.text}`
    } else {
      out.push(block.kind === 'markdown' ? { ...block } : block)
    }
  }

  return out.filter(block => block.kind !== 'markdown' || block.text.trim() !== '')
}

// Every formula of the nodes as written, in order (code left out).
function collectFormulas(nodes: Node[], lines: string[], out: string[]): void {
  for (const node of nodes) {
    switch (node.type) {
      case 'blank':
      case 'fence':
      case 'code':
      case 'hr':
        break
      case 'math':
        out.push(mathBlockSource(lines, node))
        break
      case 'quote':
        collectFormulas(node.children, node.inner, out)
        break
      case 'list':
        for (const item of node.items) {
          collectFormulas(item.children, item.inner, out)
        }
        break
      default:
        for (const tok of lexInline(lines.slice(node.start, node.end).join('\n'), { links: false })) {
          if (tok.t === 'math') {
            out.push(tok.latex)
          }
        }
    }
  }
}

/**
 * Split a reply into markdown to draw as is, paragraphs with formulas,
 * display formulas and diagrams. The reply's own macros are expanded in
 * every formula of it.
 */
export function parseMessage(markdown: string): Block[] {
  const mayMath = /[$\\]/.test(markdown) && /\$\S|\\[([]/.test(markdown)
  if (!mayMath && !mayHaveDiagram(markdown)) {
    return [{ kind: 'markdown', text: markdown }]
  }
  const lines = markdown.split(/\r?\n/)
  const nodes = scanBlocks(lines)
  if (!nodes.some(node => nodeHasMath(node, lines) || nodeHasDiagram(node, lines))) {
    return [{ kind: 'markdown', text: markdown }]
  }
  const saved = activeMacros
  activeMacros = null
  if (DEFINES.test(markdown)) {
    const formulas: string[] = []
    collectFormulas(nodes, lines, formulas)
    if (formulas.some(latex => DEFINES.test(latex))) {
      activeMacros = { table: macroTable(formulas), done: new Map() }
    }
  }
  try {
    return emitBlocks(lines, nodes)
  } finally {
    activeMacros = saved
  }
}

function emitBlocks(lines: string[], nodes: Node[]): Block[] {
  const emit: Emit = { blocks: [], gap: false }
  // Lines of markdown not yet emitted, from `pending` to the last node's end.
  let pendingStart = -1
  let pendingEnd = -1
  let pendingGap = false
  let sawBlank = false
  let converted: string[] | null = null
  const flushPending = () => {
    if (pendingStart >= 0) {
      const source = converted ?? lines
      emit.gap = pendingGap
      push(emit, { kind: 'markdown', text: source.slice(pendingStart, pendingEnd).join('\n') })
    }
    pendingStart = -1
  }
  for (const node of nodes) {
    if (node.type === 'blank') {
      sawBlank = true
      continue
    }
    const diagram = diagramSource(lines, node)
    const drawn =
      node.type === 'math' ||
      diagram !== null ||
      ((node.type === 'paragraph' || node.type === 'list') && nodeHasMath(node, lines)) ||
      (node.type === 'list' && nodeHasDiagram(node, lines))
    if (!drawn) {
      if (nodeHasMath(node, lines)) {
        converted = converted ?? lines.slice()
        convertNodes([node], lines).slice(node.start, node.end).forEach((text, k) => {
          ;(converted as string[])[node.start + k] = text
        })
      }
      if (pendingStart < 0) {
        pendingStart = node.start
        pendingGap = sawBlank
      }
      pendingEnd = node.end
      sawBlank = false
      continue
    }
    flushPending()
    emit.gap = sawBlank
    sawBlank = false
    if (diagram !== null) {
      push(emit, { kind: 'mermaid', source: diagram, indent: 0 })
    } else if (node.type === 'math') {
      const latex = mathBlockLatex(lines, node)
      if (latex === '') {
        continue // definitions only
      }
      push(emit, { kind: 'display', latex, indent: 0 })
    } else if (node.type === 'list') {
      emitList(node, 0, 0, emit)
    } else {
      emitParagraph(lines, node, undefined, 0, false, emit)
    }
  }
  flushPending()

  return mergeMarkdown(emit.blocks)
}

/** Every formula that needs a picture, once each. */
export function mathRefs(blocks: Block[]): MathRef[] {
  const seen = new Set<string>()
  const out: MathRef[] = []
  const add = (latex: string, display: boolean) => {
    const key = `${display ? 'D' : 'I'}${latex}`
    if (!seen.has(key)) {
      seen.add(key)
      out.push({ latex, display })
    }
  }
  for (const block of blocks) {
    if (block.kind === 'display') {
      add(block.latex, true)
    } else if (block.kind === 'paragraph') {
      for (const inline of block.inlines) {
        if (inline.kind === 'math') {
          add(inline.latex, false)
        }
      }
    }
  }

  return out
}

// ---------------------------------------------------------------------------
// Line layout

// Characters a line may not start with, and may not end with.
const NO_START = new Set(Array.from('，。、；：！？）」』】〕〉》〗〙〛｝］｠〞”’…‥・ー々ゝゞ〻ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶ％‰℃,.;:!?)]}%'))
const NO_END = new Set(Array.from('（「『【〔〈《〖〘〚｛［｟〝“‘([{'))

type Atom =
  | { kind: 'text'; text: string; style: Style; width: number; isWide: boolean }
  | { kind: 'math'; latex: string; state: PictureState; width: number; text?: string }

type Unit = { atoms: Atom[]; width: number; gap: { text: string; style: Style; width: number } | null }
type Entry = Unit | 'break'

function atomFirst(atom: Atom): string {
  return atom.kind === 'text' ? (graphemes(atom.text)[0] ?? '') : ''
}

function atomLast(atom: Atom): string {
  return atom.kind === 'text' ? (graphemes(atom.text).pop() ?? '') : ''
}

function canBreakBetween(a: Atom, b: Atom): boolean {
  const aWide = a.kind === 'text' && a.isWide
  const bWide = b.kind === 'text' && b.isWide
  if (!aWide && !bWide) {
    return false
  }

  return !NO_START.has(atomFirst(b)) && !NO_END.has(atomLast(a))
}

function isPicture(state: PictureState): state is Picture {
  return state !== 'pending' && 'png' in state
}

function mathWidth(latex: string, state: PictureState): number {
  if (isPicture(state)) {
    return Math.max(1, state.columns)
  }

  return Math.max(1, stringWidth(standIn(latex, state)))
}

// What a formula without a picture draws as: Unicode while it is pending or
// the renderer is down, its own source once TeX refused it.
function standIn(latex: string, state: PictureState): string {
  return state !== 'pending' && 'error' in state && !state.isBackend ? `$${latex}$` : toUnicode(latex)
}

// Break inlines into units no line may break inside, each with the space before it.
function toEntries(inlines: Inline[], picture: (ref: MathRef) => PictureState): Entry[] {
  const entries: Entry[] = []
  let unit: Unit | null = null
  let gap: Unit['gap'] = null
  const closeUnit = () => {
    if (unit) {
      entries.push(unit)
      unit = null
    }
  }
  const addAtom = (atom: Atom) => {
    const current = unit as Unit | null
    const previous = current?.atoms[current.atoms.length - 1]
    if (current && previous && gap === null && !canBreakBetween(previous, atom)) {
      current.atoms.push(atom)
      current.width += atom.width
      return
    }
    closeUnit()
    unit = { atoms: [atom], width: atom.width, gap }
    gap = null
  }
  for (const inline of inlines) {
    if (inline.kind === 'math') {
      const state = picture({ latex: inline.latex, display: false })
      addAtom({ kind: 'math', latex: inline.latex, state, width: mathWidth(inline.latex, state) })
      continue
    }
    const parts = inline.text.match(/\n|[ \t]+|[^\s]+/g) ?? []
    for (const part of parts) {
      if (part === '\n') {
        closeUnit()
        entries.push('break')
        gap = null
        continue
      }
      if (/^[ \t]/.test(part)) {
        closeUnit()
        const text = part.replace(/\t/g, ' ')
        const before = gap as Unit['gap']
        gap = before ? { text: before.text + text, style: before.style, width: before.width + text.length } : { text, style: inline.style, width: text.length }
        continue
      }
      // A word: wide characters each stand alone, the rest stays together.
      let word = ''
      const flushWord = () => {
        if (word) {
          addAtom({ kind: 'text', text: word, style: inline.style, width: stringWidth(word), isWide: false })
          word = ''
        }
      }
      for (const cluster of graphemes(part)) {
        if (isWide(cluster)) {
          flushWord()
          addAtom({ kind: 'text', text: cluster, style: inline.style, width: 2, isWide: true })
        } else {
          word += cluster
        }
      }
      flushWord()
    }
  }
  closeUnit()

  return entries
}

// Cut text into pieces no wider than `room` (the first) and `full` (the rest).
function hardSplit(text: string, room: number, full: number): string[] {
  const pieces: string[] = []
  let current = ''
  let used = 0
  let limit = room
  for (const cluster of graphemes(text)) {
    const width = graphemeWidth(cluster)
    if (used + width > limit && current !== '') {
      pieces.push(current)
      current = ''
      used = 0
      limit = full
    } else if (used + width > limit && current === '' && pieces.length === 0 && limit < full) {
      pieces.push('')
      limit = full
    }
    current += cluster
    used += width
  }
  if (current !== '' || pieces.length === 0) {
    pieces.push(current)
  }

  return pieces
}

// Fill lines with the words of a text (a formula's stand-in has spaces),
// cutting a word only when it is wider than a whole line. Spaces at the
// edges of a piece are dropped.
function wrapText(text: string, room: number, full: number): string[] {
  const pieces: string[] = []
  let current = ''
  let used = 0
  let limit = room
  const next = () => {
    pieces.push(current.trimEnd())
    current = ''
    used = 0
    limit = full
  }
  for (const word of text.match(/\S+|\s+/g) ?? []) {
    if (/^\s/.test(word)) {
      if (used > 0) {
        current += ' '
        used++
      }
      continue
    }
    const width = stringWidth(word)
    if (used + width <= limit) {
      current += word
      used += width
      continue
    }
    if (used > 0 && width <= full) {
      next()
      current = word
      used = width
      continue
    }
    const cut = hardSplit(word, limit - used, full)
    cut.forEach((part, k) => {
      if (k > 0) {
        next()
      }
      current += part
      used += stringWidth(part)
    })
  }
  if (current.trim() !== '' || pieces.length === 0) {
    pieces.push(current.trimEnd())
  }

  return pieces
}

type Placed = Atom | { kind: 'gap'; text: string; style: Style; width: number }

/** Lay a paragraph out as lines of runs at the given width (cells). */
export function layoutParagraph(block: Extract<Block, { kind: 'paragraph' }>, picture: (ref: MathRef) => PictureState, width: number): Line[] {
  const total = Math.max(1, Math.floor(Number.isFinite(width) ? width : 80))
  const indent = Math.max(0, Math.floor(block.indent))
  const marker = block.marker ?? ''
  const hang = indent + stringWidth(marker)
  const firstPrefix = ' '.repeat(indent) + marker
  const restPrefix = ' '.repeat(hang)
  const roomFor = (lineIndex: number) => Math.max(1, total - (lineIndex === 0 ? stringWidth(firstPrefix) : hang))

  const placedLines: Placed[][] = [[]]
  let used = 0
  const current = () => placedLines[placedLines.length - 1] as Placed[]
  const newLine = () => {
    // Trailing spaces never end a line.
    const line = current()
    while (line.length > 0 && line[line.length - 1]?.kind === 'gap') {
      line.pop()
    }
    placedLines.push([])
    used = 0
  }
  const room = () => roomFor(placedLines.length - 1)

  const placeAtom = (atom: Atom) => {
    if (atom.width <= room() - used) {
      current().push(atom)
      used += atom.width
      return
    }
    if (atom.kind === 'math' && isPicture(atom.state)) {
      if (used > 0) {
        newLine()
      }
      current().push(atom)
      used += atom.width
      return
    }
    // Text, or a formula's stand-in, is cut to fit.
    const text = atom.kind === 'math' ? (atom.text ?? standIn(atom.latex, atom.state)) : atom.text
    const pieces = wrapText(text, room() - used, roomFor(placedLines.length))
    pieces.forEach((piece, k) => {
      if (k > 0) {
        newLine()
      }
      if (piece !== '') {
        const pieceWidth = stringWidth(piece)
        current().push({ ...atom, text: piece, width: pieceWidth })
        used += pieceWidth
      }
    })
  }

  for (const entry of toEntries(block.inlines, picture)) {
    if (entry === 'break') {
      newLine()
      continue
    }
    const gapWidth = used > 0 && entry.gap ? entry.gap.width : 0
    if (used > 0 && used + gapWidth + entry.width > room()) {
      newLine()
    }
    if (used > 0 && entry.gap) {
      current().push({ kind: 'gap', ...entry.gap })
      used += entry.gap.width
    }
    if (used + entry.width <= room()) {
      for (const atom of entry.atoms) {
        current().push(atom)
        used += atom.width
      }
      continue
    }
    // Wider than a whole line: place atom by atom, splitting text.
    for (const atom of entry.atoms) {
      placeAtom(atom)
    }
  }
  const last = current()
  while (last.length > 0 && last[last.length - 1]?.kind === 'gap') {
    last.pop()
  }

  return placedLines.map((placed, lineIndex) => toLine(placed, lineIndex === 0 ? firstPrefix : restPrefix))
}

function toLine(placed: Placed[], prefix: string): Line {
  let lift = 0
  for (const item of placed) {
    if (item.kind === 'math' && isPicture(item.state)) {
      lift = Math.max(lift, item.state.textRow)
    }
  }
  const runs: Run[] = []
  const addText = (text: string, style: Style) => {
    if (text === '') {
      return
    }
    const last = runs[runs.length - 1]
    if (last?.kind === 'text' && sameStyle(last.style, style)) {
      last.text += text
    } else {
      runs.push({ kind: 'text', text, style: cleanStyle(style), marginTop: lift })
    }
  }
  addText(prefix, {})
  let height = lift + 1
  for (const item of placed) {
    if (item.kind === 'gap' || item.kind === 'text') {
      addText(item.text, item.style)
      continue
    }
    const state = item.state
    if (isPicture(state)) {
      const marginTop = lift - state.textRow
      runs.push({ kind: 'image', picture: state, latex: item.latex, marginTop })
      height = Math.max(height, marginTop + state.rows)
    } else {
      const text = item.text ?? standIn(item.latex, state)
      runs.push({ kind: 'fallback', text, latex: item.latex, marginTop: lift, isError: state !== 'pending' })
    }
  }

  return { runs, height }
}
