# Compatibility

What the renderer (`bin/render.py`, behind `bin/client.py` and the daemon
`bin/renderd.py`) draws, measured on three corpora in `bin/corpus/`:

| Corpus | Source | Formulas | Drawn | Drawn, allow-list aside |
|---|---|---|---|---|
| `katex.txt` | every formula of KaTeX's [support table](https://katex.org/docs/support_table.html) and [supported functions](https://katex.org/docs/supported.html) pages (fetched 2026-10-27) | 1200 | 1185 (98.8 %) | 100 % |
| `mathjax.txt` | examples for the MathJax 3 TeX extensions (base, ams, amscd, bbox, boldsymbol, braket, cancel, cases, centernot, color, colorv2, empheq, enclose, extpfeil, gensymb, html, mathtools, mhchem, physics, upgreek, unicode, verb, textmacros, action) | 150 | 142 (94.7 %) | 100 % |
| `claude.txt` | formulas as Claude writes them: analysis, linear algebra, probability, ML, physics, chemistry, CS, Chinese `\text`, aligned derivations | 239 | 239 (100 %) | 100 % |

Run `python3 bin/coverage.py` (options: `katex mathjax claude`,
`--in-process`, `--quiet`, `--json`) to measure again; it prints every
failure with its TeX error. `bin/test_coverage.py` fails when a formula
outside the allow-list below fails, or when an allow-list entry draws (or
is in no corpus), so this page stays true.

## How a formula gets drawn

1. `hooks/layout.ts` finds the math in a reply and expands the reply's own
   `\newcommand`, `\renewcommand`, `\providecommand`, `\def`, `\gdef`,
   `\let` and `\DeclareMathOperator`, with or without `\global` before
   them (decision 6). The backend never sees a definition.
2. `prepare()` in `render.py` rewrites what KaTeX and MathJax accept and
   TeX does not (next section), then the safety check refuses anything that
   could read or write files, run programs, define commands or leave the
   formula's box. The rewrites run before the check, so the check sees
   their result.
3. TeX typesets the formula with a preamble precompiled into a format:
   lmodern, amsmath, amssymb, mathtools, mathrsfs, cancel, xcolor, bm,
   CJKutf8, mhchem, siunitx, dsfont, stmaryrd (with the kernel's binary
   `\bigtriangleup`/`\bigtriangledown` kept), amscd, ulem, centernot,
   fontenc (T1 glyphs, OT1 stays the default), then the user's `packages`
   and `macros`, then `COMPAT_KATEX` and `COMPAT` (aliases and
   approximations; a name a user package defined is kept). A glyph the
   fonts lack is a TeX error (`\tracinglostchars=3`), not a blank.
   arydshln is not loaded: it removed the vertical rules from the first
   row of every array (an augmented matrix `{cc|c}` lost its bar).
4. Ink outside a formula's box is kept: a formula with a CD diagram,
   `\mathclap`/`\llap`/`\rlap`, `\smash`, `\makebox` or a kern gets a
   60pt margin on its page (others 1pt), so a CD arrow's side label or a
   `\mathclap` label under a brace is not cut off.

## KaTeX and MathJax syntax that TeX lacks

| What | How it is drawn |
|---|---|
| KaTeX aliases: `\darr \dArr \Darr \uarr \uArr \Uarr \larr \lArr \Larr \rarr \rArr \Rarr \harr \hArr \Harr \lrarr \lrArr \Lrarr \lang \rang \sub \sube \supe \infin \isin \notni \natnums \reals \Reals \Complex \cnums \alef \alefsym \exist \image \real \weierp \thetasym \plusmn \sdot \bull \clubs \diamonds \hearts \spades \Dagger \sect` | defined in the preamble |
| `\empty` (∅ in KaTeX, a kernel macro in LaTeX), `\and` | renamed to `\emptyset`, `\land` before TeX |
| `\ch \sh \cth \tg \ctg \cotg \arctg \arcctg \cosec \plim` | operators |
| colon relations (`\coloncolon`, `\colonequals`, `\minuscolon`...) | mathtools' forms |
| `\Bra \Ket \Braket{a\|b\|c}` | `\Braket` turns each top-level `\|` into a `\middle` bar |
| `\Overrightarrow \overleftharpoon \overrightharpoon \overlinesegment \underlinesegment \overgroup \undergroup \utilde` | built from amsmath's arrow fills and plain TeX's brace pieces |
| `\widecheck` | the mathabx glyph in five widths, set as text (a symbol font would spend one of TeX's 16 math families) |
| `\oiint` | the esint glyph, set as text (esint itself would redraw every integral) |
| `\oiiint` | approximation: `\oiint` and an overlapping `\int` |
| `\xtwoheadrightarrow \xtwoheadleftarrow \xlongequal \xtofrom` | amsmath's extensible arrows |
| `\lBrace \rBrace \origof \imageof \phase \angl \angln` | approximations from two glyphs |
| `\mathsfit` | lmss slanted |
| `\KaTeX`; `\TeX`, `\LaTeX`, `\th` in math | as text |
| `\mathreflectbox`, `\reflectbox`, `\scalebox`, `\rotatebox`, `\resizebox` | their content, unchanged: dvipng cannot mirror, scale or rotate glyphs |
| `\Huge ... \tiny` in math | the rest of the group goes in a box at that size |
| `\vcenter{math}` | a centred box with the math in it |
| `\mskip{10mu}`, `\above{2pt}`, `\kern{..}`, `\hskip{..}` | the braces dropped |
| `\rule{18mu}{18mu}` | mu converted to em |
| `\char"263A` (above 255) | the Unicode character, then the Unicode table |
| `\color{#00f}`, `\textcolor`, `\colorbox`, `\fcolorbox` with `#rgb` or `#rrggbb` | xcolor's `[HTML]` form |
| CSS colour names (`aqua`, `teal`, `navy`...) | defined; xcolor's own names keep xcolor's values |
| `$...$` and `\(...\)` inside `\text`, `\mbox`, `\fbox`, `\hbox`, `\colorbox`, `\raisebox`... | `\ensuremath{...}`; a `$` anywhere else is still refused |
| `darray`, `CD` | environments (amscd for `CD`; side labels of vertical arrows are kept) |
| KaTeX's dashed rules: `:` in an array's column specification, `\hdashline`, `\cdashline` | drawn solid: `\|`, `\hline`, `\cline` (only the column argument of `array`, `darray`, `subarray`, `tabular` is rewritten; a `:` elsewhere is a colon) |
| `\pu{123 kJ/mol}` | number and unit through siunitx |
| `\verb!...!`, `\url{...}` | the characters as typed, in typewriter |
| MathJax `\bbox[colour, border]{x}` | `\colorbox`, `\fbox` or `\fcolorbox`; padding and other CSS dropped |
| MathJax `\enclose{notation}{x}` | box, rounded box and circle as `\boxed`, strikes as `\cancel`/`\bcancel`/`\xcancel`, top/bottom as over/underline; others draw the content |
| MathJax `\unicode{x2124}` | the character |
| MathJax `numcases`, `subnumcases` | the left side, then `cases` |
| MathJax `empheq` | its inner environment, with `left=`/`right=` delimiters or `box=` |
| `\intertext`, `\shortintertext` | the blocks one under the other (columns are aligned within each block only) |
| plain TeX `\matrix{..\cr..}`, `\pmatrix{..}` | the environments |
| MathJax upgreek (`\upalpha`... `\Upomega`) | Euler glyphs, set as text (no math family spent) |
| MathJax physics: `\qty() \qty[] \qty\|\| \qty{} \pqty \bqty \Bqty \vqty \eval \order \comm \acomm \pb \expval \ev \mel \ketbra \op \ip \dyad \vu \va \vdot \cross \cp \curl \divergence \laplacian \trace \fdv \Res \pv \erfc \mqty() \pmqty \bmqty \vmqty \Bmqty \smqty`, `\bra \ket \braket` | defined without the physics package, so `\div`, `\Re` and `\Im` stay standard; `\qty{5}{\meter}` (two arguments) stays siunitx's. A user who adds `physics` to `packages` gets physics' own commands instead |
| physics' derivatives: `\dv{x}`, `\dv[n]{f}{x}`, `\pdv{f}{x}`, `\pdv[n]{f}{x}`, `\pdv{f}{x}{y}` (mixed: ∂²f/∂x∂y), `\dd{x}`, `\dd[3]{x}` (d³x) | as physics draws them |
| physics' `\vb{x}` (bold), `\vb*{x}` (bold italic) | `\mathbf`, `\bm` |
| `\abs{x}`, `\norm{x}` | bars that grow with their content (`\left...\right`), as physics and MathJax draw them; the starred form too (in mathtools the star means "grow"; in physics it means "do not": here both grow), and `\abs[\big]{x}` picks a size. `\floor`, `\ceil` are mathtools' |
| physics' powers `\sin[2](x)`, `\cos[n]\theta` (trigonometric, hyperbolic, `\exp`, `\log`, `\ln`; the power a number or `n`, `m`, `k`) | `\sin^{2}(x)`; `\sin[x]` stays brackets |
| `\perthousand \iddots \Rule \Space` | defined |

Web-only commands draw their content, or nothing: `\href{url}{x}` and
`\htmlClass`, `\htmlId`, `\htmlStyle`, `\htmlData`, `\class`, `\cssId`,
`\style`, `\data`, `\mmlToken` draw `x`; `\mathtip{x}{tip}`,
`\texttip{x}{tip}` and `\toggle{x}{y}...\endtoggle` draw `x`; `\require{..}`
draws nothing. Their other arguments are removed from the text before TeX
or the safety check sees anything, so a URL or CSS string can never reach
TeX.

## Unicode

Unicode math characters in a formula become TeX commands (`render.UNICODE_MATH`,
then `bin/unimath.py`):

- 290 symbols generated from unicode-math's table, kept only when this
  preamble draws them (regenerate with `python3 bin/unimath.py` after a
  preamble change), plus KaTeX's own mappings (`┌` as `\ulcorner`, `⎰` as
  `\lmoustache`...).
- Mathematical letters and digits (`𝐀 𝑥 𝔄 𝕜 𝟙 𝛂 𝒜 𝖠 𝚊`, and `ℬ ℰ ℋ`...),
  derived from their Unicode names: bold, italic, bold italic, script,
  fraktur, double-struck, sans-serif (all four), monospace. Lower-case
  script letters draw as italic (no font here has them), and double-struck
  lower case only as `𝕜`.
- In `\text{...}` they are wrapped in `\ensuremath` (any mapping TeX
  reads as math, also `°` as `{}^\circ`: `\text{25 °C}` draws).
- Straight double quotes in text (`\text{"free"}`) pair up as “ and ”
  (the default OT1 font has no straight one); in `\texttt` they stay.

## Chinese, Japanese and Korean

CJK text goes through the CJK package (CJKutf8), one font per formula:

| Script | Font | Result |
|---|---|---|
| Simplified Chinese | `gbsn` (Arphic, GB2312) | works (as in v1) |
| Traditional Chinese | `bsmi` (Arphic, Big5), chosen when a character is in Big5 and not in GB2312 | works (`繁體中文：機率與統計`) |
| Japanese | `ipxm` (IPAex Mincho), chosen when the text has kana, or when its kanji are in Shift JIS but not in GB2312 or Big5 (`学習率`, `変数`, `期待値`) | works |
| Korean | `mj` (UHC Myeongjo), chosen when the text has Hangul | works |

A font that is not installed is not chosen (`--check` lists the installed
ones as `cjkFonts`). Without Hangul or kana, the first font whose
character set holds the whole text is chosen, in the order Simplified,
Traditional, Japanese, Korean. Limits: one font per formula, so a formula
that mixes scripts (Hangul with Hanja, kana with simplified-only
characters, `这個` with simplified and traditional characters) gets the
font of its first script, and a character that font lacks is an error
(the line then shows as Unicode text, as any failed formula does). v1
dropped such characters without a word.

## tikz-cd

Works, best effort. `\begin{tikzcd}...\end{tikzcd}` (only that
environment; `\tikz`, `\pgf...` and every other TikZ command stay refused)
is typeset alone, with tikz-cd added to its own precompiled format, and
drawn by `dvips -R2` (no shell commands, no absolute paths) and
Ghostscript `-dSAFER` (dvipng cannot draw TikZ's PostScript: it misplaces
the node text). TikZ options that run code, define keys or write files
are refused (`/.`, `exec`, `execute at ...`, `remember picture`,
`overlay`, `external`, `insert path`, `... after command`, `path picture`,
`name prefix`/`suffix`); every control sequence inside goes through the
usual check. A diagram costs about 400 ms (no warm process; measured from
a fresh Python process), the first one about 1 s more to build its format.
`--check` reports `diagrams: true` when tikz-cd, dvips and gs are there. Formulas themselves never start
Ghostscript: dvipng runs with `--noghostscript`. When tikz-cd, dvips or gs
is missing, the diagram fails with that reason and the line shows its
source.

## Known differences

- Approximations (listed above) look close to KaTeX's, not the same:
  `\oiiint`, `\lBrace`, `\phase`, `\angl`, `\origof`, `\imageof`,
  `\xtofrom`, `\utilde`, MathJax `\enclose{circle}`.
- Size commands in math cannot change the size of a script.
- TeX has 16 math families. The preamble keeps 10 for itself; every math
  alphabet used in one formula (`\mathsf`, `\mathtt`, `\mathit`, `\mathbf`,
  `\mathscr`, `\mathfrak`, `\mathds`, `\mathsfit`, `\bm`...) takes one more, so
  a single formula with all of them at once fails ("Too many math
  alphabets"); eleven at once still draw.
- `\hdashline`, `\cdashline` and `:` columns draw as solid rules.
- physics' `\var` (functional variation, δ) is not defined: in a reply it
  is as likely to mean variance, and a wrong picture is worse than the
  Unicode stand-in a failed formula gets.
- A command KaTeX allows with `trust` only is drawn without its effect
  (`\href`, `\url`, `\html...`), and `\includegraphics` is refused.

## Allow-list

Formulas of the corpora that are known not to draw. The test reads the
block below: one line per formula, `reason: formula` (a reason never
holds `: `).

- `reply-macro`: a definition, which `hooks/layout.ts` expands before the
  backend; the backend refuses definitions by design. Every one of these
  draws after layout.ts's expansion (checked once with layout.ts compiled by
  tsc; coverage.py cannot run TypeScript).
- `reply-macro-gap`: as `reply-macro`, but layout.ts's expansion does not
  draw: `\edef`/`\xdef` are not expanded.
- `definition`: a definition layout.ts does not handle (`\Newextarrow`,
  `\DeclarePairedDelimiter`).
- `no-glyph`: no installed font has the character (☺).
- `file-access`: `\includegraphics` loads an image from a URL; refused.
- `vertical-mode`: `\moveleft` works only between paragraphs in TeX.
- `plain-cases`: plain TeX's `\cases` with `$...$` in its text column.

```allow-list
reply-macro: \def\sqr#1{#1^2} \sqr{y}
reply-macro: \def\arraystretch{1.5}\begin{array}{cc}a&b\\c&d\end{array}
reply-macro: \def\arraystretch{1.5}\begin{array}{c:c:c} a & b & c \\ \hline d & e & f \\ \hdashline g & h & i \end{array}
reply-macro: \def\foo{x^2} \foo + \foo
reply-macro: \gdef\sqr#1{#1^2} \sqr{y} + \sqr{y}
reply-macro: \gdef\foo#1{#1^2} \foo{y} + \foo{y}
reply-macro: \newcommand\chk{\checkmark} \chk
reply-macro: \def\hail{Hi!}\renewcommand\hail{\text{Ahoy!}} \hail
reply-macro: \def\RR{\mathbb{R}} f\colon \RR \to \RR
reply-macro: \let\phi\varphi \phi(x)
reply-macro-gap: \def\foo{a}\edef\fcopy{\foo}\def\foo{}\fcopy
reply-macro-gap: \def\foo{a}\xdef\fcopy{\foo}\def\foo{}\fcopy
reply-macro: \global\def\add#1#2{#1+#2} \add 2 3
reply-macro: \providecommand\greet{\text{Hello}} \greet
reply-macro: \newcommand{\norm}[1]{\lVert#1\rVert} \norm{x}
definition: \Newextarrow{\xrightharpoonup}{5,10}{0x21C0} \xrightharpoonup{x}
definition: \DeclarePairedDelimiter\abs{\lvert}{\rvert} \abs{x}
no-glyph: \char"263a
no-glyph: \unicode{x263A}
file-access: \includegraphics[height=0.8em, totalheight=0.9em, width=0.9em, alt=KA logo]{https://cdn.kastatic.org/images/apple-touch-icon-57x57-precomposed.new.png}
file-access: \includegraphics[height=0.8em, totalheight=0.9em, width=0.9em, alt=KA logo]{https://katex.org/img/khan-academy.png}
vertical-mode: \moveleft 1em \hbox{x} \raise 2pt \hbox{y} \lower 2pt \hbox{z}
plain-cases: \cases{x & if $x>0$ \cr -x & otherwise}
```
