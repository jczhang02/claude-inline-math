"""Render LaTeX formulas to PNG pictures that fill whole boxes of terminal cells.

Usage:
    render.py            JSON request on stdin, JSON response on stdout
    render.py --check    JSON status of the tools this renderer needs
    render.py --detect   JSON {"cell", "fg"} the 'auto' options resolve to now

Request:
    {"items": [{"latex": str, "display": bool}, ...],
      "options": {"fg": "auto" | "#rrggbb", "cell": "auto" | "WxH",
                              "baseline": 0.773, "ptPx": 3.4, "minShrink": 0.8,
                              "cacheDir": str | null, "maxColumns": 255}}

Response (results in item order, exit status 0 even when items fail):
    {"cell": {"width", "height"}, "fg": "#rrggbb",
      "results": [{"png", "columns", "rows", "textRow"} | {"error"}, ...]}

Each picture is drawn at the terminal's own pixel size (columns x cell width
by rows x cell height), so the terminal never rescales it. Its baseline sits
on the text baseline of cell row `textRow`, so text beside it lines up when
that text is drawn `textRow` rows down.

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
import time

VERSION = 'inline-math-render 3'
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

PACKAGES_TTL = 600.0  # seconds a cached kpsewhich answer is trusted
PACKAGES = ['lmodern', 'amsmath', 'amssymb', 'mathtools', 'mathrsfs', 'cancel', 'xcolor', 'bm']

# Slow packages, loaded only for a batch that uses one of their commands.
ON_DEMAND = [
    ('mhchem', '[version=4]', re.compile(r'\\(ce|pu)(?![A-Za-z])')),
    ('siunitx', '', re.compile(r'\\(SI|si|num|qty|unit|ang|SIrange|numrange|qtyrange)(?![A-Za-z])')),
    ('dsfont', '', re.compile(r'\\(mathds|mathbbm)(?![A-Za-z])')),
    ('stmaryrd', '', re.compile(r'\\(llbracket|rrbracket|lbrbrak|rbrbrak|llparenthesis|rrparenthesis|'
                                r'shortrightarrow|shortleftarrow|mapsfrom|Mapsto|Mapsfrom|'
                                r'boxdot|boxplus|boxtimes|oblong|varovee|merge|interleave|sslash|bbslash|'
                                r'nnearrow|nnwarrow|ssearrow|sswarrow|lightning)(?![A-Za-z])')),
]
SENTINEL = (r'\pi\alpha+\sum_{i=1}^{n}x_i^2\le\frac{a}{b}\sqrt{y}\int\mathbb{R}\in\infty'
            r'\cdot\left(\mathcal{L}\right)\text{Ab}')

# Commands models use without defining them. \providecommand keeps any
# package definition that already exists.
COMPAT = r'''
\providecommand\R{\mathbb{R}}\providecommand\N{\mathbb{N}}
\providecommand\Z{\mathbb{Z}}\providecommand\Q{\mathbb{Q}}
\providecommand\C{\mathbb{C}}\providecommand\E{\mathbb{E}}
\providecommand\dd{\mathop{}\!\mathrm{d}}
\providecommand\vb[1]{\mathbf{#1}}
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
\ifdefined\DeclarePairedDelimiter
  \DeclarePairedDelimiter\abs{\lvert}{\rvert}
  \DeclarePairedDelimiter\norm{\lVert}{\rVert}
  \DeclarePairedDelimiter\floor{\lfloor}{\rfloor}
  \DeclarePairedDelimiter\ceil{\lceil}{\rceil}
\fi
\providecommand\bra[1]{\left\langle #1\right\rvert}
\providecommand\ket[1]{\left\lvert #1\right\rangle}
\makeatletter
\providecommand\braket[1]{\@ifnextchar\bgroup{\im@braket{#1}}{\left\langle #1\right\rangle}}
\newcommand\im@braket[2]{\left\langle #1\middle\vert #2\right\rangle}
\newcommand\im@order[1]{\if\relax\detokenize{#1}\relax\else^{#1}\fi}
\newcommand\im@dv[3]{\@ifnextchar\bgroup{\im@dvof{#1}{#2}{#3}}%
    {\frac{#1\im@order{#2}}{#1#3\im@order{#2}}}}
\newcommand\im@dvof[4]{\frac{#1\im@order{#2}#3}{#1#4\im@order{#2}}}
\providecommand\dv[2][]{\im@dv{\mathrm{d}}{#1}{#2}}
\providecommand\pdv[2][]{\im@dv{\partial}{#1}{#2}}
\makeatother
'''

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
UNSAFE_PREFIXES = ('pdf', 'every', 'tracing', 'show', 'Show', 'global', 'new', 'renew', 'provide',
                   'New', 'Renew', 'Provide', 'Declare', 'Use', 'Expand', 'AddTo', 'RemoveFrom',
                   'At', 'Activate', 'Disable', 'Clear', 'Copy', 'Debug')
SAFE_NAMES = {'newline', 'newton'}  # start like an unsafe prefix, but are not
SAFE_ENVS = set('''
matrix pmatrix bmatrix Bmatrix vmatrix Vmatrix smallmatrix
matrix* pmatrix* bmatrix* Bmatrix* vmatrix* Vmatrix* smallmatrix*
psmallmatrix bsmallmatrix Bsmallmatrix vsmallmatrix Vsmallmatrix
psmallmatrix* bsmallmatrix* Bsmallmatrix* vsmallmatrix* Vsmallmatrix*
cases cases* dcases dcases* rcases rcases* drcases drcases*
aligned alignedat gathered split multlined lgathered rgathered
array subarray tabular
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
    return {
        'fg': raw.get('fg', 'auto'),
        'cell': raw.get('cell', 'auto'),
        'baseline': number('baseline', 0.773, 0.05, 0.95),
        'ptPx': number('ptPx', 3.4, 0.5, 20),
        'minShrink': number('minShrink', 0.8, MIN_SCALE, 1),
        'maxColumns': int(number('maxColumns', MAX_BOX, 1, MAX_BOX)),
        'cacheDir': cache_dir if isinstance(cache_dir, str) and cache_dir else default_cache_dir(),
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


def unwrap_once(latex):
    whole = whole_environment(latex)
    if not whole or whole[0] not in WHOLE_ENVS:
        return latex
    name, body, tail = whole
    inner = WHOLE_ENVS[name]
    if inner is None:
        return body.strip() + tail
    if inner == 'alignedat':
        count = re.match(r'\s*(\{\d+\})', body)
        if not count:
            return latex
        return rf'\begin{{alignedat}}{count.group(1)}{body[count.end():]}\end{{alignedat}}' + tail
    return rf'\begin{{{inner}}}{body}\end{{{inner}}}' + tail


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


def map_unicode(toks):
    """Rewrite Unicode symbols as TeX; bare CJK runs in math go into \\text{}."""
    out, stack, pending_text, i = [], [], False, 0
    while i < len(toks):
        kind, value = toks[i]
        in_text = any(stack)
        accent = toks[i + 1][1] if i + 1 < len(toks) and toks[i + 1][0] == 'char' else None
        if kind == 'char' and accent in COMBINING and value.isascii() and value.isalnum() and not in_text:
            out.append(COMBINING[accent] + '{' + value + '}')
            i += 2
            continue
        if kind == 'cs':
            out.append('\\' + value + (' ' if value.isalpha() else ''))
            pending_text = value in TEXT_COMMANDS
            i += 1
            continue
        if value == '{':
            stack.append(pending_text or in_text)
        elif value == '}' and stack:
            stack.pop()
        if value not in ' {}':
            pending_text = False
        if value in SUPERSCRIPTS or value in SUBSCRIPTS:
            table, mark = (SUPERSCRIPTS, '^') if value in SUPERSCRIPTS else (SUBSCRIPTS, '_')
            run = []
            while i < len(toks) and toks[i][0] == 'char' and toks[i][1] in table:
                run.append(table[toks[i][1]])
                i += 1
            script = mark + '{' + ''.join(run) + '}'
            out.append(rf'\ensuremath{{{script}}}' if in_text else script)
            continue
        if value in UNICODE_MATH:
            tex = UNICODE_MATH[value]
            if in_text:
                out.append(rf'\ensuremath{{{tex}}}' if tex.startswith('\\') else tex)
            else:
                out.append(tex + ' ')
        elif not in_text and not value.isascii() and value not in UNICODE_MATH:
            run = []
            while (i < len(toks) and toks[i][0] == 'char' and not toks[i][1].isascii()
                          and toks[i][1] not in UNICODE_MATH and toks[i][1] not in SUPERSCRIPTS
                          and toks[i][1] not in SUBSCRIPTS):
                run.append(toks[i][1])
                i += 1
            out.append(r'\text{' + ''.join(run) + '}')
            continue
        else:
            out.append(value)
        i += 1
    return ''.join(out)


def prepare(latex, display):
    """Check one formula and turn it into the TeX that goes in its box."""
    if not isinstance(latex, str) or not latex.strip():
        raise Unsafe('empty formula')
    if len(latex) > MAX_LATEX:
        raise Unsafe(f'formula longer than {MAX_LATEX} characters')
    try:
        latex.encode('utf-8')
    except UnicodeEncodeError:
        raise Unsafe('formula is not valid Unicode') from None
    latex = strip_comments(latex.replace('\r', ''))
    latex = re.sub(r'\n[ \t]*(\n[ \t]*)+', '\n', latex).strip()
    if not latex:
        raise Unsafe('empty formula')
    check_safe(tokens(latex), latex)
    latex = strip_numbering(unwrap_environment(latex))
    try:
        have_dsfont = 'dsfont' in available_packages()
    except RendererError:
        have_dsfont = False
    if have_dsfont:
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
    for name in ('TEXINPUTS', 'TEXMFOUTPUT', 'TEXMFCNF'):
        env.pop(name, None)
    return env


class RendererError(Exception):
    """A failure of this renderer or its tools, not of a formula; never cached."""


_available = None


def available_packages(cache_dir=None):
    """Packages of the preamble that this TeX installation has.

    One kpsewhich call, remembered for PACKAGES_TTL seconds in cache_dir, as
    a cache hit must not pay for it. kpsewhich exits 1 when any one file is
    missing, so its answer is read whatever the exit status; only a failure
    to run it at all raises RendererError.
    """
    global _available
    if _available is not None:
        return _available
    names = PACKAGES + ['preview', 'CJKutf8'] + [name for name, _, _ in ON_DEMAND]
    memo = os.path.join(cache_dir, 'packages.txt') if cache_dir else None
    if memo:
        try:
            if time.time() - os.path.getmtime(memo) < PACKAGES_TTL:
                with open(memo, encoding='utf-8') as f:
                    lines = f.read().split('\n')
                if lines[0] == ' '.join(names):
                    _available = [name for name in lines[1].split() if name in names]
                    return _available
        except (OSError, IndexError):
            pass
    try:
        done = subprocess.run(['kpsewhich'] + [f'{name}.sty' for name in names], capture_output=True,
                              text=True, timeout=5, stdin=subprocess.DEVNULL, env=tex_env())
    except subprocess.TimeoutExpired:
        raise RendererError('renderer: kpsewhich timed out') from None
    except OSError as err:
        raise RendererError(f'renderer: cannot run kpsewhich: {err}') from None
    found = {os.path.basename(line)[:-4] for line in done.stdout.splitlines() if line.endswith('.sty')}
    _available = [name for name in names if name in found]
    if memo:
        try:
            os.makedirs(cache_dir, mode=0o700, exist_ok=True)
            tmp = f'{memo}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(' '.join(names) + '\n' + ' '.join(_available) + '\n')
            os.replace(tmp, memo)
        except OSError:
            pass
    return _available


def tex_document(entries, guard, fg, sentinel=False):
    """One latex document with one preview page per (tex, display) entry.

    With guard = (width, height, area) in pt, a formula larger than that is
    measured but not drawn (its page stays empty), so one huge formula cannot
    make dvipng raster gigapixels; it is drawn alone, smaller, afterwards.

    With sentinel, the same fixed formula is drawn as the first and the last
    page. Every item is typeset inside its own groups, so its local changes
    end with it; if a global change still slipped past check_safe, the two
    sentinel pages differ and the caller trusts none of the batch.
    """
    have = available_packages()
    lines = [r'\documentclass[10pt]{article}']
    lines += [rf'\usepackage{{{name}}}' for name in PACKAGES if name in have]
    if 'CJKutf8' in have:
        lines.append(r'\usepackage{CJKutf8}')
    for name, options, used in ON_DEMAND:
        if name in have and any(used.search(tex) for tex, _ in entries):
            lines.append(rf'\usepackage{options}{{{name}}}')
    # A border keeps italic overhangs from being cut off; fit() crops to the ink.
    lines += [r'\usepackage[active,tightpage]{preview}', r'\setlength\PreviewBorder{1pt}', COMPAT,
              r'\pagestyle{empty}', r'\begin{document}']
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
        lines.append(r'\begin{CJK}{UTF8}{gbsn}' if cjk else '')
        lines.append(r'\begin{preview}\sbox0{' + colour + '$' + style + ' ' + tex)
        lines.append(r'$}\typeout{IMDIM \the\wd0,\the\ht0,\the\dp0}' + draw_box(guard) + r'\end{preview}')
        lines.append(r'\end{CJK}' if cjk else '')
    if sentinel:
        lines.append(r'\typeout{IMEND}' + probe)
    lines.append(r'\end{document}')
    return '\n'.join(lines) + '\n'


def draw_box(guard):
    if guard is None:
        return r'\usebox0'
    width, height, area = guard
    # Area in pt^2 from whole points: no overflow for sides below 16384pt.
    return (rf'\ifdim\wd0>{width:.2f}pt\typeout{{IMBIG}}'
            rf'\else\ifdim\dimexpr\ht0+\dp0\relax>{height:.2f}pt\typeout{{IMBIG}}'
            rf'\else\ifnum\numexpr(\wd0/65536)*(\dimexpr\ht0+\dp0\relax/65536)\relax>{int(area)} '
            r'\typeout{IMBIG}\else\usebox0\fi\fi\fi')


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
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (LATEX_MEMORY, LATEX_MEMORY))
        resource.setrlimit(resource.RLIMIT_FSIZE, (LATEX_LOG_MAX, LATEX_LOG_MAX))
    except (ImportError, ValueError, OSError):
        pass


def run_latex(workdir, entries, timeout, guard, fg, sentinel=False):
    """Typeset entries into f.dvi.

    Returns (sizes, bad, error, probes): sizes holds [wd, ht, dp, big] in pt
    for each item typeset before any error; bad is the index of the item that
    stopped TeX, or None; error is the TeX "!" line or another failure, or
    None; probes holds the sentinel sizes (two when the run was complete).
    """
    path = os.path.join(workdir, 'f.tex')
    for name in os.listdir(workdir):
        os.remove(os.path.join(workdir, name))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(tex_document(entries, guard, fg, sentinel))
    try:
        done = subprocess.run(
            ['latex', '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error', 'f.tex'],
            cwd=workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
            env=tex_env(), stdin=subprocess.DEVNULL, preexec_fn=limit_latex,
        )
    except subprocess.TimeoutExpired:
        return [], None, 'timeout', []
    try:
        with open(os.path.join(workdir, 'f.log'), encoding='utf-8', errors='replace') as f:
            log = f.read(LOG_READ)
    except OSError:
        return [], None, 'latex wrote no log', []
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
    if error is None and done.returncode < 0:
        error = 'latex stopped at its memory or output limit'
    if error is None:
        if len(dims) != len(entries):
            return dims, None, 'latex made the wrong number of pages', probes
        return dims, None, None, probes
    if ended and len(dims) == len(entries):
        return dims, None, None, probes  # the last sentinel failed: an item changed something
    if current is None:
        where = 'preamble' if not dims else 'run'
        return dims, None, f'renderer {where} failed: {error}', probes
    if current != len(dims):
        return dims, None, error, probes  # the error is not inside an item
    return dims, current, error, probes


def limit_memory():
    """Child-process cap so a huge page cannot exhaust memory."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    except (ImportError, ValueError, OSError):
        pass


def run_dvipng(workdir, dpi, fg):
    """Raster every page of f.dvi at dpi; {page: (path, depth)}.

    Always the whole file: preview's tight box is only right when dvipng
    sees the pages in order, so -pp cannot be used.
    """
    r, g, b = (int(fg[i:i + 2], 16) / 255 for i in (1, 3, 5))
    argv = ['dvipng', '-T', 'tight', '-D', str(dpi), '-bg', 'Transparent',
            '-fg', f'rgb {r:.4f} {g:.4f} {b:.4f}', '--depth', '-z', '1', '-o', 'p%d.png']
    try:
        done = subprocess.run(argv + ['f.dvi'], cwd=workdir, capture_output=True, text=True,
                              timeout=DVIPNG_TIMEOUT, env=tex_env(), stdin=subprocess.DEVNULL,
                              preexec_fn=limit_memory)
    except subprocess.TimeoutExpired:
        return {}
    out = {}
    for page, depth in re.findall(r'\[(\d+)[^\[\]]*?depth=(-?\d+)', done.stdout):
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


def raster(workdir, dims, entries, cfg, cell, fg, deadline, probes=None):
    """Pictures for the first len(dims) entries, whose pages are in f.dvi,
    and whether the batch can be trusted (see tex_document's sentinel).

    With probes (a sentinel run), page 1 is the sentinel and item n is page
    n + 2. Results are None when dvipng did not give exactly one page per
    entry: then no page can be trusted to belong to its entry.
    """
    dpi = raster_dpi(cfg)
    found = run_dvipng(workdir, dpi, fg)
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
            results.append(None)  # drawn alone below; that clears workdir
        else:
            results.append(page_result(found.get(n + 1 + shift), dpi, entries[n][1], cfg, cell))
    for n, dim in enumerate(dims):
        if dim[3]:
            results[n] = typeset_big(workdir, entries[n], dim, dpi, cfg, cell, fg, deadline)
    return results, trusted


def typeset_big(workdir, entry, dim, dpi, cfg, cell, fg, deadline):
    """A formula too large for the batch, typeset alone at a lower resolution."""
    limit = size_limit(dim, cfg, cell)
    if limit < MIN_SCALE:
        return {'error': 'too large'}
    left = deadline - time.monotonic()
    if left <= 0:
        return {'error': 'timeout'}
    area = max(1.0, dim[0] * (dim[1] + dim[2]))
    small = max(1, math.floor(dpi * min(limit, math.sqrt(area_limit(dpi) / area))))
    dims, _, error, _ = run_latex(workdir, [entry], min(left, LATEX_TIMEOUT), None, fg)
    if error is not None:
        return {'error': error}
    found = run_dvipng(workdir, small, fg)
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


def typeset(entries, cfg, cell, fg, deadline):
    """(result, trusted) for each (tex, display) entry: one latex run, plus one per bad item.

    A result is trusted (may be cached) when it came from a run that held it
    alone, or from a batch whose two sentinel pages match.
    """
    results = [None] * len(entries)
    trusted = [False] * len(entries)
    queue = list(range(len(entries)))
    with tempfile.TemporaryDirectory(prefix='inline-math-') as workdir:

        def alone(n):
            results[n] = typeset_one(entries[n], cfg, cell, fg, deadline, workdir)
            trusted[n] = True

        while queue:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            batch = [entries[n] for n in queue]
            several = len(batch) > 1
            timeout = min(left, LATEX_TIMEOUT + 0.5 * len(batch))
            dims, bad, error, probes = run_latex(workdir, batch, timeout, guard_size(cfg, cell), fg,
                                                 sentinel=several)
            if dims:
                done, ok = raster(workdir, dims, batch, cfg, cell, fg, deadline,
                                  probes if several else None)
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


def typeset_one(entry, cfg, cell, fg, deadline, workdir):
    left = deadline - time.monotonic()
    if left <= 0:
        return {'error': 'timeout'}
    dims, _, error, _ = run_latex(workdir, [entry], min(left, LATEX_TIMEOUT), guard_size(cfg, cell), fg)
    if error is not None:
        return {'error': error}
    done, _ = raster(workdir, dims, [entry], cfg, cell, fg, deadline)
    return done[0] if done else {'error': 'dvipng failed'}


# ---------------------------------------------------------------- cache

def cache_key(latex, display, cell, fg, cfg, packages):
    """Everything a picture depends on, including the installed packages
    (installing one changes how a formula looks, or whether it works)."""
    parts = [VERSION, latex, bool(display), list(cell), fg, cfg['baseline'], cfg['ptPx'],
             cfg['minShrink'], cfg['maxColumns'], ' '.join(packages)]
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
        tmp = os.path.join(cache_dir, f'.{key}.{os.getpid()}.tmp')
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


def render_request(request):
    """The full response for a request dict."""
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
        packages = available_packages(cfg['cacheDir'])
    except RendererError as err:
        response['results'] = [{'error': str(err)} for _ in items]
        return response

    def key_of(latex, display):
        return cache_key(latex, display, cell, fg, cfg, packages)

    todo = {}  # (latex, display) -> [indices]; also dedupes
    for n, item in enumerate(items):
        item = item if isinstance(item, dict) else {}
        latex, display = item.get('latex'), bool(item.get('display'))
        hit = cache_get(cfg['cacheDir'], key_of(latex, display)) if isinstance(latex, str) else None
        if hit is not None:
            results[n] = hit
        else:
            todo.setdefault((latex, display), []).append(n)

    entries, owners = [], []
    for (latex, display), indices in todo.items():
        try:
            entries.append((prepare(latex, display), display))
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
            try:
                done = typeset(entries, cfg, cell, fg, started + DEADLINE)
            except Exception as err:  # noqa: BLE001 - reported per item, never cached
                done = [({'error': f'renderer failed: {err}'[:300]}, False)] * len(entries)
        wrote = False
        for (latex, display, indices), (result, trusted) in zip(owners, done):
            if trusted and cacheable(result):
                wrote |= cache_put(cfg['cacheDir'], key_of(latex, display), result)
            for n in indices:
                results[n] = result
        if wrote:
            cache_prune(cfg['cacheDir'])

    return response


def detect():
    """The 'auto' cell size and colour alone: cheap enough to ask every few seconds."""
    cell, fg = detect_cell('auto'), detect_fg('auto')
    return {'cell': {'width': cell[0], 'height': cell[1]}, 'fg': fg}


def check():
    """Status for a /status command: tools, a real render, cell and colour."""
    problems = missing_tools()
    try:
        have = available_packages()
    except RendererError as err:
        have = []
        problems.append(str(err))
    for name in ('preview', 'amsmath', 'amssymb'):
        if name not in have:
            problems.append(f'LaTeX package {name} not found')
    for name in [p for p in PACKAGES if p not in have] + ([] if 'CJKutf8' in have else ['CJKutf8']):
        if name not in ('amsmath', 'amssymb'):
            problems.append(f'optional LaTeX package {name} not found')
    cell, fg = detect_cell('auto'), detect_fg('auto')
    rendered = False
    if not missing_tools():
        with tempfile.TemporaryDirectory() as tmp:
            cfg = parse_options({'cell': f'{cell[0]}x{cell[1]}', 'fg': fg, 'cacheDir': tmp})
            result = render_request({'items': [{'latex': 'x^2', 'display': False}], 'options': cfg})
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
        'ok': rendered and not [p for p in problems if not p.startswith('optional')],
        'latex': bool(shutil.which('latex')),
        'dvipng': bool(shutil.which('dvipng')),
        'pil': pil,
        'cjk': 'CJKutf8' in have,
        'cell': {'width': cell[0], 'height': cell[1]},
        'fg': fg,
        'cacheDir': default_cache_dir(),
        'problems': problems,
    }


def main():
    if '--check' in sys.argv[1:]:
        print(json.dumps(check()))
        return
    if '--detect' in sys.argv[1:]:
        print(json.dumps(detect()))
        return
    try:
        request = json.loads(sys.stdin.read() or '{}')
    except ValueError as err:
        request, problem = {}, f'bad request: {err}'
    else:
        problem = None
    if not isinstance(request, dict):
        request, problem = {}, 'bad request: not an object'
    try:
        response = render_request(request)
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
    print(json.dumps(response))


if __name__ == '__main__':
    main()
