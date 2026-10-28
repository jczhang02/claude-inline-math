"""Render LaTeX formulas to PNG pictures that fill whole boxes of terminal cells.

Usage:
    render.py            JSON request on stdin, JSON response on stdout
    render.py --check    JSON status of the tools this renderer needs (an
                         optional {"options": {...}} on stdin is checked too)
    render.py --detect   JSON {"cell", "fg"} the 'auto' options resolve to now

The mod runs client.py, which passes the same request to a long-lived
renderd.py (warm TeX processes) and falls back to this module in-process.

Request:
    {"items": [{"latex": str, "display": bool}, ...],
      "options": {"fg": "auto" | "#rrggbb", "cell": "auto" | "WxH",
                              "baseline": 0.773, "ptPx": 3.4, "minShrink": 0.8,
                              "cacheDir": str | null, "maxColumns": 255,
                              "packages": [str, ...], "macros": str}}

`packages` (extra LaTeX packages) and `macros` (\\newcommand lines) come from
the user, not the model: they go into the preamble unchecked. A package name
that is malformed or not installed is skipped and reported by --check.

Response (results in item order, exit status 0 even when items fail):
    {"cell": {"width", "height"}, "fg": "#rrggbb",
      "results": [{"png", "columns", "rows", "textRow"} | {"error"}, ...]}

Each picture is drawn at the terminal's own pixel size (columns x cell width
by rows x cell height), so the terminal never rescales it. Its baseline sits
on the text baseline of cell row `textRow`, so text beside it lines up when
that text is drawn `textRow` rows down.

The preamble is precompiled into a LaTeX format (cacheDir/fmt), rebuilt
when the renderer, the preamble or the TeX installation changes; without a
format the preamble is read on every run, as before.

All items of a request are typeset in one latex run (one preview page per
item, between two sentinel pages that must come out the same). A TeX error
stops that run at the bad item; the bad item gets the TeX "!" line as its
error (checked alone when other items ran before it) and the other items go
into the next run. Results are cached on disk, so a hit costs no TeX run.

The LaTeX comes from model output, so it is checked before TeX sees it:
commands that read or write files, run programs, or change TeX's own rules
are rejected, latex runs with -no-shell-escape and paranoid file access in a
private temporary folder, and every run has a time limit.
"""

import base64
import errno
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

try:
    import unimath
except ImportError:  # run as a script with -I: its folder is not on the path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import unimath
try:
    import fcntl
except ImportError:  # not POSIX: no format builds, no locks
    fcntl = None
try:
    import resource
except ImportError:
    resource = None

VERSION = 'inline-math-render 6'
SUPERSAMPLE = 3
PAD = 2  # transparent pixels left and right of a formula
DARK_FG = '#cdd6f4'  # catppuccin mocha text
LIGHT_FG = '#4c4f69'  # catppuccin latte text
DEFAULT_CELL = (19, 42)
MAX_LATEX = 4000
MAX_BOX = 255  # columns and rows of an Image element
MIN_SCALE = 0.25  # smaller than this and a formula is "too large"
MAX_PNG = 2 * 1024 * 1024
CACHE_MAX_ENTRIES = 2000
CACHE_MAX_BYTES = 200 * 1024 * 1024
LATEX_TIMEOUT = 10.0  # one latex run, plus 0.5 s per item in it
LATEX_MEMORY = 1 << 30  # address space of one latex run
LATEX_LOG_MAX = 16 * 1024 * 1024  # largest file latex may write (its log)
LOG_READ = 4 * 1024 * 1024  # the markers and the first error are near the start
MAX_PIXELS = 64_000_000  # largest page dvipng may raster
DVIPNG_TIMEOUT = 10.0
DEADLINE = 30.0  # whole request; later items get {"error": "timeout"}
DETECT_TIMEOUT = 1.0

PACKAGES_TTL = 600.0  # seconds a cached kpsewhich answer (or a failed format build) is trusted
FAILURE_TTL = 86400.0  # a preamble that TeX itself refused (a "!" error): the same input fails the same way
FORMAT_TIMEOUT = 60.0  # building one format
FORMAT_FILE_MAX = 256 * 1024 * 1024  # largest format file latex -ini may write
FORMAT_KEEP = 6  # formats kept in the format folder
USER_PACKAGE = re.compile(r'[A-Za-z0-9-]{1,64}')
MAX_USER_PACKAGES = 64
MAX_MACROS = 64 * 1024

# ================================================================ preamble
# The one place to extend what TeX knows. PACKAGES are loaded in this order
# when installed (a missing one is skipped; REQUIRED ones make --check fail),
# then the user's `packages`, preview, the user's `macros`, and COMPAT. All
# of it is precompiled into the format, so a longer list costs nothing per
# formula; keep the total within TeX's 16 math families.

REQUIRED = ('preview', 'amsmath', 'amssymb')
PACKAGES = [  # (name, options)
    # [T1,OT1]: the T1 glyphs (\th, \dh...) are there, OT1 stays the default.
    ('fontenc', '[T1,OT1]'),
    ('lmodern', ''), ('amsmath', ''), ('amssymb', ''), ('mathtools', ''), ('mathrsfs', ''),
    ('cancel', ''), ('xcolor', ''), ('bm', ''), ('CJKutf8', ''),
    ('mhchem', '[version=4]'), ('siunitx', ''), ('dsfont', ''), ('stmaryrd', ''),
    ('amscd', ''), ('ulem', '[normalem]'), ('centernot', ''),
]
# Lines around a package's \usepackage: stmaryrd turns the kernel's binary
# \bigtriangleup/\bigtriangledown into big operators; KaTeX and LaTeX keep
# them binary (symmetric difference), so the kernel's come back after it.
# arydshln is not loaded: it drops the vertical rules from the first row of
# every array; KaTeX's dashed rules are drawn solid instead (katex_syntax).
AROUND = {
    'stmaryrd': (r'\let\imbigtriangleup\bigtriangleup\let\imbigtriangledown\bigtriangledown',
                 r'\let\bigtriangleup\imbigtriangleup\let\bigtriangledown\imbigtriangledown'),
}
# Space kept around each formula's box, as ink outside the box is cut off at
# the page edge: 1pt holds an italic overhang. A formula built to put ink
# outside its box (a CD arrow's side label, \mathclap text, \rlap...) gets
# OVERHANG_BORDER_PT on its own page. fit() crops the transparent margin away.
PREVIEW_BORDER_PT = 1
OVERHANG_BORDER_PT = 60
OVERHANG = re.compile(r'\\(?:math[clr]lap|[clr]lap|smash|begin\s*\{CD\}|makebox)(?![A-Za-z])'
                      r'|\\(?:hspace\*?|kern|mkern|hskip|mskip)\s*\{?\s*-')  # only a negative space overhangs
SENTINEL = (r'\pi\alpha+\sum_{i=1}^{n}x_i^2\le\frac{a}{b}\sqrt{y}\int\mathbb{R}\in\infty'
            r'\cdot\left(\mathcal{L}\right)\text{Ab}')

# What KaTeX and MathJax know that LaTeX does not: their aliases, and
# approximations of their own commands, from the glyphs these fonts have.
# A missing glyph is an error (\tracinglostchars), not a blank.
COMPAT_KATEX = r'''
\tracinglostchars=3
\providecommand\darr{\downarrow}\providecommand\dArr{\Downarrow}\providecommand\Darr{\Downarrow}
\providecommand\uarr{\uparrow}\providecommand\uArr{\Uparrow}\providecommand\Uarr{\Uparrow}
\providecommand\larr{\leftarrow}\providecommand\lArr{\Leftarrow}\providecommand\Larr{\Leftarrow}
\providecommand\rarr{\rightarrow}\providecommand\rArr{\Rightarrow}\providecommand\Rarr{\Rightarrow}
\providecommand\harr{\leftrightarrow}\providecommand\hArr{\Leftrightarrow}\providecommand\Harr{\Leftrightarrow}
\providecommand\lrarr{\leftrightarrow}\providecommand\lrArr{\Leftrightarrow}\providecommand\Lrarr{\Leftrightarrow}
\providecommand\lang{\langle}\providecommand\rang{\rangle}
\providecommand\sub{\subset}\providecommand\sube{\subseteq}\providecommand\supe{\supseteq}
\providecommand\infin{\infty}\providecommand\isin{\in}\providecommand\notni{\not\ni}
\providecommand\natnums{\mathbb{N}}\providecommand\reals{\mathbb{R}}\providecommand\Reals{\mathbb{R}}
\providecommand\Complex{\mathbb{C}}\providecommand\cnums{\mathbb{C}}
\providecommand\alef{\aleph}\providecommand\alefsym{\aleph}\providecommand\exist{\exists}
\providecommand\image{\Im}\providecommand\real{\Re}\providecommand\weierp{\wp}
\providecommand\thetasym{\vartheta}\providecommand\plusmn{\pm}\providecommand\sdot{\cdot}
\providecommand\bull{\bullet}\providecommand\clubs{\clubsuit}\providecommand\diamonds{\diamondsuit}
\providecommand\hearts{\heartsuit}\providecommand\spades{\spadesuit}\providecommand\Dagger{\ddagger}
\providecommand\sect{\S}\providecommand\sixptsize{\scriptsize}
\providecommand\ch{\operatorname{ch}}\providecommand\sh{\operatorname{sh}}
\providecommand\cth{\operatorname{cth}}\providecommand\tg{\operatorname{tg}}
\providecommand\ctg{\operatorname{ctg}}\providecommand\cotg{\operatorname{cotg}}
\providecommand\arctg{\operatorname{arctg}}\providecommand\arcctg{\operatorname{arcctg}}
\providecommand\cosec{\operatorname{cosec}}\providecommand\plim{\operatorname*{plim}}
\providecommand\Bra[1]{\left\langle #1\right\rvert}\providecommand\Ket[1]{\left\lvert #1\right\rangle}
\providecommand\coloncolon{\dblcolon}\providecommand\colonequals{\coloneqq}
\providecommand\colonminus{\coloneq}\providecommand\coloncolonequals{\Coloneqq}
\providecommand\coloncolonminus{\Coloneq}\providecommand\equalscolon{\eqqcolon}
\providecommand\equalscoloncolon{\Eqqcolon}\providecommand\minuscolon{\eqcolon}
\providecommand\minuscoloncolon{\Eqcolon}\providecommand\coloncolonapprox{\Colonapprox}
\providecommand\coloncolonsim{\Colonsim}
\providecommand\approxcoloncolon{\mathrel{\approx\mathrel{\mkern-1.2mu}\dblcolon}}
\providecommand\simcoloncolon{\mathrel{\sim\mathrel{\mkern-1.2mu}\dblcolon}}
\providecommand\lBrace{\mathopen{\{\mkern-6.5mu\{}}\providecommand\rBrace{\mathclose{\}\mkern-6.5mu\}}}
\providecommand\origof{\mathrel{\circ\mkern-3mu\relbar\mkern-3mu\bullet}}
\providecommand\imageof{\mathrel{\bullet\mkern-3mu\relbar\mkern-3mu\circ}}
\providecommand\KaTeX{\text{K\kern-.17em\raisebox{.21em}{\scriptsize A}\kern-.15em T\kern-.1667em%
  \raisebox{-.5ex}{E}\kern-.125em X}}
\providecommand\angl[1]{{\left.\overline{#1\,}\right\rvert}}\providecommand\angln{\angl{n}}
\providecommand\phase[1]{\mathord{\underline{\smash{/}\mkern-2mu#1}}}
\DeclareMathAlphabet{\mathsfit}{OT1}{lmss}{m}{sl}
\DeclareTextSymbolDefault{\th}{T1}\DeclareTextSymbolDefault{\TH}{T1}
\DeclareTextSymbolDefault{\dh}{T1}\DeclareTextSymbolDefault{\DH}{T1}
\DeclareTextSymbolDefault{\ng}{T1}\DeclareTextSymbolDefault{\NG}{T1}
\DeclareFontFamily{U}{mathx}{\hyphenchar\font45}
\DeclareFontShape{U}{mathx}{m}{n}{<-> mathx10}{}
\makeatletter
% Text commands that also work in math, as in KaTeX.
\newcommand\im@text[1]{\ifmmode\text{#1}\else#1\fi}
\NewCommandCopy\im@TeX\TeX\DeclareRobustCommand\TeX{\im@text\im@TeX}
\NewCommandCopy\im@LaTeX\LaTeX\DeclareRobustCommand\LaTeX{\im@text\im@LaTeX}
\NewCommandCopy\im@th\th\DeclareRobustCommand\th{\im@text\im@th}
% Arrows and accents of KaTeX, built as amsmath builds its own.
\def\im@Rightarrowfill{\arrowfill@\Relbar\Relbar\Rightarrow}
\def\im@lharpoonfill{\arrowfill@\leftharpoonup\relbar\relbar}
\def\im@rharpoonfill{\arrowfill@\relbar\relbar\rightharpoonup}
\def\im@segmentfill{\arrowfill@{\mapstochar\relbar}\relbar{\relbar\mkern-3.5mu\mapstochar\mkern3.5mu}}
\providecommand\Overrightarrow{\mathpalette{\overarrow@\im@Rightarrowfill}}
\providecommand\overleftharpoon{\mathpalette{\overarrow@\im@lharpoonfill}}
\providecommand\overrightharpoon{\mathpalette{\overarrow@\im@rharpoonfill}}
\providecommand\overlinesegment{\mathpalette{\overarrow@\im@segmentfill}}
\providecommand\underlinesegment{\mathpalette{\underarrow@\im@segmentfill}}
\def\im@groupfill#1#2{$\m@th\setbox\z@\hbox{$#1$}#1\leaders\vrule\@height\ht\z@\@depth\z@\hfill#2$}
\providecommand\overgroup[1]{\mathop{\vbox{\m@th\ialign{##\crcr\noalign{\kern3\p@}%
  \im@groupfill\braceld\bracerd\crcr\noalign{\kern3\p@\nointerlineskip}%
  $\hfil\displaystyle{#1}\hfil$\crcr}}}\limits}
\providecommand\undergroup[1]{\mathop{\vtop{\m@th\ialign{##\crcr$\hfil\displaystyle{#1}\hfil$\crcr
  \noalign{\kern3\p@\nointerlineskip}\im@groupfill\bracelu\braceru\crcr\noalign{\kern3\p@}}}}\limits}
\newcommand\im@utilde[2]{\vtop{\m@th\ialign{##\crcr$\hfil#1#2\hfil$\crcr
  \noalign{\kern1\p@\nointerlineskip}$\hfil#1\widetilde{\hphantom{#2}}\hfil$\crcr}}}
\providecommand\utilde[1]{\mathpalette\im@utilde{#1}}
\providecommand\xtwoheadrightarrow[2][]{\ext@arrow 0579{\arrowfill@\relbar\relbar\twoheadrightarrow}{#1}{#2}}
\providecommand\xtwoheadleftarrow[2][]{\ext@arrow 5097{\arrowfill@\twoheadleftarrow\relbar\relbar}{#1}{#2}}
\providecommand\xlongequal[2][]{\ext@arrow 0055{\arrowfill@\Relbar\Relbar\Relbar}{#1}{#2}}
\def\im@tofroml{\vcenter{\offinterlineskip\hbox{$\m@th\relbar\mkern-2mu\relbar$}\kern.3ex\hbox{$\m@th\leftarrow$}}}
\def\im@tofromm{\vcenter{\offinterlineskip\hbox{$\m@th\relbar$}\kern.3ex\hbox{$\m@th\relbar$}}}
\def\im@tofromr{\vcenter{\offinterlineskip\hbox{$\m@th\mkern2mu\rightarrow$}\kern.3ex
  \hbox{$\m@th\relbar\mkern-2mu\relbar$}}}
\providecommand\xtofrom[2][]{\ext@arrow 0359{\arrowfill@\im@tofroml\im@tofromm\im@tofromr}{#1}{#2}}
% \Braket{a|b|c}: each top-level | becomes a \middle bar.
\providecommand\Braket[1]{\left\langle\im@bk#1|\im@nil\right\rangle}
\def\im@bk#1|#2\im@nil{#1\if\relax\detokenize{#2}\relax\else\,\middle\vert\,\im@bk#2\im@nil\fi}
% \widecheck from the mathabx font, set as text (a symbol font would spend a
% math family, and the 16 are needed by the alphabets of a single formula):
% the widest of its five sizes that fits, raised as TeX raises an accent.
\providecommand\widecheck[1]{\mathord{\mathchoice{\im@wc\displaystyle{10}{#1}}{\im@wc\textstyle{10}{#1}}%
  {\im@wc\scriptstyle{7}{#1}}{\im@wc\scriptscriptstyle{5}{#1}}}}
\newcommand\im@wc[3]{\setbox0\hbox{$\m@th#1#3$}%
  \setbox2\hbox{\fontsize{#2}{#2}\usefont{U}{mathx}{m}{n}\ifdim\wd0<.8em\char`q\else\ifdim\wd0<1.25em
    \char'174\else\ifdim\wd0<1.7em\char'175\else\ifdim\wd0<2.15em\char'176\else\char'177\fi\fi\fi\fi}%
  \setbox4\hbox{\fontsize{#2}{#2}\usefont{U}{mathx}{m}{n}\vrule height\fontdimen5\font width\z@}%
  \dimen@\ht0\advance\dimen@-\ht4\ifdim\dimen@<\z@\dimen@\z@\fi
  \hbox{\hbox to\wd0{\hss\raise\dimen@\box2\hss}\kern-\wd0\box0}}
% \oiint from the esint font, without esint (which redraws every integral).
\newcommand\im@esg[2]{\vcenter{\hbox{\fontsize{#2}{#2}\usefont{U}{esint}{m}{n}\char#1}}}
\newcommand\im@esint[3]{\mathop{\mathchoice{\im@esg{#1}{10}#3}{\im@esg{#2}{10}#3}{\im@esg{#2}{7}#3}%
  {\im@esg{#2}{5}#3}}\nolimits}
\providecommand\oiint{\im@esint{'016}{'015}{}}
\providecommand\oiiint{\im@esint{'016}{'015}{\mkern-12mu\int}}
% MathJax's upgreek, from the Euler font as text (no math family spent).
\newcommand\im@ugr[1]{\mathord{\text{\usefont{U}{eur}{m}{n}\char#1}}}
\providecommand\upalpha{\im@ugr{"0B}}\providecommand\upbeta{\im@ugr{"0C}}\providecommand\upgamma{\im@ugr{"0D}}
\providecommand\updelta{\im@ugr{"0E}}\providecommand\upepsilon{\im@ugr{"0F}}\providecommand\upzeta{\im@ugr{"10}}
\providecommand\upeta{\im@ugr{"11}}\providecommand\uptheta{\im@ugr{"12}}\providecommand\upiota{\im@ugr{"13}}
\providecommand\upkappa{\im@ugr{"14}}\providecommand\uplambda{\im@ugr{"15}}\providecommand\upmu{\im@ugr{"16}}
\providecommand\upnu{\im@ugr{"17}}\providecommand\upxi{\im@ugr{"18}}\providecommand\uppi{\im@ugr{"19}}
\providecommand\uprho{\im@ugr{"1A}}\providecommand\upsigma{\im@ugr{"1B}}\providecommand\uptau{\im@ugr{"1C}}
\providecommand\upupsilon{\im@ugr{"1D}}\providecommand\upphi{\im@ugr{"1E}}\providecommand\upchi{\im@ugr{"1F}}
\providecommand\uppsi{\im@ugr{"20}}\providecommand\upomega{\im@ugr{"21}}\providecommand\upvarepsilon{\im@ugr{"22}}
\providecommand\upvartheta{\im@ugr{"23}}\providecommand\upvarpi{\im@ugr{"24}}\providecommand\upvarphi{\im@ugr{"27}}
\providecommand\Upgamma{\im@ugr{"00}}\providecommand\Updelta{\im@ugr{"01}}\providecommand\Uptheta{\im@ugr{"02}}
\providecommand\Uplambda{\im@ugr{"03}}\providecommand\Upxi{\im@ugr{"04}}\providecommand\Uppi{\im@ugr{"05}}
\providecommand\Upsigma{\im@ugr{"06}}\providecommand\Upupsilon{\im@ugr{"07}}\providecommand\Upphi{\im@ugr{"08}}
\providecommand\Uppsi{\im@ugr{"09}}\providecommand\Upomega{\im@ugr{"0A}}
% MathJax's physics: the common commands, without the package (which
% would redefine \div, \Re and \Im).
\newcommand\imqty{\@ifnextchar(\im@qtyp{\@ifnextchar[\im@qtyb{\@ifnextchar|\im@qtyv\im@qtyc}}}
\def\im@qtyp(#1){\left(#1\right)}\def\im@qtyb[#1]{\left[#1\right]}
\def\im@qtyv|#1|{\left\lvert#1\right\rvert}\def\im@qtyc#1{\left\{#1\right\}}
\providecommand\pqty[1]{\left(#1\right)}\providecommand\bqty[1]{\left[#1\right]}
\providecommand\Bqty[1]{\left\{#1\right\}}\providecommand\vqty[1]{\left\lvert#1\right\rvert}
\providecommand\eval[1]{\left.#1\right\rvert}\providecommand\order[1]{\mathcal{O}\left(#1\right)}
\providecommand\comm[2]{\left[#1,#2\right]}\providecommand\acomm[2]{\left\{#1,#2\right\}}
\providecommand\pb[2]{\left\{#1,#2\right\}}
\providecommand\expval[1]{\@ifnextchar\bgroup{\im@expval{#1}}{\left\langle#1\right\rangle}}
\newcommand\im@expval[2]{\left\langle#2\middle\vert#1\middle\vert#2\right\rangle}
\providecommand\ev{\expval}
\providecommand\mel[3]{\left\langle#1\middle\vert#2\middle\vert#3\right\rangle}
\providecommand\matrixel{\mel}
\providecommand\ketbra[2]{\left\lvert#1\middle\rangle\!\middle\langle#2\right\rvert}
\providecommand\outerproduct{\ketbra}\providecommand\innerproduct{\braket}
\providecommand\op{\ketbra}\providecommand\ip{\braket}\providecommand\dyad[1]{\ketbra{#1}{#1}}
\providecommand\vu[1]{\hat{\mathbf{#1}}}\providecommand\va[1]{\vec{\mathbf{#1}}}
\providecommand\curl{\nabla\times}\providecommand\divergence{\nabla\cdot}
\providecommand\laplacian{\nabla^2}\providecommand\trace{\operatorname{tr}}
\providecommand\fdv[2]{\frac{\delta#1}{\delta#2}}
\providecommand\Res{\operatorname{Res}}\providecommand\pv{\operatorname{P.V.}}\providecommand\PV{\pv}
\providecommand\erfc{\operatorname{erfc}}\providecommand\sinc{\operatorname{sinc}}
\providecommand\mqty{\@ifnextchar(\im@mqtyp{\@ifnextchar[\im@mqtyb{\im@mqtyc}}}
\def\im@mqtyp(#1){\begin{pmatrix}#1\end{pmatrix}}\def\im@mqtyb[#1]{\begin{bmatrix}#1\end{bmatrix}}
\def\im@mqtyc#1{\begin{matrix}#1\end{matrix}}
\providecommand\pmqty[1]{\begin{pmatrix}#1\end{pmatrix}}\providecommand\bmqty[1]{\begin{bmatrix}#1\end{bmatrix}}
\providecommand\vmqty[1]{\begin{vmatrix}#1\end{vmatrix}}\providecommand\Bmqty[1]{\begin{Bmatrix}#1\end{Bmatrix}}
\providecommand\smqty[1]{\begin{smallmatrix}#1\end{smallmatrix}}
% gensymb, and other MathJax commands.
\providecommand\perthousand{\text{\textperthousand}}
\providecommand\iddots{\mathinner{\mkern1mu\raise1\p@\hbox{.}\mkern2mu\raise4\p@\hbox{.}\mkern2mu
  \raise7\p@\vbox{\kern7\p@\hbox{.}}\mkern1mu}}
\providecommand\Rule[3]{\rule[-#3]{#1}{#2}}
\providecommand\Space[3]{\hbox to#1{\vrule width\z@ height#2 depth#3\hss}}
% mhchem's \pu (KaTeX and MathJax have it, the LaTeX package does not): number, then unit.
\providecommand\pu[1]{\im@pu#1 \im@nil}
\def\im@pu#1 #2\im@nil{\if\relax\detokenize{#2}\relax\mathrm{#1}\else\SI{#1}{#2}\fi}
% KaTeX's display-style array.
\newenvironment{darray}{\everymath{\displaystyle}\array}{\endarray}
% \vcenter with math inside (what prepare() makes of KaTeX's \vcenter{...}).
\newcommand\imvcenter[1]{\mathpalette\im@vcenter{#1}}
\newcommand\im@vcenter[2]{\vcenter{\hbox{$\m@th#1#2$}}}
% A KaTeX size command in math (prepare() moves the rest of its group in;
% an \hbox, as \text would typeset nested sizes 4^depth times).
\newcommand\imsize[2]{\hbox{#1$\m@th#2$}}
% What prepare() makes of commands it drops: their content alone.
\newcommand\imcontent[1]{#1}
\makeatother
'''

# CSS colour names (KaTeX takes them in \color and friends) that xcolor
# lacks; xcolor's own names (red, green...) keep xcolor's values.
CSS_COLOURS = dict(zip(*[iter('''
    aliceblue f0f8ff antiquewhite faebd7 aqua 00ffff aquamarine 7fffd4 azure f0ffff beige f5f5dc
    bisque ffe4c4 black 000000 blanchedalmond ffebcd blue 0000ff blueviolet 8a2be2 brown a52a2a
    burlywood deb887 cadetblue 5f9ea0 chartreuse 7fff00 chocolate d2691e coral ff7f50 cornflowerblue 6495ed
    cornsilk fff8dc crimson dc143c cyan 00ffff darkblue 00008b darkcyan 008b8b darkgoldenrod b8860b
    darkgray a9a9a9 darkgreen 006400 darkgrey a9a9a9 darkkhaki bdb76b darkmagenta 8b008b
    darkolivegreen 556b2f darkorange ff8c00 darkorchid 9932cc darkred 8b0000 darksalmon e9967a
    darkseagreen 8fbc8f darkslateblue 483d8b darkslategray 2f4f4f darkslategrey 2f4f4f darkturquoise 00ced1
    darkviolet 9400d3 deeppink ff1493 deepskyblue 00bfff dimgray 696969 dimgrey 696969 dodgerblue 1e90ff
    firebrick b22222 floralwhite fffaf0 forestgreen 228b22 fuchsia ff00ff gainsboro dcdcdc ghostwhite f8f8ff
    gold ffd700 goldenrod daa520 gray 808080 green 008000 greenyellow adff2f grey 808080 honeydew f0fff0
    hotpink ff69b4 indianred cd5c5c indigo 4b0082 ivory fffff0 khaki f0e68c lavender e6e6fa
    lavenderblush fff0f5 lawngreen 7cfc00 lemonchiffon fffacd lightblue add8e6 lightcoral f08080
    lightcyan e0ffff lightgoldenrodyellow fafad2 lightgray d3d3d3 lightgreen 90ee90 lightgrey d3d3d3
    lightpink ffb6c1 lightsalmon ffa07a lightseagreen 20b2aa lightskyblue 87cefa lightslategray 778899
    lightslategrey 778899 lightsteelblue b0c4de lightyellow ffffe0 lime 00ff00 limegreen 32cd32 linen faf0e6
    magenta ff00ff maroon 800000 mediumaquamarine 66cdaa mediumblue 0000cd mediumorchid ba55d3
    mediumpurple 9370db mediumseagreen 3cb371 mediumslateblue 7b68ee mediumspringgreen 00fa9a
    mediumturquoise 48d1cc mediumvioletred c71585 midnightblue 191970 mintcream f5fffa mistyrose ffe4e1
    moccasin ffe4b5 navajowhite ffdead navy 000080 oldlace fdf5e6 olive 808000 olivedrab 6b8e23 orange ffa500
    orangered ff4500 orchid da70d6 palegoldenrod eee8aa palegreen 98fb98 paleturquoise afeeee
    palevioletred db7093 papayawhip ffefd5 peachpuff ffdab9 peru cd853f pink ffc0cb plum dda0dd
    powderblue b0e0e6 purple 800080 rebeccapurple 663399 red ff0000 rosybrown bc8f8f royalblue 4169e1
    saddlebrown 8b4513 salmon fa8072 sandybrown f4a460 seagreen 2e8b57 seashell fff5ee sienna a0522d
    silver c0c0c0 skyblue 87ceeb slateblue 6a5acd slategray 708090 slategrey 708090 snow fffafa
    springgreen 00ff7f steelblue 4682b4 tan d2b48c teal 008080 thistle d8bfd8 tomato ff6347 turquoise 40e0d0
    violet ee82ee wheat f5deb3 white ffffff whitesmoke f5f5f5 yellow ffff00 yellowgreen 9acd32
'''.split())] * 2))

# Commands models use without defining them. \providecommand keeps any
# package definition (and any user macro) that already exists.
COMPAT = r'''
\providecommand\R{\mathbb{R}}\providecommand\N{\mathbb{N}}
\providecommand\Z{\mathbb{Z}}\providecommand\Q{\mathbb{Q}}
\providecommand\C{\mathbb{C}}\providecommand\E{\mathbb{E}}
% physics' forms (a user's own physics package, loaded before this, wins):
% \dd[3]{x}, \vb*{x} (bold italic).
\ProvideDocumentCommand\dd{o}{\mathop{}\!\mathrm{d}\IfValueT{#1}{^{#1}}}
\ProvideDocumentCommand\vb{s m}{\IfBooleanTF{#1}{\bm{#2}}{\mathbf{#2}}}
\providecommand\vdot{\cdot}\providecommand\cross{\times}\providecommand\cp{\times}
\providecommand\grad{\nabla}
\providecommand\argmax{\operatorname*{arg\,max}}
\providecommand\argmin{\operatorname*{arg\,min}}
\providecommand\sgn{\operatorname{sgn}}\providecommand\sign{\operatorname{sign}}
\providecommand\diag{\operatorname{diag}}\providecommand\rank{\operatorname{rank}}
\providecommand\tr{\operatorname{tr}}\providecommand\Tr{\operatorname{Tr}}
\providecommand\Var{\operatorname{Var}}\providecommand\Cov{\operatorname{Cov}}
\providecommand\supp{\operatorname{supp}}\providecommand\Span{\operatorname{span}}
\providecommand\softmax{\operatorname{softmax}}
\providecommand\erf{\operatorname{erf}}
\providecommand\lt{<}\providecommand\gt{>}
\providecommand\degree{{}^\circ}\providecommand\celsius{{}^\circ\mathrm{C}}
\providecommand\ohm{\Omega}\providecommand\micro{\mu}
\providecommand\euro{\text{\texteuro}}
\providecommand\T{\mathsf{T}}\providecommand\mathbbm{\mathds}
\providecommand\Alpha{\mathrm{A}}\providecommand\Beta{\mathrm{B}}
\providecommand\Epsilon{\mathrm{E}}\providecommand\Zeta{\mathrm{Z}}
\providecommand\Eta{\mathrm{H}}\providecommand\Iota{\mathrm{I}}
\providecommand\Kappa{\mathrm{K}}\providecommand\Mu{\mathrm{M}}
\providecommand\Nu{\mathrm{N}}\providecommand\Omicron{\mathrm{O}}
\providecommand\Rho{\mathrm{P}}\providecommand\Tau{\mathrm{T}}
\providecommand\Chi{\mathrm{X}}\providecommand\omicron{o}
\providecommand\set[1]{\left\{#1\right\}}\providecommand\Set[1]{\left\{\,#1\,\right\}}
% \abs and \norm grow with their content, as physics and MathJax draw them
% (\abs{\frac{a}{b}}); the starred form too (mathtools' meaning of the star),
% and [\big] picks a size. A name a user package defined (physics) is kept.
\ifdefined\abs\else
  \NewDocumentCommand\abs{s o m}{\IfValueTF{#2}{\mathopen{#2\lvert}#3\mathclose{#2\rvert}}%
    {\left\lvert#3\right\rvert}}
\fi
\ifdefined\norm\else
  \NewDocumentCommand\norm{s o m}{\IfValueTF{#2}{\mathopen{#2\lVert}#3\mathclose{#2\rVert}}%
    {\left\lVert#3\right\rVert}}
\fi
\ifdefined\DeclarePairedDelimiter
  \ifdefined\floor\else\DeclarePairedDelimiter\floor{\lfloor}{\rfloor}\fi
  \ifdefined\ceil\else\DeclarePairedDelimiter\ceil{\lceil}{\rceil}\fi
\fi
\providecommand\bra[1]{\left\langle #1\right\rvert}
\providecommand\ket[1]{\left\lvert #1\right\rangle}
\makeatletter
\providecommand\braket[1]{\@ifnextchar\bgroup{\im@braket{#1}}{\left\langle #1\right\rangle}}
\newcommand\im@braket[2]{\left\langle #1\middle\vert #2\right\rangle}
\newcommand\im@order[1]{\if\relax\detokenize{#1}\relax\else^{#1}\fi}
% \dv[n]{x}, \dv[n]{f}{x}; \pdv{f}{x}{y} is the mixed derivative.
\newcommand\im@dv[4]{\@ifnextchar\bgroup{#4{#1}{#2}{#3}}%
    {\frac{#1\im@order{#2}}{#1#3\im@order{#2}}}}
\newcommand\im@dvof[4]{\frac{#1\im@order{#2}#3}{#1#4\im@order{#2}}}
\newcommand\im@pdvof[4]{\@ifnextchar\bgroup{\im@pdvmix{#1}{#3}{#4}}{\im@dvof{#1}{#2}{#3}{#4}}}
\newcommand\im@pdvmix[4]{\frac{#1^{2}#2}{#1#3\,#1#4}}
\providecommand\dv[2][]{\im@dv{\mathrm{d}}{#1}{#2}\im@dvof}
\providecommand\pdv[2][]{\im@dv{\partial}{#1}{#2}\im@pdvof}
\makeatother
''' + COMPAT_KATEX

# Control sequences that read or write files, run programs, define or
# redefine commands, change catcodes, build command names at run time, make
# global changes that would reach the next formula of a batch, or leave the
# item's box. Names with a prefix in UNSAFE_PREFIXES are rejected as well
# (\UseName, \ExpandArgs, \NewCommandCopy, \AddToHook, \AtEndDocument...),
# and \begin/\end take only the environments in SAFE_ENVS, since \begin{x}
# runs the command named x.
UNSAFE = set('''
input include includeonly endinput InputIfFileExists IfFileExists openin openout
closein closeout read readline write immediate newread newwrite typein typeout
message errmessage special shipout output font
def edef gdef xdef let futurelet global long outer protected
newcommand renewcommand providecommand DeclareRobustCommand
NewDocumentCommand RenewDocumentCommand ProvideDocumentCommand
DeclareDocumentCommand NewExpandableDocumentCommand newenvironment
renewenvironment DeclareMathOperator DeclarePairedDelimiter
chardef mathchardef countdef dimendef skipdef muskipdef toksdef
catcode lccode uccode sfcode mathcode delcode
csname endcsname expandafter scantokens uppercase lowercase
afterassignment aftergroup loop
usepackage RequirePackage documentclass makeatletter makeatother
ExplSyntaxOn ExplSyntaxOff directlua latelua
preview endpreview CJK endCJK document enddocument CJKfamily
batchmode nonstopmode scrollmode errorstopmode interactionmode dump
fontdimen hyphenchar skewchar textfont scriptfont scriptscriptfont
endlinechar newlinechar escapechar romannumeral
includegraphics graphicspath verbatiminput lstinputlisting
ifcsname primitive xglobal pagecolor nopagecolor MakeRobust
setcounter addtocounter stepcounter refstepcounter
egroup endgroup stop vadjust insert mark marks
'''.split())
UNSAFE_PREFIXES = ('pdf', 'pgf', 'tikz', 'every', 'tracing', 'show', 'Show', 'global', 'new', 'renew',
                   'provide', 'New', 'Renew', 'Provide', 'Declare', 'Use', 'Expand', 'AddTo', 'RemoveFrom',
                   'At', 'Activate', 'Disable', 'Clear', 'Copy', 'Debug')
SAFE_NAMES = {'newline', 'newton'}  # start like an unsafe prefix, but are not
SAFE_ENVS = set('''
matrix pmatrix bmatrix Bmatrix vmatrix Vmatrix smallmatrix
matrix* pmatrix* bmatrix* Bmatrix* vmatrix* Vmatrix* smallmatrix*
psmallmatrix bsmallmatrix Bsmallmatrix vsmallmatrix Vsmallmatrix
psmallmatrix* bsmallmatrix* Bsmallmatrix* vsmallmatrix* Vsmallmatrix*
cases cases* dcases dcases* rcases rcases* drcases drcases*
aligned alignedat gathered split multlined lgathered rgathered
array subarray tabular darray CD empheq numcases subnumcases tikzcd
align align* gather gather* flalign flalign* alignat alignat* multline multline*
eqnarray eqnarray* equation equation* displaymath math
'''.split())

# Environments that only work as a whole equation, and the inner forms that
# work inside $...$.
WHOLE_ENVS = {
    'align': 'aligned', 'align*': 'aligned', 'flalign': 'aligned', 'flalign*': 'aligned',
    'eqnarray': 'aligned', 'eqnarray*': 'aligned', 'split': 'aligned',
    'gather': 'gathered', 'gather*': 'gathered',
    'multline': 'multlined', 'multline*': 'multlined',
    'alignat': 'alignedat', 'alignat*': 'alignedat',
    'equation': None, 'equation*': None, 'displaymath': None, 'math': None,
    'numcases': 'cases', 'subnumcases': 'cases', 'empheq': 'empheq',
}

TEXT_COMMANDS = {
    'text', 'textrm', 'textit', 'textbf', 'textsf', 'texttt', 'textnormal', 'textup',
    'textsl', 'textmd', 'emph', 'mbox', 'fbox', 'intertext', 'shortintertext',
}

# Unicode a model may write inside a formula, as TeX math.
UNICODE_MATH = {
    'α': r'\alpha', 'β': r'\beta', 'γ': r'\gamma', 'δ': r'\delta', 'ε': r'\varepsilon',
    'ϵ': r'\epsilon', 'ζ': r'\zeta', 'η': r'\eta', 'θ': r'\theta', 'ϑ': r'\vartheta',
    'ι': r'\iota', 'κ': r'\kappa', 'λ': r'\lambda', 'μ': r'\mu', 'µ': r'\mu', 'ν': r'\nu',
    'ξ': r'\xi', 'ο': 'o', 'π': r'\pi', 'ϖ': r'\varpi', 'ρ': r'\rho', 'ϱ': r'\varrho',
    'σ': r'\sigma', 'ς': r'\varsigma', 'τ': r'\tau', 'υ': r'\upsilon', 'φ': r'\varphi',
    'ϕ': r'\phi', 'χ': r'\chi', 'ψ': r'\psi', 'ω': r'\omega',
    'Α': 'A', 'Β': 'B', 'Γ': r'\Gamma', 'Δ': r'\Delta', 'Ε': 'E', 'Ζ': 'Z', 'Η': 'H',
    'Θ': r'\Theta', 'Ι': 'I', 'Κ': 'K', 'Λ': r'\Lambda', 'Μ': 'M', 'Ν': 'N', 'Ξ': r'\Xi',
    'Ο': 'O', 'Π': r'\Pi', 'Ρ': 'P', 'Σ': r'\Sigma', 'Τ': 'T', 'Υ': r'\Upsilon',
    'Φ': r'\Phi', 'Χ': 'X', 'Ψ': r'\Psi', 'Ω': r'\Omega',
    '−': '-', '–': '-', '±': r'\pm', '∓': r'\mp', '×': r'\times', '÷': r'\div',
    '·': r'\cdot', '⋅': r'\cdot', '∙': r'\cdot', '∘': r'\circ', '…': r'\ldots',
    '⋯': r'\cdots', '⋮': r'\vdots', '⋱': r'\ddots', '∗': '*', '⋆': r'\star',
    '≤': r'\le', '≥': r'\ge', '⩽': r'\leqslant', '⩾': r'\geqslant', '≠': r'\ne',
    '≈': r'\approx', '≡': r'\equiv', '∼': r'\sim', '≃': r'\simeq', '≅': r'\cong',
    '∝': r'\propto', '≪': r'\ll', '≫': r'\gg', '≔': r'\coloneqq', '≜': r'\triangleq',
    '→': r'\to', '←': r'\leftarrow', '↔': r'\leftrightarrow', '⇒': r'\Rightarrow',
    '⇐': r'\Leftarrow', '⇔': r'\Leftrightarrow', '↦': r'\mapsto', '⟶': r'\longrightarrow',
    '⟹': r'\Longrightarrow', '⟺': r'\Longleftrightarrow', '↑': r'\uparrow', '↓': r'\downarrow',
    '∈': r'\in', '∉': r'\notin', '∋': r'\ni', '⊂': r'\subset', '⊆': r'\subseteq',
    '⊃': r'\supset', '⊇': r'\supseteq', '⊊': r'\subsetneq', '∪': r'\cup', '∩': r'\cap',
    '∖': r'\setminus', '∅': r'\emptyset', '∀': r'\forall', '∃': r'\exists',
    '∄': r'\nexists', '¬': r'\neg', '∧': r'\wedge', '∨': r'\vee', '⊕': r'\oplus',
    '⊗': r'\otimes', '⊥': r'\perp', '∥': r'\parallel', '‖': r'\|', '∠': r'\angle',
    '∑': r'\sum', '∏': r'\prod', '∐': r'\coprod', '∫': r'\int', '∬': r'\iint',
    '∭': r'\iiint', '∮': r'\oint', '∂': r'\partial', '∇': r'\nabla', '√': r'\surd',
    '∞': r'\infty', 'ℓ': r'\ell', 'ℏ': r'\hbar', 'ħ': r'\hbar', 'ℵ': r'\aleph',
    '℘': r'\wp', 'ℜ': r'\Re', 'ℑ': r'\Im', '⟨': r'\langle', '⟩': r'\rangle',
    '⌊': r'\lfloor', '⌋': r'\rfloor', '⌈': r'\lceil', '⌉': r'\rceil',
    'ℝ': r'\mathbb{R}', 'ℕ': r'\mathbb{N}', 'ℤ': r'\mathbb{Z}', 'ℚ': r'\mathbb{Q}',
    'ℂ': r'\mathbb{C}', 'ℙ': r'\mathbb{P}', '𝔼': r'\mathbb{E}',
    '′': "'", '″': "''", '°': r'{}^\circ', '\u00a0': '~', '\u2009': r'\,', '\u202f': r'\,',
    '†': r'\dagger', '‡': r'\ddagger', '□': r'\square', '∎': r'\blacksquare',
}
COMBINING = {
    '\u0300': r'\grave', '\u0301': r'\acute', '\u0302': r'\hat', '\u0303': r'\tilde',
    '\u0304': r'\bar', '\u0305': r'\overline', '\u0306': r'\breve', '\u0307': r'\dot',
    '\u0308': r'\ddot', '\u030c': r'\check', '\u20d7': r'\vec',
}
SUPERSCRIPTS = dict(zip('⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ', '0123456789+-=()ni'))
SUBSCRIPTS = dict(zip('₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓᵢⱼₖₙ', '0123456789+-=()aeoxijkn'))
CJK = re.compile('[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]')
HANGUL = re.compile('[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]')
KANA = re.compile('[\u3040-\u30ff\u31f0-\u31ff]')
# UTF8 font families of the CJK package: Simplified Chinese (the default),
# Traditional Chinese, Japanese, Korean. One that is not installed is not used.
CJK_FONTS = ('gbsn', 'bsmi', 'ipxm', 'mj')


class Unsafe(ValueError):
    """A formula that is refused before TeX sees it."""


# ---------------------------------------------------------------- options

def parse_options(raw):
    """Read request options, with defaults for missing or bad values."""
    raw = raw if isinstance(raw, dict) else {}

    def number(name, default, low, high):
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            return default
        return float(value)

    cache_dir = raw.get('cacheDir')
    packages, bad = [], []
    for name in raw.get('packages') if isinstance(raw.get('packages'), list) else []:
        if isinstance(name, str) and USER_PACKAGE.fullmatch(name.strip()) and len(packages) < MAX_USER_PACKAGES:
            if name.strip() not in packages:
                packages.append(name.strip())
        else:
            bad.append(str(name)[:40])
    macros = raw.get('macros')
    macros = macros if isinstance(macros, str) and len(macros) <= MAX_MACROS else ''
    return {
        'fg': raw.get('fg', 'auto'),
        'cell': raw.get('cell', 'auto'),
        'baseline': number('baseline', 0.773, 0.05, 0.95),
        'ptPx': number('ptPx', 3.4, 0.5, 20),
        'minShrink': number('minShrink', 0.8, MIN_SCALE, 1),
        'maxColumns': int(number('maxColumns', MAX_BOX, 1, MAX_BOX)),
        'cacheDir': cache_dir if isinstance(cache_dir, str) and cache_dir else default_cache_dir(),
        'packages': packages,
        'badPackages': bad,
        'macros': macros.strip(),
    }


def default_cache_dir():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'claude-inline-math')


def quiet_run(argv, timeout=DETECT_TIMEOUT):
    """Run a small helper program; its stdout, or '' when it fails."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ''
    return done.stdout if done.returncode == 0 else ''


def parse_cell(spec):
    found = re.fullmatch(r'\s*(\d+)x(\d+)\s*', spec or '')
    if not found:
        return None
    w, h = int(found.group(1)), int(found.group(2))
    return (w, h) if 2 <= w <= 400 and 4 <= h <= 800 else None


def tty_cell():
    """Cell size from the pixel size the terminal reports for /dev/tty."""
    try:
        import fcntl
        import struct
        import termios
        fd = os.open('/dev/tty', os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except (ImportError, OSError):
        return None
    try:
        rows, cols, xpix, ypix = struct.unpack('HHHH', fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\0' * 8))
    except OSError:
        return None
    finally:
        os.close(fd)
    if rows and cols and xpix and ypix:
        return parse_cell(f'{xpix // cols}x{ypix // rows}')
    return None


def detect_cell(spec):
    """Cell size in pixels: the option, then tmux, then the tty, then 19x42."""
    cell = parse_cell(spec) if spec != 'auto' else None
    if not cell and os.environ.get('TMUX'):
        cell = parse_cell(quiet_run(['tmux', 'display', '-p', '#{client_cell_width}x#{client_cell_height}']))
    return cell or tty_cell() or DEFAULT_CELL


def detect_fg(spec):
    """Text colour: the option, else from the desktop's light or dark scheme."""
    if isinstance(spec, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', spec):
        return spec.lower()
    portal = quiet_run([
        'gdbus', 'call', '--session', '--timeout', '1',
        '--dest', 'org.freedesktop.portal.Desktop',
        '--object-path', '/org/freedesktop/portal/desktop',
        '--method', 'org.freedesktop.portal.Settings.ReadOne',
        'org.freedesktop.appearance', 'color-scheme',
    ])
    found = re.search(r'uint32 (\d)', portal)
    if found:  # 0 no preference (light), 1 dark, 2 light
        return DARK_FG if found.group(1) == '1' else LIGHT_FG
    scheme = quiet_run(['gsettings', 'get', 'org.gnome.desktop.interface', 'color-scheme'])
    if scheme:
        return DARK_FG if 'dark' in scheme else LIGHT_FG
    return DARK_FG  # no desktop to ask: most terminals are dark


# ---------------------------------------------------------------- input

def tokens(latex):
    """Split LaTeX into (kind, value) tokens: 'cs' for control sequences, 'char' otherwise."""
    out, i, n = [], 0, len(latex)
    while i < n:
        c = latex[i]
        if c == '\\' and i + 1 < n:
            j = i + 1
            if latex[j].isascii() and latex[j].isalpha():
                while j < n and latex[j].isascii() and latex[j].isalpha():
                    j += 1
                out.append(('cs', latex[i + 1:j]))
                i = j
            else:
                out.append(('cs', latex[j]))
                i = j + 1
        else:
            out.append(('char', c))
            i += 1
    return out


def strip_comments(latex):
    """Drop TeX comments as TeX does: from an unescaped % through the line
    break and the next line's leading blanks. The checks then see the tokens
    TeX sees (no \\end{pre%<newline>view})."""
    out, i, n = [], 0, len(latex)
    while i < n:
        c = latex[i]
        if c == '\\' and i + 1 < n:
            out.append(latex[i:i + 2])
            i += 2
        elif c == '%':
            end = latex.find('\n', i)
            if end < 0:
                break
            i = end + 1
            while i < n and latex[i] in ' \t':
                i += 1
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def is_unsafe_name(name):
    return name not in SAFE_NAMES and (name in UNSAFE or name.startswith(UNSAFE_PREFIXES))


def check_safe(toks, latex):
    """Raise Unsafe for anything that could reach outside the formula's box."""
    if '^^' in latex:
        raise Unsafe('unsafe: ^^ character codes')
    depth = 0
    for k, (kind, value) in enumerate(toks):
        if kind == 'cs':
            if is_unsafe_name(value):
                raise Unsafe(f'unsafe: \\{value}')
            if value in ('(', ')', '[', ']'):
                raise Unsafe(f'math delimiter \\{value} inside a formula')
            if value in ('begin', 'end'):
                name = env_name(toks, k + 1)
                if name not in SAFE_ENVS:
                    raise Unsafe(f'unsupported environment: \\{value}{{{name or "?"}}}')
        elif value == '$':
            raise Unsafe('unescaped $ inside a formula')
        elif value == '{':
            depth += 1
        elif value == '}':
            depth -= 1
            if depth < 0:
                raise Unsafe('unbalanced braces')
    if depth:
        raise Unsafe('unbalanced braces')


def env_name(toks, k):
    """The {name} argument starting at token k, or None."""
    while k < len(toks) and toks[k] == ('char', ' '):
        k += 1
    if k >= len(toks) or toks[k] != ('char', '{'):
        return None
    name = []
    for kind, value in toks[k + 1:]:
        if (kind, value) == ('char', '}'):
            return ''.join(name).strip()
        if kind == 'cs':
            return None
        name.append(value)
    return None


# What may follow a whole environment and still leave it whole: the sentence
# punctuation layout.ts appends to a display formula ("\\,{.}", "\\text{。}").
TRAILING_PUNCT = re.compile(r'\s*(?:\\,\{[^{}]*\}|\\text\{[^{}]*\})?\s*')


def whole_environment(latex):
    """(name, body, tail) when all of latex is one \\begin{name}...\\end{name}
    (then perhaps trailing punctuation, the tail), else None."""
    start = re.match(r'\s*\\begin\s*\{([A-Za-z]+\*?)\}', latex)
    if not start:
        return None
    name = start.group(1)
    edge = re.compile(r'\\(begin|end)\s*\{' + re.escape(name) + r'\}')
    depth = 0
    for found in edge.finditer(latex, start.start()):
        depth += 1 if found.group(1) == 'begin' else -1
        if depth == 0:
            tail = latex[found.end():]
            if not TRAILING_PUNCT.fullmatch(tail):
                return None
            return name, latex[start.end():found.start()], tail.strip()
    return None


def unwrap_environment(latex):
    """Turn a whole align/gather/equation (also nested, as equation > split)
    into its inner form for $...$."""
    for _ in range(4):
        unwrapped = unwrap_once(latex)
        if unwrapped == latex:
            break
        latex = unwrapped
    return latex


EMPHEQ_DELIMS = {'lbrace': r'\{', 'rbrace': r'\}', 'lbrack': '[', 'rbrack': ']', 'langle': r'\langle',
                 'rangle': r'\rangle', 'lparen': '(', 'rparen': ')', 'lvert': r'\lvert', 'rvert': r'\rvert',
                 'lVert': r'\lVert', 'rVert': r'\rVert'}


def empheq_side(options, side):
    """What empheq's left= or right= option puts beside the equations, as \\left/\\right."""
    found = re.search(side + r'\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|[^,]*)', options)
    value = found.group(1).strip() if found else ''
    if value.startswith('{') and value.endswith('}'):
        value = value[1:-1]
    word = 'left' if side == 'left' else 'right'
    value, count = re.subn(r'\\empheq(?:big)?([lr][A-Za-z]+)',
                           lambda m: '\\' + word + EMPHEQ_DELIMS.get(m.group(1), '.'), value)
    return value if count else (value + r'\left.' if side == 'left' else r'\right.' + value)


def unwrap_once(latex):
    whole = whole_environment(latex)
    if not whole or whole[0] not in WHOLE_ENVS:
        return latex
    name, body, tail = whole
    inner = WHOLE_ENVS[name]
    if inner is None:
        return body.strip() + tail
    if inner == 'cases':  # numcases{lhs}: lhs, then the cases
        lhs = re.match(r'\s*\{((?:[^{}]|\{[^{}]*\})*)\}', body)
        if not lhs:
            return latex
        return lhs.group(1) + rf'\begin{{cases}}{body[lhs.end():]}\end{{cases}}' + tail
    if inner == 'empheq':  # empheq[options]{align}: the inner form, with its delimiters or box
        head = re.match(r'\s*(?:\[((?:[^\[\]]|\{[^{}]*\})*)\])?\s*\{([A-Za-z]+\*?)\}', body)
        if not head or head.group(2) not in WHOLE_ENVS or WHOLE_ENVS[head.group(2)] in ('cases', 'empheq'):
            return latex
        options = head.group(1) or ''
        inside = unwrap_once(rf'\begin{{{head.group(2)}}}{body[head.end():]}\end{{{head.group(2)}}}')
        if 'box' in options:
            inside = r'\boxed{' + inside + '}'
        if 'left' in options or 'right' in options:
            inside = empheq_side(options, 'left') + inside + empheq_side(options, 'right')
        return inside + tail
    body = interjections(body) if inner in ('aligned', 'alignedat') else body
    if isinstance(body, tuple):  # \intertext: blocks one under the other
        blocks = [rf'\begin{{{inner}}}{part}\end{{{inner}}}' if k % 2 == 0 else rf'\text{{{part}}}'
                  for k, part in enumerate(body)]
        return r'\begin{array}{@{}l@{}}' + r'\\'.join(blocks) + r'\end{array}' + tail
    if inner == 'alignedat':
        count = re.match(r'\s*(\{\d+\})', body)
        if not count:
            return latex
        return rf'\begin{{alignedat}}{count.group(1)}{body[count.end():]}\end{{alignedat}}' + tail
    return rf'\begin{{{inner}}}{body}\end{{{inner}}}' + tail


def interjections(body):
    """body, or (lines, text, lines, ...) split at its \\intertext{text}
    and \\shortintertext{text} (which only work in a whole align)."""
    parts, start = [], 0
    for found in re.finditer(r'(?:\\\\\s*)?\\(?:short)?intertext\s*\{((?:[^{}]|\{[^{}]*\})*)\}\s*', body):
        parts += [body[start:found.start()], found.group(1)]
        start = found.end()
    if not parts:
        return body
    return tuple(parts + [body[start:]])


def strip_numbering(latex):
    """Drop \\label, \\nonumber, \\notag; draw \\tag{x} as a plain (x)."""
    latex = re.sub(r'\\label\s*\{[^{}]*\}', '', latex)
    latex = re.sub(r'\\(nonumber|notag)(?![A-Za-z])', '', latex)
    latex = re.sub(r'\\tag\*\s*\{([^{}]*)\}', r'\\qquad\\text{\1}', latex)
    return re.sub(r'\\tag\s*\{([^{}]*)\}', r'\\qquad\\text{(\1)}', latex)


def stack_lines(latex, toks):
    """A display formula with top-level \\\\ becomes gathered (or aligned with &)."""
    depth, envs, breaks, amps = 0, 0, False, False
    for k, (kind, value) in enumerate(toks):
        if kind == 'char' and value == '{':
            depth += 1
        elif kind == 'char' and value == '}':
            depth -= 1
        elif kind == 'cs' and value == 'begin':
            envs += 1
        elif kind == 'cs' and value == 'end':
            envs -= 1
        elif depth == 0 and envs == 0:
            breaks |= (kind, value) == ('cs', '\\')
            amps |= (kind, value) == ('char', '&')
    if not breaks:
        return latex
    inner = 'aligned' if amps else 'gathered'
    return rf'\begin{{{inner}}}{latex}\end{{{inner}}}'


def unicode_tex(c):
    """TeX for one non-ASCII character in math, or None (then it is text)."""
    return UNICODE_MATH.get(c) or unimath.lookup(c)


def map_unicode(toks):
    """Rewrite Unicode symbols as TeX; bare CJK runs in math go into \\text{};
    straight double quotes in text pair up as `` and '' (OT1 has no straight one)."""
    out, stack, i = [], [], 0
    opens = text_groups(toks)
    typewriter = {j for j in opens if j > 0 and toks[j - 1] == ('cs', 'texttt')}
    tt = []  # per {group}: inside \texttt? (there a quote is a straight one already)
    quoted = False
    while i < len(toks):
        kind, value = toks[i]
        in_text = bool(stack) and stack[-1]
        if (kind, value) == ('char', '"') and in_text and not (tt and tt[-1]):
            # A lone quote with none after it closes (5" screen), as KaTeX draws it.
            opens_one = not quoted and ('char', '"') in toks[i + 1:]
            out.append('``' if opens_one else "''")
            quoted = opens_one
            i += 1
            continue
        accent = toks[i + 1][1] if i + 1 < len(toks) and toks[i + 1][0] == 'char' else None
        if kind == 'char' and accent in COMBINING and value.isascii() and value.isalnum() and not in_text:
            out.append(COMBINING[accent] + '{' + value + '}')
            i += 2
            continue
        if kind == 'cs':
            out.append('\\' + value + (' ' if value.isalpha() else ''))
            i += 1
            continue
        if value == '{':
            # A group in text is text, but for \ensuremath{...} (what text_math makes of $...$).
            math_again = i > 0 and toks[i - 1] == ('cs', 'ensuremath')
            stack.append(i in opens or (in_text and not math_again))
            tt.append(i in typewriter or (bool(tt) and tt[-1]))
        elif value == '}' and stack:
            stack.pop()
            tt.pop()
        if value in SUPERSCRIPTS or value in SUBSCRIPTS:
            table, mark = (SUPERSCRIPTS, '^') if value in SUPERSCRIPTS else (SUBSCRIPTS, '_')
            run = []
            while i < len(toks) and toks[i][0] == 'char' and toks[i][1] in table:
                run.append(table[toks[i][1]])
                i += 1
            script = mark + '{' + ''.join(run) + '}'
            out.append(rf'\ensuremath{{{script}}}' if in_text else script)
            continue
        tex = unicode_tex(value) if not value.isascii() else None
        if tex is not None:
            if in_text:
                # Anything TeX reads as math (\circ, the ^ of °) goes in \ensuremath.
                out.append(rf'\ensuremath{{{tex}}}' if any(c in tex for c in '\\^_') else tex)
            else:
                out.append(tex + ' ')
        elif not in_text and not value.isascii():
            run = []
            while (i < len(toks) and toks[i][0] == 'char' and not toks[i][1].isascii()
                          and unicode_tex(toks[i][1]) is None and toks[i][1] not in SUPERSCRIPTS
                          and toks[i][1] not in SUBSCRIPTS):
                run.append(toks[i][1])
                i += 1
            out.append(r'\text{' + ''.join(run) + '}')
            continue
        else:
            out.append(value)
        i += 1
    return ''.join(out)


# ---------------------------------------------------------------- KaTeX syntax
# What KaTeX and MathJax accept and TeX does not, rewritten before the
# safety check (which then sees the result). Commands of the web are
# replaced by their content here, so their other arguments (a URL, CSS, an
# HTML class) never reach TeX at all.

URL_SAFE = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:;/?=+@!*()-\'')
# name: arguments before the content, which is kept; None drops all (one argument).
WEB_COMMANDS = {
    'href': 1, 'htmlClass': 1, 'htmlId': 1, 'htmlStyle': 1, 'htmlData': 1,
    'class': 1, 'cssId': 1, 'style': 1, 'data': 1, 'mmlToken': 1, 'mathtip': 0, 'texttip': 0,
    'require': None,
}


def brace_group(text, i):
    """(content, end) of the {...} group at text[i] (blanks before it
    skipped), counting only unescaped braces; None when there is none."""
    n = len(text)
    while i < n and text[i] in ' \t\n':
        i += 1
    if i >= n or text[i] != '{':
        return None
    depth, j = 0, i
    while j < n:
        c = text[j]
        if c == '\\':
            j += 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return None


def literal_text(raw, visible_space=False):
    """raw shown as typed, in a typewriter font: every TeX special as its character code."""
    out = []
    for c in raw:
        if c == ' ' and visible_space:
            out.append(r'\char32 ')
        elif c in URL_SAFE:
            out.append(c)
        elif c in '\n\t':
            out.append(' ')
        elif c.isascii():
            out.append(rf'\char{ord(c)} ')
        else:
            out.append('?')
    return r'\text{\texttt{' + ''.join(out) + '}}'


ENCLOSE = {  # MathJax \\enclose notations, as what this preamble can draw
    'box': r'\boxed', 'roundedbox': r'\boxed', 'circle': r'\boxed', 'updiagonalstrike': r'\cancel',
    'downdiagonalstrike': r'\bcancel', 'top': r'\overline', 'bottom': r'\underline', 'radical': r'\sqrt',
    'actuarial': r'\angl', 'phasorangle': r'\phase',
}


def bracket_option(text, i):
    """(content, end) of an [option] at text[i] (blanks before it skipped), or ('', i)."""
    j = i
    while j < len(text) and text[j] in ' \t\n':
        j += 1
    if j < len(text) and text[j] == '[':
        close = text.find(']', j)
        if close > 0:
            return text[j + 1:close], close + 1
    return '', i


def mathjax_command(name, latex, i):
    """(TeX, end) for MathJax's \\bbox, \\enclose, \\unicode and \\toggle at latex[i]
    (after the name), or None. Their options (CSS, HTML attributes) are dropped."""
    if name == 'bbox':
        options, j = bracket_option(latex, i)
        group = brace_group(latex, j)
        if not group:
            return None
        parts = [part.strip() for part in options.split(',')]
        background = next((colour_hex(part) for part in parts if colour_hex(part)), None)
        border = next((part for part in parts if part.startswith('border')), None)
        frame = next((colour_hex(word) for word in (border or '').replace(':', ' ').split() if colour_hex(word)),
                     None)
        body = '$' + group[0] + '$'
        if background and border:
            return rf'\fcolorbox{{#{frame or "000000"}}}{{#{background}}}{{{body}}}', group[1]
        if background:
            return rf'\colorbox{{#{background}}}{{{body}}}', group[1]
        return (rf'\fbox{{{body}}}' if border else '{' + group[0] + '}'), group[1]
    if name == 'enclose':
        notation = brace_group(latex, i)
        if not notation:
            return None
        _, j = bracket_option(latex, notation[1])
        group = brace_group(latex, j)
        if not group:
            return None
        words = notation[0].replace(',', ' ').split()
        if 'updiagonalstrike' in words and 'downdiagonalstrike' in words:
            return r'\xcancel{' + group[0] + '}', group[1]
        command = next((ENCLOSE[word] for word in words if word in ENCLOSE), '')
        return command + '{' + group[0] + '}', group[1]
    if name == 'unicode':
        _, j = bracket_option(latex, i)
        group = brace_group(latex, j)
        found = group and re.fullmatch(r'\s*(?:x([0-9A-Fa-f]{1,6})|(\d{1,7}))\s*', group[0])
        if not found:
            return None
        code = int(found.group(1), 16) if found.group(1) else int(found.group(2))
        if not 0x20 <= code <= 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            return None
        return (chr(code) if code > 0x7F else literal_text(chr(code))), group[1]
    if name == 'toggle':
        first = brace_group(latex, i)
        close = latex.find(r'\endtoggle', i)
        if not first or close < first[1]:
            return None
        return '{' + first[0] + '}', close + len(r'\endtoggle')
    return None


def verbatim_arguments(latex):
    """\\url, \\verb and the web commands: done on the raw text, as their
    arguments may hold % # _ and other characters TeX would take as code."""
    if '\\' not in latex:
        return latex
    out, i, n = [], 0, len(latex)
    while i < n:
        found = re.compile(r'\\([A-Za-z]+)').match(latex, i) if latex[i] == '\\' else None
        if not found:
            out.append(latex[i:i + 2] if latex[i] == '\\' else latex[i])
            i += 2 if latex[i] == '\\' else 1
            continue
        name, end = found.group(1), found.end()
        if name == 'verb' and end < n:
            star = end < n and latex[end] == '*'
            start = end + 1 if star else end
            if start < n and not latex[start].isalpha() and latex[start] not in ' {':
                close = latex.find(latex[start], start + 1)
                if close > 0:
                    out.append(literal_text(latex[start + 1:close].replace(' ', '␣' if star else ' ')))
                    i = close + 1
                    continue
        elif name == 'url':
            group = brace_group(latex, end)
            if group:
                out.append(literal_text(group[0]))
                i = group[1]
                continue
        elif name in ('bbox', 'enclose', 'unicode', 'toggle'):
            done = mathjax_command(name, latex, end)
            if done:
                out.append(done[0])
                i = done[1]
                continue
        elif name in WEB_COMMANDS:
            groups, j = [], end
            for _ in range(1 if WEB_COMMANDS[name] is None else 2):
                group = brace_group(latex, j)
                if not group:
                    break
                groups.append(group[0])
                j = group[1]
            keep = WEB_COMMANDS[name]
            if len(groups) == (1 if keep is None else 2):
                out.append('' if keep is None else '{' + groups[keep] + '}')
                i = j
                continue
        out.append(latex[i:end])
        i = end
    return ''.join(out)


# Commands whose argument is text (with the number of {arguments} before
# it; [optional] ones are skipped): there a $...$ or \(...\) is math again.
TEXT_ARGS = dict({name: 0 for name in TEXT_COMMANDS}, hbox=0, vbox=0, reflectbox=0, makebox=0, framebox=0,
                 colorbox=1, fcolorbox=2, raisebox=1, parbox=1)


def skip_arguments(toks, k, count):
    """Index of the token after `count` {arguments} (and any [options] or blanks) from k; None if they are not there."""
    n = len(toks)
    while True:
        while k < n and toks[k] == ('char', ' '):
            k += 1
        if k < n and toks[k] == ('char', '['):
            while k < n and toks[k] != ('char', ']'):
                k += 1
            k += 1
            continue
        if count == 0:
            return k
        if k >= n or toks[k] != ('char', '{'):
            return None
        k = group_end(toks, k) + 1
        count -= 1


def group_end(toks, k):
    """Index of the } that closes the { at toks[k] (len(toks) when it is not closed)."""
    depth = 0
    for j in range(k, len(toks)):
        if toks[j] == ('char', '{'):
            depth += 1
        elif toks[j] == ('char', '}'):
            depth -= 1
            if depth == 0:
                return j
    return len(toks)


def text_groups(toks):
    """Indices of the { tokens that open a text argument."""
    found = set()
    for k, (kind, value) in enumerate(toks):
        if kind == 'cs' and value in TEXT_ARGS:
            j = skip_arguments(toks, k + 1, TEXT_ARGS[value])
            if j is not None and j < len(toks) and toks[j] == ('char', '{'):
                found.add(j)
    return found


def text_math(toks):
    """$...$ and \\(...\\) inside a text argument become \\ensuremath{...}.
    One left anywhere else stays, for check_safe to refuse."""
    if not any(t in (('char', '$'), ('cs', '('), ('cs', ')')) for t in toks):
        return toks
    opens = text_groups(toks)
    out, stack = [], []  # stack: 'text' | 'math' per {group}, '$' or '(' for math inside text
    for k, tok in enumerate(toks):
        in_text = bool(stack) and stack[-1] == 'text'
        if tok == ('char', '{'):
            stack.append('text' if k in opens or in_text else 'math')
        elif tok == ('char', '}'):
            if stack and stack[-1] in ('$', '('):
                raise Unsafe('unbalanced $ inside a text argument')
            if stack:
                stack.pop()
        elif tok in (('char', '$'), ('cs', '(')) and in_text:
            stack.append('$' if tok[1] == '$' else '(')
            out += [('cs', 'ensuremath'), ('char', '{')]
            continue
        elif tok in (('char', '$'), ('cs', ')')) and stack and stack[-1] == ('$' if tok[1] == '$' else '('):
            stack.pop()
            out.append(('char', '}'))
            continue
        out.append(tok)
    if any(frame in ('$', '(') for frame in stack):
        raise Unsafe('unbalanced $ inside a text argument')
    return out


# KaTeX names that LaTeX has for something else (and must keep: the kernel
# and packages use them), or drawings dvipng cannot make (mirrored, scaled,
# rotated boxes): renamed. name: (new name, {arguments} dropped first).
RENAME = {
    'empty': ('emptyset', 0), 'and': ('land', 0), 'vcenter': ('imvcenter', 0),
    'reflectbox': ('imcontent', 0), 'mathreflectbox': ('imcontent', 0),
    'scalebox': ('imcontent', 1), 'rotatebox': ('imcontent', 1), 'resizebox': ('imcontent', 2),
    # KaTeX's dashed rules (arydshln's, which this preamble does not load): solid.
    'hdashline': ('hline', 0), 'cdashline': ('cline', 0),
}
# Environments whose column specification may hold KaTeX's ':' (a dashed
# vertical rule): drawn as '|'.
COLUMN_ENVS = {'array', 'darray', 'subarray', 'tabular'}
# physics' power of a function: \sin[2](x) is \sin^{2}(x).
POWERED = {'sin', 'cos', 'tan', 'csc', 'sec', 'cot', 'sinh', 'cosh', 'tanh', 'csch', 'sech', 'coth',
           'arcsin', 'arccos', 'arctan', 'exp', 'log', 'ln'}
POWER = re.compile(r'-?\d{1,2}|[nmk]')  # not \sin[x]: brackets, perhaps
SIZES = {'tiny', 'scriptsize', 'footnotesize', 'small', 'normalsize', 'large', 'Large', 'LARGE', 'huge', 'Huge'}
BRACED_DIMEN = {'mskip', 'mkern', 'kern', 'hskip', 'above'}  # KaTeX takes \mskip{10mu}; TeX takes \mskip 10mu
COLOUR_ARGS = {'color': 1, 'textcolor': 1, 'colorbox': 1, 'fcolorbox': 2}
HEX_COLOUR = re.compile(r'\s*#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\s*')


def group_text(toks, k):
    """The text of the {group} at toks[k], and the index after it."""
    end = group_end(toks, k)
    return ''.join(('\\' + v) if kind == 'cs' else v for kind, v in toks[k + 1:end]), end + 1


def colour_hex(spec):
    """The HTML hex of a KaTeX colour argument (#rgb, #rrggbb, or a CSS name), or None."""
    found = HEX_COLOUR.fullmatch(spec)
    if found:
        value = found.group(1)
        return (value if len(value) == 6 else ''.join(c * 2 for c in value)).upper()
    name = spec.strip().lower()
    return CSS_COLOURS[name].upper() if name in CSS_COLOURS else None


def rewrite_colour(toks, k, out):
    """\\color{#hex} and friends as xcolor's \\color[HTML]{hex}; the index after
    what was rewritten, or None when there is nothing to rewrite."""
    count = COLOUR_ARGS[toks[k][1]]
    j, specs = k + 1, []
    for _ in range(count):
        while j < len(toks) and toks[j] == ('char', ' '):
            j += 1
        if j >= len(toks) or toks[j] != ('char', '{'):
            return None
        spec, j = group_text(toks, j)
        specs.append(spec)
    if not any(HEX_COLOUR.fullmatch(spec) for spec in specs):
        return None
    hexes = [colour_hex(spec) for spec in specs]
    if None in hexes:
        return None
    out.append(toks[k])
    out += tokens('[HTML]' + ''.join('{' + h + '}' for h in hexes))
    return j


def katex_syntax(toks):
    """The token-level rewrites (see RENAME, SIZES, BRACED_DIMEN, COLOUR_ARGS)."""
    out, k, n = [], 0, len(toks)
    opens = text_groups(toks)
    stack = []  # per {group}: inside text?
    while k < n:
        kind, value = toks[k]
        in_text = bool(stack) and stack[-1]
        if (kind, value) == ('char', '{'):
            stack.append(k in opens or in_text)
        elif (kind, value) == ('char', '}') and stack:
            stack.pop()
        if kind != 'cs':
            out.append(toks[k])
            k += 1
            continue
        if value in RENAME:
            new, drop = RENAME[value]
            j = skip_arguments(toks, k + 1, drop) if drop else k + 1
            if j is not None:
                out.append(('cs', new))
                k = j
                continue
        if value == 'begin' and env_name(toks, k + 1) in COLUMN_ENVS:
            spec = column_spec(toks, k + 1)
            if spec is not None:
                start, end = spec
                depth = 0
                for j in range(k, end + 1):
                    tok = toks[j]
                    if j > start and tok == ('char', '{'):
                        depth += 1
                    elif j > start and tok == ('char', '}'):
                        depth -= 1
                    out.append(('char', '|') if start < j < end and depth == 0 and tok == ('char', ':') else tok)
                k = end + 1
                continue
        if value in POWERED and k + 1 < n and toks[k + 1] == ('char', '['):
            close = next((j for j in range(k + 2, min(n, k + 6)) if toks[j] == ('char', ']')), None)
            power = ''.join(v for _, v in toks[k + 2:close]) if close else ''
            if close and all(kind == 'char' for kind, _ in toks[k + 2:close]) and POWER.fullmatch(power):
                out += [toks[k], ('char', '^'), ('char', '{')] + toks[k + 2:close] + [('char', '}')]
                k = close + 1
                continue
        if value in ('matrix', 'pmatrix') and k + 1 < n and toks[k + 1] == ('char', '{'):
            end = group_end(toks, k + 1)  # plain TeX's \matrix{a & b \cr c & d}
            if end < n:
                inner = [('cs', '\\') if tok == ('cs', 'cr') else tok for tok in toks[k + 2:end]]
                out += tokens(rf'\begin{{{value}}}') + katex_syntax(inner) + tokens(rf'\end{{{value}}}')
                k = end + 1
                continue
        if value == 'qty':
            j = k + 1
            while j < n and toks[j] == ('char', ' '):
                j += 1
            nxt = toks[j] if j < n else None
            if nxt in (('char', '('), ('char', '['), ('char', '|')) or (
                    nxt == ('char', '{') and skip_arguments(toks, j, 2) is None):
                out.append(('cs', 'imqty'))  # physics' \qty(...), not siunitx's \qty{number}{unit}
                k += 1
                continue
        if value in COLOUR_ARGS:
            j = rewrite_colour(toks, k, out)
            if j is not None:
                k = j
                continue
        if value in BRACED_DIMEN:
            j = k + 1
            while j < n and toks[j] == ('char', ' '):
                j += 1
            if j < n and toks[j] == ('char', '{'):
                end = group_end(toks, j)
                inner = toks[j + 1:end]
                if end < n and all(t[0] == 'char' and t[1] not in '{}' for t in inner):
                    out += [toks[k], ('char', ' ')] + inner + [('cs', 'relax')]
                    k = end + 1
                    continue
        if value == 'rule':
            out.append(toks[k])
            k += 1
            for _ in range(3):  # [raise]{width}{height}, any of them in mu
                while k < n and toks[k] == ('char', ' '):
                    out.append(toks[k])
                    k += 1
                if k < n and toks[k] in (('char', '['), ('char', '{')):
                    close = ']' if toks[k][1] == '[' else '}'
                    end = k + 1
                    while end < n and toks[end] != ('char', close):
                        end += 1
                    text = ''.join(v for _, v in toks[k + 1:end])
                    found = re.fullmatch(r'\s*(-?[\d.]+)\s*mu\s*', text)
                    if found and end < n:
                        out += tokens(toks[k][1] + f'{float(found.group(1)) / 18:.4f}em' + close)
                    else:
                        out += toks[k:end + 1]
                    k = end + 1
            continue
        if value == 'char':
            found = re.match(r'"([0-9A-Fa-f]+)', ''.join(v for _, v in toks[k + 1:k + 8]))
            if found and int(found.group(1), 16) > 255 and int(found.group(1), 16) <= 0x10FFFF:
                out.append(('char', chr(int(found.group(1), 16))))
                k += 1 + len(found.group(0))
                continue
        if value in SIZES and not in_text:
            # To the end of its group (or cell, or line): inside \imsize{\Huge}{...}.
            depth, j = 0, k + 1
            while j < n:
                tok = toks[j]
                if tok == ('char', '{'):
                    depth += 1
                elif tok == ('char', '}'):
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and (tok == ('char', '&') or tok[0] == 'cs' and tok[1] in
                                     ('\\', 'end', 'right', 'middle', 'cr', 'begin') or
                                     tok[0] == 'cs' and tok[1] in SIZES):
                    break
                j += 1
            out += [('cs', 'imsize'), ('char', '{'), toks[k], ('char', '}'), ('char', '{')]
            out += katex_syntax(toks[k + 1:j]) + [('char', '}')]
            k = j
            continue
        out.append(toks[k])
        k += 1
    return out


def column_spec(toks, k):
    """(start, end): the token indices of the {...} column specification after
    the {name} group at toks[k] (an [option] between skipped), or None."""
    n = len(toks)
    k = group_end(toks, k) + 1
    while k < n and toks[k] == ('char', ' '):
        k += 1
    if k < n and toks[k] == ('char', '['):
        while k < n and toks[k] != ('char', ']'):
            k += 1
        k += 1
        while k < n and toks[k] == ('char', ' '):
            k += 1
    if k >= n or toks[k] != ('char', '{'):
        return None
    end = group_end(toks, k)
    return (k, end) if end < n else None


def untokens(toks):
    """The text of a token list (a space after every control word)."""
    return ''.join(('\\' + v + (' ' if v.isalpha() else '')) if kind == 'cs' else v for kind, v in toks)


def prepare(latex, display, have=None):
    """Check one formula and turn it into the TeX that goes in its box
    (`have`: the installed packages, when the caller knows them)."""
    if not isinstance(latex, str) or not latex.strip():
        raise Unsafe('empty formula')
    if len(latex) > MAX_LATEX:
        raise Unsafe(f'formula longer than {MAX_LATEX} characters')
    try:
        latex.encode('utf-8')
    except UnicodeEncodeError:
        raise Unsafe('formula is not valid Unicode') from None
    latex = strip_comments(verbatim_arguments(latex.replace('\r', '')))
    latex = re.sub(r'\n[ \t]*(\n[ \t]*)+', '\n', latex).strip()
    if not latex:
        raise Unsafe('empty formula')
    toks = tokens(latex)
    rewritten = katex_syntax(text_math(toks))
    if rewritten != toks:
        latex = untokens(rewritten).strip()
        if not latex:
            raise Unsafe('empty formula')
    check_safe(tokens(latex), latex)
    if DIAGRAM.search(latex):
        check_diagram(latex)
    latex = strip_numbering(unwrap_environment(latex))
    if have is None:
        try:
            have = available_packages()
        except RendererError:
            have = []
    if 'dsfont' in have:
        # msbm has no digits: \mathbb{1} would draw another glyph, silently.
        latex = re.sub(r'\\mathbb\s*(?:\{\s*1\s*\}|1)', r'\\mathds{1}', latex)
    toks = tokens(latex)
    if display:
        latex = stack_lines(latex, toks)
        toks = tokens(latex)
    return map_unicode(toks)


# ---------------------------------------------------------------- TeX

def tex_env():
    env = dict(os.environ)
    env.update({
        'openin_any': 'p', 'openout_any': 'p', 'shell_escape': 'f',
        'max_print_line': '10000', 'error_line': '254', 'half_error_line': '238',
        # A missing glyph (Hangul, say) must fail at once, not start building fonts.
        'MKTEXTFM': '0', 'MKTEXMF': '0',
    })
    for name in ('TEXINPUTS', 'TEXMFOUTPUT', 'TEXMFCNF', 'TEXFORMATS'):
        env.pop(name, None)
    return env


class RendererError(Exception):
    """A failure of this renderer or its tools, not of a formula; never cached."""


FORMAT_FILES = ('latex.fmt', 'mylatexformat.ltx')
_found = {}  # tuple of file names -> (time.time(), {name: path})
_found_lock = threading.Lock()
# Set by renderd.py: an answer past PACKAGES_TTL is still used once while a
# thread asks kpsewhich again, so no request waits for kpsewhich (~40 ms).
REFRESH_BEHIND = False
_refreshing = set()


def kpse_find(files, cache_dir=None):
    """{name: path} for those of `files` this TeX installation has.

    One kpsewhich call, remembered for PACKAGES_TTL seconds in memory and
    in cache_dir, as a cache hit must not pay for it; a remembered path that
    is gone asks again. kpsewhich exits 1 when any one file is missing, so
    its answer is read whatever the exit status; only a failure to run it at
    all raises RendererError.
    """
    files = tuple(files)
    now = time.time()
    with _found_lock:
        known = _found.get(files)
    if known and all(map(os.path.exists, known[1].values())):
        if now - known[0] < PACKAGES_TTL:
            return known[1]
        if REFRESH_BEHIND:
            with _found_lock:
                start = files not in _refreshing
                _refreshing.add(files)
            if start:
                threading.Thread(target=_refresh, args=(files, cache_dir), daemon=True).start()
            return known[1]
    return _kpse_query(files, cache_dir, now)


def _refresh(files, cache_dir):
    try:
        _kpse_query(files, cache_dir, time.time(), memo_ok=False)
    except RendererError:
        pass
    finally:
        with _found_lock:
            _refreshing.discard(files)


def _kpse_query(files, cache_dir, now, memo_ok=True):
    """kpse_find's work past its memory: the memo file (when memo_ok), else kpsewhich."""
    memo = os.path.join(cache_dir, 'tex-paths.memo') if cache_dir else None
    if memo and memo_ok:
        try:
            if now - os.path.getmtime(memo) < PACKAGES_TTL:
                with open(memo, encoding='utf-8') as f:
                    saved = json.load(f)
                paths = saved.get('paths')
                if saved.get('files') == list(files) and isinstance(paths, dict) and \
                        all(isinstance(p, str) and os.path.exists(p) for p in paths.values()):
                    with _found_lock:
                        _found[files] = (os.path.getmtime(memo), paths)
                    return paths
        except (OSError, ValueError, AttributeError):
            pass
    try:
        done = subprocess.run(['kpsewhich', '-engine=pdftex'] + list(files), capture_output=True,
                              text=True, timeout=5, stdin=subprocess.DEVNULL, env=tex_env())
    except subprocess.TimeoutExpired:
        raise RendererError('renderer: kpsewhich timed out') from None
    except OSError as err:
        raise RendererError(f'renderer: cannot run kpsewhich: {err}') from None
    by_name = {os.path.basename(line): line for line in done.stdout.splitlines() if line.strip()}
    paths = {name: by_name[name] for name in files if name in by_name}
    with _found_lock:
        _found[files] = (now, paths)
    if memo:
        try:
            os.makedirs(cache_dir, mode=0o700, exist_ok=True)
            tmp = f'{memo}.{os.getpid()}.{threading.get_ident()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'files': list(files), 'paths': paths}, f)
            os.replace(tmp, memo)
        except OSError:
            pass
    return paths


def tex_files(user_packages=()):
    """The files kpse_find looks for: the format tools, the CJK fonts, then every package."""
    names = [name for name, _ in PACKAGES] + ['preview'] + list(user_packages)
    return list(FORMAT_FILES) + [f'c70{font}.fd' for font in CJK_FONTS] + [f'{name}.sty' for name in names]


def installed(found):
    """The built-in packages (and preview, and 'cjk-' + each CJK font) among the files kpse_find found."""
    return ([name for name, _ in PACKAGES + [('preview', '')] if f'{name}.sty' in found] +
            [f'cjk-{font}' for font in CJK_FONTS if f'c70{font}.fd' in found])


def available_packages(cache_dir=None):
    """Packages of the built-in preamble (and preview) that this TeX installation has."""
    return installed(kpse_find(tex_files(), cache_dir))


def preamble_text(have, user_packages=(), macros=''):
    """The preamble for the installed packages `have`, up to (not with) \\begin{document}."""
    lines = [r'\documentclass[10pt]{article}']
    for name, options in PACKAGES:
        if name in have:
            before, after = AROUND.get(name, ('', ''))
            lines += [line for line in (before, rf'\usepackage{options}{{{name}}}', after) if line]
    lines += [rf'\usepackage{{{name}}}' for name in user_packages]
    lines += [r'\usepackage[active,tightpage]{preview}', rf'\setlength\PreviewBorder{{{PREVIEW_BORDER_PT}pt}}']
    if macros:
        lines += [macros, r'\relax']  # \relax ends a definition the user left open-ended
    if 'xcolor' in have:
        lines += [''.join(rf'\providecolor{{{name}}}{{HTML}}{{{rgb.upper()}}}' for name, rgb in CSS_COLOURS.items())]
    lines += [COMPAT, r'\pagestyle{empty}']
    return '\n'.join(lines) + '\n'


_which = {}


def tex_identity(paths):
    """What a format depends on besides its preamble: the latex binary and
    every file kpse_find found, with their modification times."""
    now = time.time()
    if 'latex' not in _which or now - _which['latex'][0] > PACKAGES_TTL:
        found = shutil.which('latex')
        _which['latex'] = (now, os.path.realpath(found) if found else '')
    binary = _which['latex'][1]
    parts = []
    for path in [binary] + sorted(paths.values()):
        try:
            parts.append([path, os.stat(path).st_mtime_ns])
        except OSError:
            parts.append([path, None])
    return parts


def format_dir(cache_dir):
    return os.environ.get('INLINE_MATH_FORMAT_DIR') or os.path.join(cache_dir, 'fmt')


class Setup:
    """What one request typesets with: the preamble, the format holding it
    (None: read the preamble on every run), its key, and what went wrong."""

    def __init__(self, preamble, fmt, have, problems):
        self.preamble = preamble
        self.fmt = fmt
        self.have = have
        self.problems = problems
        self.key = hashlib.sha256(preamble.encode('utf-8')).hexdigest()[:24]

    def plain(self):
        """The same preamble without the format (after the format failed to load)."""
        return Setup(self.preamble, None, self.have, self.problems)


def tex_setup(cfg, build=True):
    """The Setup for request options cfg. With build, a missing format is
    built (once per key); without, the Setup only tells the preamble (and
    a format that is there already), which is all a cache lookup needs."""
    problems = [f'packages option: bad package name {name!r}, skipped' for name in cfg['badPackages']]
    found = kpse_find(tex_files(cfg['packages']), cfg['cacheDir'])
    have = installed(found)
    user = [name for name in cfg['packages'] if f'{name}.sty' in found]
    problems += [f'packages option: LaTeX package {name} is not installed, skipped'
                 for name in cfg['packages'] if name not in user]
    preamble = preamble_text(have, user, cfg['macros'])
    fmt, failure = format_for(preamble, found, cfg['cacheDir'], build)
    if fmt is None and failure and not failure.get('plain', True) and (user or cfg['macros']):
        # The user's packages or macros break the preamble: keep what works.
        kept, macros, notes = resolve_user(have, user, cfg['macros'], found, cfg['cacheDir'], failure['error'], build)
        problems += notes
        preamble = preamble_text(have, kept, macros)
        fmt, failure = format_for(preamble, found, cfg['cacheDir'], build)
    if failure:
        problems.append(f'format: {failure["error"]}' +
                        ('; reading the preamble on every run (slower)' if failure.get('plain', True) else ''))
    return Setup(preamble, fmt, have, problems)


def resolve_user(have, user, macros, found, cache_dir, error, build=True):
    """The user's packages and macros that make a working preamble together,
    when all of them at once do not: (packages kept, macros or '', notes).
    The macros are tried alone, then each package is added in turn and kept
    when the preamble still works. Each try is one latex run on the preamble
    (no format), and the answer is remembered beside the formats, so this
    runs once per set of options. Without build, nothing is tried: the
    remembered answer, or none of the user's options."""
    key = hashlib.sha256(json.dumps([VERSION, preamble_text(have, user, macros), tex_identity(found)])
                         .encode('utf-8')).hexdigest()[:24]
    path = os.path.join(format_dir(cache_dir), f'im-{key}.res')
    try:
        if time.time() - os.path.getmtime(path) < FAILURE_TTL:
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
            if isinstance(saved.get('kept'), list) and isinstance(saved.get('macros'), bool):
                return ([name for name in user if name in saved['kept']], macros if saved['macros'] else '',
                        [note for note in saved.get('notes', []) if isinstance(note, str)])
    except (OSError, ValueError, AttributeError):
        pass
    if not build:
        return [], '', [f'packages/macros options break the preamble, left out: {error}']
    notes = []
    keep_macros = False
    if macros:
        failed = error if not user else preamble_error(preamble_text(have, [], macros))
        keep_macros = failed is None
        if failed:
            notes.append(f'macros option breaks the preamble, left out: {failed}')
    kept = []
    for name in user:
        failed = preamble_error(preamble_text(have, kept + [name], macros if keep_macros else ''))
        if failed is None:
            kept.append(name)
        else:
            notes.append(f'packages option: LaTeX package {name} breaks the preamble, left out: {failed}')
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'kept': kept, 'macros': keep_macros, 'notes': notes}, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return kept, macros if keep_macros else '', notes


def format_for(preamble, found, cache_dir, build=True):
    """(path of the format for preamble, None), or (None, {"error", "plain"})
    when it cannot be built; "plain" tells whether the preamble works
    without a format. Built once, under a lock, and installed atomically; a
    failure is remembered for PACKAGES_TTL seconds. Without build, a format
    that is not there yet is (None, None)."""
    if fcntl is None or 'latex.fmt' not in found:
        return None, {'error': 'cannot build a format here (no latex.fmt or no file locks)', 'plain': True}
    key = hashlib.sha256(json.dumps([VERSION, preamble, tex_identity(found)]).encode('utf-8')).hexdigest()[:24]
    folder = format_dir(cache_dir)
    path = os.path.join(folder, f'im-{key}.fmt')
    if os.path.exists(path):
        return path, None
    failure = read_failure(path)
    if failure or not build:
        return None, failure
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        lock = os.open(os.path.join(folder, '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as err:
        return None, {'error': f'cannot use the format folder: {err}', 'plain': True}
    try:
        deadline = time.monotonic() + FORMAT_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as err:
                if err.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() > deadline:
                    return None, {'error': 'format build is busy', 'plain': True}
                time.sleep(0.02)
        if os.path.exists(path):  # another process built it while this one waited
            return path, None
        failure = read_failure(path) or build_format(preamble, found, path)
        if failure:
            return None, failure
        prune_formats(folder, path)
        return path, None
    finally:
        os.close(lock)  # also releases the lock


def read_failure(path):
    """The remembered failure to build the format at path, or None. A
    preamble TeX itself refused (its error a "!" line) is remembered for
    FAILURE_TTL: the same preamble on the same TeX fails the same way."""
    try:
        failed = path[:-4] + '.err'
        age = time.time() - os.path.getmtime(failed)
        if age < FAILURE_TTL:
            with open(failed, encoding='utf-8') as f:
                failure = json.load(f)
            if isinstance(failure, dict) and isinstance(failure.get('error'), str) and (
                    age < PACKAGES_TTL or (failure['error'].startswith('!') and not failure.get('plain', True))):
                return failure
    except (OSError, ValueError):
        pass
    return None


def limit_format():
    """Child-process caps for latex -ini: memory, and the size of the format it writes."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (LATEX_MEMORY, LATEX_MEMORY))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FORMAT_FILE_MAX, FORMAT_FILE_MAX))
    except (AttributeError, ValueError, OSError):
        pass


def build_format(preamble, found, path):
    """Dump preamble into a format at path; None, or the failure (also kept beside path)."""
    folder = os.path.dirname(path)
    with tempfile.TemporaryDirectory(prefix='build-', dir=folder) as tmp:
        if 'mylatexformat.ltx' in found:
            # mylatexformat dumps at \begin{document}, keeping what packages
            # delay until then; plain \dump is the fallback without it.
            source = preamble + '\\begin{document}\n\\end{document}\n'
            argv = ['&latex', 'mylatexformat.ltx', 'pre.tex']
        else:
            source = preamble + '\\dump\n'
            argv = ['&latex', 'pre.tex']
        with open(os.path.join(tmp, 'pre.tex'), 'w', encoding='utf-8') as f:
            f.write(source)
        error = None
        try:
            done = subprocess.run(
                ['latex', '-ini', '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error',
                 '-jobname=im'] + argv,
                cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=FORMAT_TIMEOUT,
                env=tex_env(), stdin=subprocess.DEVNULL, preexec_fn=limit_format)
            built = os.path.join(tmp, 'im.fmt')
            if done.returncode == 0 and os.path.getsize(built) > 0:
                os.chmod(built, 0o600)
                os.replace(built, path)
                return None
            error = log_error(os.path.join(tmp, 'im.log')) or f'latex -ini exited {done.returncode}'
        except subprocess.TimeoutExpired:
            error = 'latex -ini timed out'
        except OSError as err:
            error = f'cannot build the format: {err}'
    failure = {'error': error[:300], 'plain': plain_preamble_works(preamble)}
    try:
        tmp = f'{path[:-4]}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(failure, f)
        os.replace(tmp, path[:-4] + '.err')
    except OSError:
        pass
    return failure


def log_error(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return first_error(f.read(LOG_READ))
    except OSError:
        return None


def plain_preamble_works(preamble):
    """Does preamble make a document without a format (is the format the trouble, or the preamble)?"""
    return preamble_error(preamble) is None


def preamble_error(preamble):
    """None when preamble makes a document (read without a format), else its first error."""
    with tempfile.TemporaryDirectory(prefix='inline-math-') as tmp:
        with open(os.path.join(tmp, 'f.tex'), 'w', encoding='utf-8') as f:
            f.write(preamble + '\\begin{document}\n\\end{document}\n')
        try:
            done = subprocess.run(['latex', '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error',
                                   'f.tex'], cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=FORMAT_TIMEOUT, env=tex_env(), stdin=subprocess.DEVNULL,
                                  preexec_fn=limit_latex)
        except subprocess.TimeoutExpired:
            return 'latex timed out on the preamble'
        except (OSError, subprocess.SubprocessError) as err:
            return f'cannot run latex: {err}'[:300]
        if done.returncode == 0:
            return None
        return (log_error(os.path.join(tmp, 'f.log')) or f'latex exited {done.returncode}')[:300]


def prune_formats(folder, keep):
    """Keep the FORMAT_KEEP newest formats (and failures) of folder; keep is never removed."""
    try:
        with os.scandir(folder) as found:
            files = [(entry.stat().st_mtime, entry.path) for entry in found
                     if entry.name.startswith('im-') and entry.name.endswith(('.fmt', '.err', '.res'))]
    except OSError:
        return
    files.sort(reverse=True)
    for _, path in files[FORMAT_KEEP:]:
        if path != keep:
            try:
                os.remove(path)
            except OSError:
                pass


def tex_body(entries, guard, fg, have, sentinel=False, redraw=False):
    """The document after the preamble: one preview page per (tex, display) entry.

    With guard = (width, height, area) in pt, a formula larger than that is
    measured but not drawn (its page stays empty), so one huge formula cannot
    make dvipng raster gigapixels; it is drawn alone, smaller, afterwards.

    With sentinel, the same fixed formula is drawn as the first and the last
    page. Every item is typeset inside its own groups, so its local changes
    end with it; if a global change still slipped past check_safe, the two
    sentinel pages differ and the caller trusts none of the batch.

    With redraw, a formula is typeset again for its page instead of using
    the measured box (a tikz-cd diagram does not survive being boxed).
    """
    lines = [r'\begin{document}']
    # xcolor writes its own colour, which makes dvipng ignore -fg; so set it here too.
    colour = rf'\color[HTML]{{{fg[1:].upper()}}}' if 'xcolor' in have else ''
    probe = (r'\begin{preview}\sbox0{' + colour + r'$\textstyle ' + SENTINEL +
             r'$}\typeout{IMSENT \the\wd0,\the\ht0,\the\dp0}\usebox0\end{preview}')
    if sentinel:
        lines.append(probe)
    for n, (tex, display) in enumerate(entries):
        style = r'\displaystyle' if display else r'\textstyle'
        cjk = 'CJKutf8' in have and CJK.search(tex)
        lines.append(rf'\typeout{{IMITEM {n}}}')
        lines.append(rf'\begin{{CJK}}{{UTF8}}{{{cjk_family(tex, have)}}}' if cjk else '')
        # A diagram is no macro argument (\sbox's would fix the catcode of its &).
        box = r'\setbox0\hbox{' if redraw else r'\sbox0{'
        wide = not redraw and OVERHANG.search(tex)
        border = rf'\setlength\PreviewBorder{{{OVERHANG_BORDER_PT}pt}}' if wide else ''
        lines.append(border + r'\begin{preview}' + box + colour + '$' + style + ' ' + tex)
        again = '{' + colour + '$' + style + ' ' + tex + '\n$}' if redraw else r'\usebox0'
        lines.append(r'$}\typeout{IMDIM \the\wd0,\the\ht0,\the\dp0}' + draw_box(guard, again) +
                     r'\end{preview}' + (rf'\setlength\PreviewBorder{{{PREVIEW_BORDER_PT}pt}}' if wide else ''))
        lines.append(r'\end{CJK}' if cjk else '')
    if sentinel:
        lines.append(r'\typeout{IMEND}' + probe)
    lines.append(r'\end{document}')
    return '\n'.join(lines) + '\n'


def encodes(text, codec):
    try:
        text.encode(codec)
    except UnicodeEncodeError:
        return False
    return True


# The character set each CJK font covers, in the order they are tried
# when the text has neither Hangul nor kana.
CJK_CHARSETS = (('gbsn', 'gb2312'), ('bsmi', 'big5'), ('ipxm', 'shift_jis'), ('mj', 'cp949'))


def cjk_family(tex, have):
    """The CJK font for the CJK text in tex: Korean for Hangul, Japanese for
    kana; else the first font whose character set holds all of the text
    (Simplified Chinese, Traditional, then Japanese for kanji-only terms
    such as 学習率); Simplified Chinese when none does."""
    text = ''.join(CJK.findall(tex))
    if HANGUL.search(text) and 'cjk-mj' in have:
        return 'mj'
    if KANA.search(text) and 'cjk-ipxm' in have:
        return 'ipxm'
    for font, charset in CJK_CHARSETS:
        if (font == 'gbsn' or f'cjk-{font}' in have) and encodes(text, charset):
            return font
    return 'gbsn'


def draw_box(guard, draw=r'\usebox0'):
    if guard is None:
        return draw
    width, height, area = guard
    # Area in pt^2 from whole points: no overflow for sides below 16384pt.
    return (rf'\ifdim\wd0>{width:.2f}pt\typeout{{IMBIG}}'
            rf'\else\ifdim\dimexpr\ht0+\dp0\relax>{height:.2f}pt\typeout{{IMBIG}}'
            rf'\else\ifnum\numexpr(\wd0/65536)*(\dimexpr\ht0+\dp0\relax/65536)\relax>{int(area)} '
            r'\typeout{IMBIG}\else ' + draw + r'\fi\fi\fi')


def raster_dpi(cfg):
    return max(1, round(cfg['ptPx'] * 72.27 * SUPERSAMPLE))


def area_limit(dpi):
    """Largest page area in pt^2 that dvipng rasters at dpi within MAX_PIXELS (with room for the border)."""
    return 0.8 * MAX_PIXELS / (dpi / 72.27) ** 2


def guard_size(cfg, cell):
    """Largest formula in pt that fits the largest box at full size (TeX allows < 16384pt),
    and the largest area that rasters within MAX_PIXELS."""
    return (min(16000.0, cfg['maxColumns'] * cell[0] / cfg['ptPx']),
            min(16000.0, MAX_BOX * cell[1] / cfg['ptPx']),
            area_limit(raster_dpi(cfg)))


def first_error(log):
    """The first TeX "!" line, with the undefined name when there is one."""
    lines = log.splitlines()
    for k, line in enumerate(lines):
        if line.startswith('!'):
            if 'Undefined control sequence' in line:
                for follow in lines[k + 1:k + 6]:
                    found = re.match(r'(?:l\.\d+|<[a-z ]+>) .*?(\\[A-Za-z]+|\\.)\s*$', follow)
                    if found:
                        return f'{line} {found.group(1)}'
            return line
    return None


def limit_latex():
    """Child-process caps for latex: memory, and the size of any file it writes (its log)."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (LATEX_MEMORY, LATEX_MEMORY))
        resource.setrlimit(resource.RLIMIT_FSIZE, (LATEX_LOG_MAX, LATEX_LOG_MAX))
    except (AttributeError, ValueError, OSError):
        pass


def limit_memory():
    """Child-process cap so a huge page cannot exhaust memory."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    except (AttributeError, ValueError, OSError):
        pass


def latex_argv(fmt):
    """latex with the format at path fmt (None: the plain LaTeX format)."""
    argv = ['latex', '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error']
    return argv + ([f'-fmt={fmt[:-4]}'] if fmt else [])


class Session:
    """The latex and dvipng runs of one request, each a fresh process, in
    folders of a private temporary folder that goes when the session ends.
    renderd.py has a session with warm processes instead."""

    def __init__(self, setup):
        self.setup = setup
        self.root = None
        self.runs = 0

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix='inline-math-')
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)

    def without_format(self):
        """Switch to reading the preamble on every run; False when already so."""
        if self.setup.fmt is None:
            return False
        self.setup = self.setup.plain()
        return True

    def workdir(self):
        self.runs += 1
        path = os.path.join(self.root, str(self.runs))
        os.mkdir(path, 0o700)
        return path

    def latex(self, body, timeout):
        """Typeset body (from tex_body) into f.dvi of a new folder; (folder, exit status or None on timeout)."""
        workdir = self.workdir()
        with open(os.path.join(workdir, 'f.tex'), 'w', encoding='utf-8') as f:
            f.write(('' if self.setup.fmt else self.setup.preamble) + body)
        try:
            done = subprocess.run(latex_argv(self.setup.fmt) + ['f.tex'], cwd=workdir, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=timeout, env=tex_env(),
                                  stdin=subprocess.DEVNULL, preexec_fn=limit_latex)
        except subprocess.TimeoutExpired:
            return workdir, None
        return workdir, done.returncode

    def dvipng(self, workdir, dpi, fg):
        return run_dvipng(workdir, dpi, fg)


def run_limited(argv, cwd, timeout, memory, files=None):
    """Run argv in cwd with a memory cap (and a cap on the files it writes),
    set from outside the child, as a preexec_fn is not safe in the threads of
    renderd.py; the exit status, or None on a timeout."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=tex_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return -1
    try:
        resource.prlimit(proc.pid, resource.RLIMIT_AS, (memory, memory))
        if files:
            resource.prlimit(proc.pid, resource.RLIMIT_FSIZE, (files, files))
    except (AttributeError, OSError, ValueError):
        pass
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None


DIAGRAM = re.compile(r'\\begin\s*\{tikzcd\}')
DIAGRAM_PACKAGE = 'tikz-cd'
DIAGRAM_TIMEOUT = 20.0


class DiagramSession(Session):
    """A Session for tikz-cd diagrams, one per run: dvipng cannot draw
    TikZ's PostScript, so each page goes through dvips (-R2: no shell, no
    absolute paths) and Ghostscript (-dSAFER) instead."""

    redraw = True

    def latex(self, body, timeout):
        workdir = self.workdir()
        with open(os.path.join(workdir, 'f.tex'), 'w', encoding='utf-8') as f:
            f.write(('' if self.setup.fmt else self.setup.preamble) + body)
        status = run_limited(latex_argv(self.setup.fmt) + ['f.tex'], workdir, min(timeout, DIAGRAM_TIMEOUT),
                             LATEX_MEMORY, LATEX_LOG_MAX)
        return workdir, status

    def dvipng(self, workdir, dpi, fg):
        """{page: (path, depth)}, as run_dvipng; the depth comes from the sizes latex logged."""
        try:
            with open(os.path.join(workdir, 'f.log'), encoding='utf-8', errors='replace') as f:
                dims = re.findall(r'^IMDIM (-?[\d.]+)pt,(-?[\d.]+)pt,(-?[\d.]+)pt$', f.read(LOG_READ), re.M)
        except OSError:
            return {}
        if run_limited(['dvips', '-q', '-R2', '-o', 'f.ps', 'f.dvi'], workdir, DVIPNG_TIMEOUT, 1 << 30,
                       LATEX_LOG_MAX) != 0:
            return {}
        if run_limited(['gs', '-dSAFER', '-dBATCH', '-dNOPAUSE', '-dQUIET', '-sDEVICE=pngalpha', f'-r{dpi}',
                        '-dTextAlphaBits=4', '-dGraphicsAlphaBits=4', '-sOutputFile=p%d.png', 'f.ps'],
                       workdir, DVIPNG_TIMEOUT, 1 << 30, MAX_PNG * 64) != 0:
            return {}
        out = {}
        for page, (_, _, dp) in enumerate(dims, 1):
            path = os.path.join(workdir, f'p{page}.png')
            if os.path.exists(path):
                out[page] = (path, round((float(dp) + PREVIEW_BORDER_PT) * dpi / 72.27))
        return out


def check_diagram(latex):
    """Refuse the TikZ options that run code, define keys or write files
    (every control sequence in a diagram is checked by check_safe as well)."""
    lowered = latex.lower()
    for bad in ('/.', 'exec', 'remember picture', 'overlay', 'external', 'handlers', '/utils', 'insert path',
                'after command', 'path picture', 'name prefix', 'name suffix'):
        if bad in lowered:
            raise Unsafe(f'unsafe in a diagram: {bad}')


def run_latex(tex, entries, timeout, guard, fg, sentinel=False):
    """Typeset entries into f.dvi of a new folder.

    Returns (sizes, bad, error, probes, workdir): sizes holds [wd, ht, dp,
    big] in pt for each item typeset before any error; bad is the index of
    the item that stopped TeX, or None; error is the TeX "!" line or another
    failure, or None; probes holds the sentinel sizes (two when the run was
    complete); workdir holds f.dvi.
    """
    body = tex_body(entries, guard, fg, tex.setup.have, sentinel, redraw=getattr(tex, 'redraw', False))
    workdir, status = tex.latex(body, timeout)
    if status is None:
        return [], None, 'timeout', [], workdir
    try:
        with open(os.path.join(workdir, 'f.log'), encoding='utf-8', errors='replace') as f:
            log = f.read(LOG_READ)
    except OSError:
        return [], None, 'renderer preamble failed: latex wrote no log', [], workdir
    dims, probes, current, ended, error = [], [], None, False, first_error(log)
    for line in log.splitlines():
        if line.startswith('!'):
            break
        found = re.fullmatch(r'IMITEM (\d+)', line)
        if found:
            current = int(found.group(1))
        found = re.fullmatch(r'IMDIM (-?[\d.]+)pt,(-?[\d.]+)pt,(-?[\d.]+)pt', line)
        if found:
            dims.append([float(v) for v in found.groups()] + [False])
        if line == 'IMEND':
            current, ended = None, True
        if line.startswith('IMSENT '):
            probes.append(line)
        if line == 'IMBIG' and dims:
            dims[-1][3] = True
    if error is None and status < 0:
        error = 'latex stopped at its memory or output limit'
    if error is None:
        if len(dims) != len(entries):
            if not dims and not probes and status != 0:
                return dims, None, f'renderer preamble failed: latex exited {status}', probes, workdir
            return dims, None, 'latex made the wrong number of pages', probes, workdir
        return dims, None, None, probes, workdir
    if ended and len(dims) == len(entries):
        return dims, None, None, probes, workdir  # the last sentinel failed: an item changed something
    if current is None:
        where = 'preamble' if not dims and not probes else 'run'
        return dims, None, f'renderer {where} failed: {error}', probes, workdir
    if current != len(dims):
        return dims, None, error, probes, workdir  # the error is not inside an item
    return dims, current, error, probes, workdir


def run_dvipng(workdir, dpi, fg):
    """Raster every page of f.dvi at dpi; {page: (path, depth)}.

    Always the whole file: preview's tight box is only right when dvipng
    sees the pages in order, so -pp cannot be used.
    """
    try:
        done = subprocess.run(['dvipng'] + dvipng_options(dpi, fg) + ['-o', 'p%d.png', 'f.dvi'], cwd=workdir,
                              capture_output=True, text=True, timeout=DVIPNG_TIMEOUT, env=tex_env(),
                              stdin=subprocess.DEVNULL, preexec_fn=limit_memory)
    except subprocess.TimeoutExpired:
        return {}
    return dvipng_pages(workdir, done.stdout)


# --noghostscript: no formula needs PostScript (diagrams go through
# DiagramSession), so Ghostscript is never started for a formula's specials.
DVIPNG_FIXED = ['-T', 'tight', '-bg', 'Transparent', '--depth', '-z', '1', '--noghostscript']


def dvipng_colour(fg):
    r, g, b = (int(fg[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return f'rgb {r:.4f} {g:.4f} {b:.4f}'


def dvipng_options(dpi, fg):
    return DVIPNG_FIXED + ['-D', str(dpi), '-fg', dvipng_colour(fg)]


def dvipng_pages(workdir, output):
    """{page: (path, depth)} for the pages dvipng reported and wrote to workdir."""
    out = {}
    for page, depth in re.findall(r'\[(\d+)[^\[\]]*?depth=(-?\d+)', output):
        path = os.path.join(workdir, f'p{page}.png')
        if os.path.exists(path):
            out[int(page)] = (path, int(depth))
    return out


# ---------------------------------------------------------------- pictures

def fit(img, depth, ratio, display, cfg, cell):
    """Place a typeset formula on a canvas of whole cells; the result dict."""
    from PIL import Image
    cw, ch = cell
    baseline = cfg['baseline'] * ch
    img, depth = crop_to_ink(img, depth)
    ascent = (img.height - depth) / ratio
    descent = max(depth / ratio, 1e-9)
    width = img.width / ratio
    max_scale = min(1.0, (cfg['maxColumns'] * cw - 2 * PAD - 1) / max(width, 1),
                    (MAX_BOX * ch - 2) / max(ascent + descent, 1))
    if max_scale < MIN_SCALE:
        return {'error': 'too large'}

    # The fewest rows that hold the formula at no less than min_shrink of its
    # size (all of it for display), and in those rows the text row that lets
    # it be largest.
    floor = min(1.0 if display else cfg['minShrink'], max_scale)
    for rows in range(1, MAX_BOX + 1):
        scale, text_row = max(
            (min(max_scale, (r * ch + baseline - 1) / max(ascent, 1),
                 ((rows - 1 - r) * ch + ch - baseline - 1) / descent), r)
            for r in range(rows)
        )
        if scale >= floor - 1e-9:
            break
    else:
        return {'error': 'too large'}

    w = max(1, round(width * scale))
    h = max(1, round(img.height / ratio * scale))
    columns = min(MAX_BOX, max(1, math.ceil((w + 2 * PAD) / cw)))
    canvas = Image.new('RGBA', (columns * cw, rows * ch), (0, 0, 0, 0))
    top = round(text_row * ch + baseline - ascent * scale)
    canvas.paste(img.resize((w, h), Image.LANCZOS), ((columns * cw - w) // 2, top))
    png = encode(canvas)
    if png is None:
        return {'error': 'too large'}
    return {'png': png, 'columns': columns, 'rows': rows, 'textRow': text_row}


def crop_to_ink(img, depth):
    """Cut the transparent border off, keeping the baseline inside; (img, depth)."""
    box = img.getchannel('A').getbbox()
    if not box:
        return img, depth
    base_row = img.height - depth
    left, top, right, bottom = box
    top, bottom = min(top, base_row - 1), max(bottom, base_row)
    return img.crop((left, top, right, bottom)), bottom - base_row


def encode(canvas):
    """Base64 PNG no larger than the terminal accepts, or None."""
    buf = io.BytesIO()
    canvas.save(buf, 'PNG')
    if buf.tell() > MAX_PNG:
        buf = io.BytesIO()
        try:
            canvas.quantize(256, method=2).save(buf, 'PNG', optimize=True)  # 2: fast octree, keeps alpha
        except (ValueError, OSError):
            return None
        if buf.tell() > MAX_PNG:
            return None
    return base64.b64encode(buf.getvalue()).decode('ascii')


def picture(path, depth, ratio, display, cfg, cell):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None  # the size is checked here, from the header, before any decoding
    with Image.open(path) as img:
        if img.width * img.height > MAX_PIXELS:
            return {'error': 'too large'}
        return fit(img.convert('RGBA'), depth, ratio, display, cfg, cell)


def size_limit(dim, cfg, cell):
    """How far a formula (wd, ht, dp in pt) must shrink to fit the largest box; 1 if not."""
    wd, ht, dp = dim[:3]
    max_width, max_height, _ = guard_size(cfg, cell)
    return min(1.0, max_width / max(wd, 1e-3), max_height / max(ht + dp, 1e-3))


def same_file(a, b):
    try:
        with open(a, 'rb') as fa, open(b, 'rb') as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def raster(tex, workdir, dims, entries, cfg, cell, fg, deadline, probes=None):
    """Pictures for the first len(dims) entries, whose pages are in workdir's
    f.dvi, and whether the batch can be trusted (see tex_body's sentinel).

    With probes (a sentinel run), page 1 is the sentinel and item n is page
    n + 2. Results are None when dvipng did not give exactly one page per
    entry: then no page can be trusted to belong to its entry.
    """
    dpi = raster_dpi(cfg)
    found = tex.dvipng(workdir, dpi, fg)
    shift = 1 if probes is not None else 0
    complete = probes is not None and len(probes) == 2 and len(dims) == len(entries)
    pages = len(dims) + shift + (1 if complete else 0)
    if set(found) != set(range(1, pages + 1)):
        return None, False
    trusted = probes is None or (complete and probes[0] == probes[1] and
                                 same_file(found[1][0], found[pages][0]))
    results = []
    for n, dim in enumerate(dims):
        if dim[3]:
            results.append(typeset_big(tex, entries[n], dim, dpi, cfg, cell, fg, deadline))
        else:
            results.append(page_result(found.get(n + 1 + shift), dpi, entries[n][1], cfg, cell))
    return results, trusted


def typeset_big(tex, entry, dim, dpi, cfg, cell, fg, deadline):
    """A formula too large for the batch, typeset alone at a lower resolution."""
    limit = size_limit(dim, cfg, cell)
    if limit < MIN_SCALE:
        return {'error': 'too large'}
    left = deadline - time.monotonic()
    if left <= 0:
        return {'error': 'timeout'}
    area = max(1.0, dim[0] * (dim[1] + dim[2]))
    small = max(1, math.floor(dpi * min(limit, math.sqrt(area_limit(dpi) / area))))
    dims, _, error, _, workdir = run_latex(tex, [entry], min(left, LATEX_TIMEOUT), None, fg)
    if error is not None:
        return {'error': error}
    found = tex.dvipng(workdir, small, fg)
    return page_result(found.get(1) if len(found) == 1 else None, small, entry[1], cfg, cell)


def page_result(found, dpi, display, cfg, cell):
    """The picture of one page; a failure here is this item's alone."""
    if not found:
        return {'error': 'dvipng failed'}
    path, depth = found
    try:
        return picture(path, depth, dpi / 72.27 / cfg['ptPx'], display, cfg, cell)
    except MemoryError:
        return {'error': 'too large'}
    except Exception as err:  # noqa: BLE001 - one bad page must not fail the batch
        return {'error': f'renderer picture failed: {type(err).__name__}: {err}'[:300]}


def typeset(tex, entries, cfg, cell, fg, deadline):
    """(result, trusted) for each (tex, display) entry: one latex run, plus one per bad item.

    A result is trusted (may be cached) when it came from a run that held it
    alone, or from a batch whose two sentinel pages match.
    """
    results = [None] * len(entries)
    trusted = [False] * len(entries)
    queue = list(range(len(entries)))

    def alone(n):
        results[n] = typeset_one(tex, entries[n], cfg, cell, fg, deadline)
        trusted[n] = True

    while queue:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        batch = [entries[n] for n in queue]
        several = len(batch) > 1
        timeout = min(left, LATEX_TIMEOUT + 0.5 * len(batch))
        dims, bad, error, probes, workdir = run_latex(tex, batch, timeout, guard_size(cfg, cell), fg,
                                                      sentinel=several)
        if error and error.startswith('renderer preamble') and tex.setup.fmt:
            discard_format(tex.setup.fmt, error)
            if tex.without_format():
                continue  # the format did not load: the same batch, reading the preamble
        if dims:
            done, ok = raster(tex, workdir, dims, batch, cfg, cell, fg, deadline, probes if several else None)
        else:
            done, ok = [], True
        if done is None or (ok is False and error is None):
            # Pages and items do not match, or a sentinel changed: one at a time.
            for n in queue[:len(dims)]:
                alone(n)
        else:
            for n, result in zip(queue, done):
                results[n] = result
                trusted[n] = ok and error is None
        if bad is not None:
            culprit = queue[bad]
            if bad == 0 or not several:
                results[culprit], trusted[culprit] = {'error': error}, True
            else:
                alone(culprit)  # an item before it may have caused the error
            # The items before it are typeset again with the rest: the
            # run stopped before its last sentinel, so they are unchecked.
            queue = [n for n in queue if n != culprit]
        elif error is not None and several and not error.startswith('renderer'):
            for n in queue[len(dims):]:  # one at a time, so one bad item fails alone
                if time.monotonic() >= deadline:
                    break
                alone(n)
            queue = []
        else:
            for n in queue[len(dims):] if error is not None else []:
                results[n] = {'error': error}
            queue = []
    return [(r if r is not None else {'error': 'timeout'}, t) for r, t in zip(results, trusted)]


def typeset_one(tex, entry, cfg, cell, fg, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        return {'error': 'timeout'}
    dims, _, error, _, workdir = run_latex(tex, [entry], min(left, LATEX_TIMEOUT), guard_size(cfg, cell), fg)
    if error is not None:
        return {'error': error}
    done, _ = raster(tex, workdir, dims, [entry], cfg, cell, fg, deadline)
    return done[0] if done else {'error': 'dvipng failed'}


def diagrams_available(cache_dir):
    """Can tikz-cd diagrams be drawn here (tikz-cd, dvips and Ghostscript installed)?"""
    try:
        found = kpse_find(tex_files([DIAGRAM_PACKAGE]), cache_dir)
    except RendererError:
        return False
    return f'{DIAGRAM_PACKAGE}.sty' in found and bool(shutil.which('dvips') and shutil.which('gs'))


def typeset_diagram(entry, cfg, cell, fg, deadline):
    """(result, trusted) for one tikz-cd diagram: alone, with tikz-cd in its preamble."""
    if f'{DIAGRAM_PACKAGE}.sty' not in kpse_find(tex_files([DIAGRAM_PACKAGE]), cfg['cacheDir']):
        return {'error': 'tikz-cd is not installed'}, False
    if not (shutil.which('dvips') and shutil.which('gs')):
        return {'error': 'diagrams need dvips and Ghostscript (gs)'}, False
    setup = tex_setup(dict(cfg, packages=[DIAGRAM_PACKAGE] + cfg['packages']))
    with DiagramSession(setup) as tex:
        result = typeset(tex, [entry], cfg, cell, fg, deadline)[0]
    if 'error' in result[0] and result[0]['error'].startswith('renderer preamble'):
        return result[0], False
    return result


def discard_format(path, error):
    """A format that does not load: removed, and not built again for PACKAGES_TTL seconds."""
    try:
        with open(path[:-4] + '.err', 'w', encoding='utf-8') as f:
            json.dump({'error': f'the format did not load: {error}'[:300], 'plain': True}, f)
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- cache

def cache_key(latex, display, cell, fg, cfg, preamble_key):
    """Everything a picture depends on, including the preamble: the installed
    packages and the user's packages and macros (each changes how a formula
    looks, or whether it works)."""
    parts = [VERSION, latex, bool(display), list(cell), fg, cfg['baseline'], cfg['ptPx'],
             cfg['minShrink'], cfg['maxColumns'], preamble_key]
    return hashlib.sha256(json.dumps(parts).encode('utf-8')).hexdigest()


def cache_get(cache_dir, key):
    path = os.path.join(cache_dir, key + '.json')
    try:
        with open(path, encoding='utf-8') as f:
            value = json.load(f)
        os.utime(path)  # recently used: pruned last
    except (OSError, ValueError):
        return None
    ok = isinstance(value, dict) and ('error' in value or 'png' in value)
    return value if ok else None


def cache_put(cache_dir, key, value):
    """Write atomically: a temporary file, then rename."""
    try:
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        tmp = os.path.join(cache_dir, f'.{key}.{os.getpid()}.{threading.get_ident()}.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(value, f)
        os.replace(tmp, os.path.join(cache_dir, key + '.json'))
        return True
    except OSError:
        return False


def cache_prune(cache_dir, max_entries=CACHE_MAX_ENTRIES, max_bytes=CACHE_MAX_BYTES):
    """Delete the least recently used entries beyond the bounds (down to 80 %)."""
    try:
        entries = []
        with os.scandir(cache_dir) as found:
            for entry in found:
                st = entry.stat()
                if entry.name.endswith('.json'):
                    entries.append((st.st_mtime, st.st_size, entry.path))
                elif entry.name.endswith('.tmp') and st.st_mtime < time.time() - 3600:
                    os.remove(entry.path)  # left by a killed run
    except OSError:
        return
    total = sum(size for _, size, _ in entries)
    if len(entries) <= max_entries and total <= max_bytes:
        return
    entries.sort()
    keep_entries, keep_bytes = int(max_entries * 0.8), int(max_bytes * 0.8)
    count = len(entries)
    for _, size, path in entries:
        if count <= keep_entries and total <= keep_bytes:
            break
        try:
            os.remove(path)
        except OSError:
            pass
        count -= 1
        total -= size


# ---------------------------------------------------------------- request

def cacheable(result):
    """Pictures and errors that come from the formula itself; not timeouts or missing tools."""
    error = result.get('error', '')
    return 'png' in result or error.startswith('!') or error == 'too large'


def missing_tools():
    problems = []
    for tool in ('latex', 'dvipng'):
        if not shutil.which(tool):
            problems.append(f'{tool} not found (install TeX Live with dvipng)')
    try:
        import PIL  # noqa: F401
    except ImportError:
        problems.append('Pillow not found (install python3 Pillow)')
    return problems


# Splitting a batch over parallel sessions (renderd.py's warm latex pool).
# Weights are TeX time in ms, about: a plain formula 1, mhchem's \ce and \pu
# 2.5 per character of their argument (\ce{H2O} 6, a long reaction 60-80).
HEAVY = re.compile(r'\\(?:ce|pu)(?![A-Za-z])')
HEAVY_MS_PER_CHAR = 2.5
SPLIT_WEIGHT = 40  # a batch this heavy is split; a lighter one is not worth the extra runs


def tex_weight(tex):
    weight = 1.0
    for found in HEAVY.finditer(tex):
        group = brace_group(tex, found.end())
        weight += HEAVY_MS_PER_CHAR * len(group[0] if group else 'x' * 10)
    return weight


def split_batch(entries, parallel):
    """Groups of entry indices to typeset side by side (one group: no split):
    at most `parallel` groups of about equal weight, heaviest placed first."""
    weights = [tex_weight(tex) for tex, _ in entries]
    groups = min(parallel, len(entries))
    if groups < 2 or sum(weights) < SPLIT_WEIGHT:
        return [list(range(len(entries)))]
    bins = [[0, []] for _ in range(groups)]
    for k in sorted(range(len(entries)), key=lambda k: -weights[k]):
        lightest = min(bins, key=lambda b: b[0])
        lightest[0] += weights[k]
        lightest[1].append(k)
    return [sorted(members) for _, members in bins if members]


def typeset_split(new_session, setup, entries, cfg, cell, fg, deadline, parallel):
    """typeset() for entries, split by split_batch over parallel sessions (threads)."""
    groups = split_batch(entries, parallel)
    if len(groups) == 1:
        with new_session(setup) as tex:
            return typeset(tex, entries, cfg, cell, fg, deadline)
    done = [None] * len(entries)

    def work(members):
        try:
            with new_session(setup) as tex:
                found = typeset(tex, [entries[k] for k in members], cfg, cell, fg, deadline)
        except Exception as err:  # noqa: BLE001 - reported per item, never cached
            found = [({'error': f'renderer failed: {err}'[:300]}, False)] * len(members)
        for k, result in zip(members, found):
            done[k] = result

    threads = [threading.Thread(target=work, args=(members,), daemon=True) for members in groups[1:]]
    for thread in threads:
        thread.start()
    work(groups[0])
    for thread in threads:
        thread.join()
    return done


def render_request(request, new_session=Session, cached=True, parallel=1):
    """The full response for a request dict; new_session(setup) makes the
    Session that runs TeX (renderd.py passes one with warm processes, and
    `parallel`: how many it can run side by side)."""
    started = time.monotonic()
    cfg = parse_options(request.get('options'))
    cell = detect_cell(cfg['cell'])
    fg = detect_fg(cfg['fg'])
    items = request.get('items')
    items = items if isinstance(items, list) else []
    results = [None] * len(items)
    response = {'cell': {'width': cell[0], 'height': cell[1]}, 'fg': fg, 'results': results}
    if not items:
        return response
    try:
        setup = tex_setup(cfg, build=False)  # the preamble alone: a cache hit builds no format
    except RendererError as err:
        response['results'] = [{'error': str(err)} for _ in items]
        return response

    def key_of(latex, display, preamble_key):
        return cache_key(latex, display, cell, fg, cfg, preamble_key)

    todo = {}  # (latex, display) -> [indices]; also dedupes
    for n, item in enumerate(items):
        item = item if isinstance(item, dict) else {}
        latex, display = item.get('latex'), bool(item.get('display'))
        hit = cache_get(cfg['cacheDir'], key_of(latex, display, setup.key)) \
            if cached and isinstance(latex, str) else None
        if hit is not None:
            results[n] = hit
        else:
            todo.setdefault((latex, display), []).append(n)

    entries, owners = [], []
    for (latex, display), indices in todo.items():
        try:
            entries.append((prepare(latex, display, setup.have), display))
            owners.append((latex, display, indices))
        except Exception as err:  # noqa: BLE001 - Unsafe, or a bug that must stay with its item
            error = str(err) if isinstance(err, Unsafe) else f'renderer prepare failed: {err}'[:300]
            for n in indices:
                results[n] = {'error': error}

    if entries:
        problems = missing_tools()
        if problems:
            done = [({'error': problems[0]}, False)] * len(entries)
        else:
            diagrams = [k for k, (tex, _) in enumerate(entries) if DIAGRAM.search(tex)]
            formulas = [k for k in range(len(entries)) if k not in diagrams]
            done = [None] * len(entries)
            try:
                setup = tex_setup(cfg)
                if formulas:
                    found = typeset_split(new_session, setup, [entries[k] for k in formulas], cfg, cell, fg,
                                          started + DEADLINE, parallel)
                    for k, result in zip(formulas, found):
                        done[k] = result
                for k in diagrams:
                    done[k] = typeset_diagram(entries[k], cfg, cell, fg, started + DEADLINE)
            except Exception as err:  # noqa: BLE001 - reported per item, never cached
                done = [({'error': f'renderer failed: {err}'[:300]}, False)] * len(entries)
        wrote = False
        for (latex, display, indices), (result, trusted) in zip(owners, done):
            if cached and trusted and cacheable(result):
                wrote |= cache_put(cfg['cacheDir'], key_of(latex, display, setup.key), result)
            for n in indices:
                results[n] = result
        if wrote:
            cache_prune(cfg['cacheDir'])

    return response


def detect():
    """The 'auto' cell size and colour alone: cheap enough to ask every few seconds."""
    cell, fg = detect_cell('auto'), detect_fg('auto')
    return {'cell': {'width': cell[0], 'height': cell[1]}, 'fg': fg}


def check(options=None, new_session=Session):
    """Status for a /status command: tools, the format, a real render (past
    the picture cache), cell and colour. `options` are request options: the
    user's packages and macros are checked too."""
    options = dict(options) if isinstance(options, dict) else {}
    problems, notes = missing_tools(), []
    try:
        have = available_packages()
    except RendererError as err:
        have = []
        problems.append(str(err))
    for name in REQUIRED:
        if name not in have:
            problems.append(f'LaTeX package {name} not found')
    notes += [f'optional LaTeX package {name} not found' for name, _ in PACKAGES
              if name not in have and name not in REQUIRED]
    cell = parse_cell(options.get('cell')) if isinstance(options.get('cell'), str) else None
    cell = cell or detect_cell('auto')
    fg = options.get('fg') if isinstance(options.get('fg'), str) else 'auto'
    fg = detect_fg(fg)
    options.update({'cell': f'{cell[0]}x{cell[1]}', 'fg': fg})
    cfg = parse_options(options)
    fmt = {'ok': False}
    rendered = False
    if not problems:
        try:
            setup = tex_setup(cfg)
            notes += setup.problems
            fmt = {'ok': bool(setup.fmt), 'path': setup.fmt} if setup.fmt else {'ok': False}
        except RendererError as err:
            problems.append(str(err))
        result = render_request({'items': [{'latex': r'x^2+\alpha', 'display': False}], 'options': options},
                                new_session=new_session, cached=False)
        first = result['results'][0]
        rendered = 'png' in first
        if not rendered:
            problems.append(f'test render failed: {first.get("error")}')
    try:
        import PIL  # noqa: F401
        pil = True
    except ImportError:
        pil = False
    return {
        'ok': rendered and not problems,
        'latex': bool(shutil.which('latex')),
        'dvipng': bool(shutil.which('dvipng')),
        'pil': pil,
        'cjk': 'CJKutf8' in have,
        'cjkFonts': [font for font in CJK_FONTS if f'cjk-{font}' in have],
        'diagrams': diagrams_available(cfg['cacheDir']),
        'format': fmt,
        'packages': [name for name in cfg['packages']],
        'cell': {'width': cell[0], 'height': cell[1]},
        'fg': fg,
        'cacheDir': cfg['cacheDir'],
        'problems': problems + notes,
    }


def stdin_options(wait=0.2):
    """Request options given on stdin with --check, if any arrive in time (stdin may stay open)."""
    try:
        import select
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        ready, _, _ = select.select([sys.stdin], [], [], wait)
        if not ready:
            return {}
        text = sys.stdin.read(MAX_MACROS * 4)
        request = json.loads(text) if text.strip() else {}
    except (OSError, ValueError):
        return {}
    options = request.get('options') if isinstance(request, dict) else None
    return options if isinstance(options, dict) else {}


def answer(request, new_session=Session, parallel=1):
    """The response to a parsed request (anything json.loads gave); never raises."""
    problem = None if isinstance(request, dict) else 'bad request: not an object'
    request = request if isinstance(request, dict) else {}
    try:
        response = render_request(request, new_session=new_session, parallel=parallel)
    except Exception as err:  # noqa: BLE001 - reported to the mod as data
        items = request.get('items') if isinstance(request.get('items'), list) else []
        response = {'results': [{'error': f'renderer failed: {err}'[:300]} for _ in items]}
        # Only what the request itself fixed: a guessed cell or colour would
        # make the mod file its pictures under the wrong ones.
        options = request.get('options') if isinstance(request.get('options'), dict) else {}
        cell = parse_cell(options.get('cell')) if isinstance(options.get('cell'), str) else None
        fg = options.get('fg')
        if cell:
            response['cell'] = {'width': cell[0], 'height': cell[1]}
        if isinstance(fg, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', fg):
            response['fg'] = fg.lower()
    if problem:
        response['error'] = problem
    return response


def answer_text(text, new_session=Session):
    """The response to a request as JSON text."""
    try:
        request = json.loads(text or '{}')
    except ValueError as err:
        response = answer({})
        response['error'] = f'bad request: {err}'
        return response
    return answer(request, new_session)


def main():
    if '--check' in sys.argv[1:]:
        print(json.dumps(check(stdin_options())))
        return
    if '--detect' in sys.argv[1:]:
        print(json.dumps(detect()))
        return
    print(json.dumps(answer_text(sys.stdin.read())))


if __name__ == '__main__':
    main()
