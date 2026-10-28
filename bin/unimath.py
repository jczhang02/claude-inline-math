"""Unicode math characters as the TeX commands of render.py's preamble.

SYMBOLS is generated: every character of unicode-math's table
(unicode-math-table.tex, TeX Live) whose command this preamble defines and
draws, less the ones render.UNICODE_MATH maps already, plus EXTRA (what
KaTeX maps differently, or to a command unicode-math names otherwise).
Mathematical letters and digits (U+1D400-1D7FF, and the letter-like ones
such as ℬ) are not listed: alphanumeric() derives them from their names.

Regenerate after a preamble change (it renders every candidate once):
    python3 bin/unimath.py
"""

import unicodedata

# What KaTeX (or common use) maps that unicode-math's names do not cover here.
EXTRA = {
    '┌': r'\ulcorner', '┐': r'\urcorner', '└': r'\llcorner', '┘': r'\lrcorner',
    '⎰': r'\lmoustache', '⎱': r'\rmoustache', '⟮': r'\lgroup', '⟯': r'\rgroup',
    'ð': r'\eth', '∡': r'\measuredangle', '∢': r'\sphericalangle', '⅁': r'\Game', 'Ⅎ': r'\Finv',
    'ϝ': r'\digamma', '∴': r'\therefore', '∵': r'\because', '∁': r'\complement',
    '⦃': r'\lBrace', '⦄': r'\rBrace', '⟦': r'\llbracket', '⟧': r'\rrbracket',
    '⇠': r'\dashleftarrow', '⇢': r'\dashrightarrow', '⋠': r'\npreceq', '⋡': r'\nsucceq',
    '↺': r'\circlearrowleft', '↻': r'\circlearrowright', '≆': r'\ncong',
    '⊶': r'\origof', '⊷': r'\imageof', '∌': r'\notni', '℃': r'\celsius', 'Ω': r'\Omega',
}

SYMBOLS = {
    # generated: begin
    '£': r'\mathsterling',
    '§': r'\mathsection',
    '¶': r'\mathparagraph',
    'ð': r'\eth',
    'Ω': r'\Omega',
    'ϝ': r'\digamma',
    '‵': r'\backprime',
    '€': r'\euro',
    '℃': r'\celsius',
    '℧': r'\mho',
    'Ⅎ': r'\Finv',
    'ℶ': r'\beth',
    'ℷ': r'\gimel',
    'ℸ': r'\daleth',
    '⅁': r'\Game',
    '⅄': r'\Yup',
    '↕': r'\updownarrow',
    '↖': r'\nwarrow',
    '↗': r'\nearrow',
    '↘': r'\searrow',
    '↙': r'\swarrow',
    '↚': r'\nleftarrow',
    '↛': r'\nrightarrow',
    '↞': r'\twoheadleftarrow',
    '↠': r'\twoheadrightarrow',
    '↢': r'\leftarrowtail',
    '↣': r'\rightarrowtail',
    '↤': r'\mapsfrom',
    '↩': r'\hookleftarrow',
    '↪': r'\hookrightarrow',
    '↫': r'\looparrowleft',
    '↬': r'\looparrowright',
    '↭': r'\leftrightsquigarrow',
    '↮': r'\nleftrightarrow',
    '↰': r'\Lsh',
    '↱': r'\Rsh',
    '↶': r'\curvearrowleft',
    '↷': r'\curvearrowright',
    '↺': r'\circlearrowleft',
    '↻': r'\circlearrowright',
    '↼': r'\leftharpoonup',
    '↽': r'\leftharpoondown',
    '↾': r'\upharpoonright',
    '↿': r'\upharpoonleft',
    '⇀': r'\rightharpoonup',
    '⇁': r'\rightharpoondown',
    '⇂': r'\downharpoonright',
    '⇃': r'\downharpoonleft',
    '⇄': r'\rightleftarrows',
    '⇆': r'\leftrightarrows',
    '⇇': r'\leftleftarrows',
    '⇈': r'\upuparrows',
    '⇉': r'\rightrightarrows',
    '⇊': r'\downdownarrows',
    '⇋': r'\leftrightharpoons',
    '⇌': r'\rightleftharpoons',
    '⇍': r'\nLeftarrow',
    '⇎': r'\nLeftrightarrow',
    '⇏': r'\nRightarrow',
    '⇑': r'\Uparrow',
    '⇓': r'\Downarrow',
    '⇕': r'\Updownarrow',
    '⇚': r'\Lleftarrow',
    '⇛': r'\Rrightarrow',
    '⇝': r'\rightsquigarrow',
    '⇠': r'\dashleftarrow',
    '⇢': r'\dashrightarrow',
    '⇽': r'\leftarrowtriangle',
    '⇾': r'\rightarrowtriangle',
    '⇿': r'\leftrightarrowtriangle',
    '∁': r'\complement',
    '∌': r'\notni',
    '∔': r'\dotplus',
    '∡': r'\measuredangle',
    '∢': r'\sphericalangle',
    '∣': r'\mid',
    '∤': r'\nmid',
    '∦': r'\nparallel',
    '∯': r'\oiint',
    '∰': r'\oiiint',
    '∴': r'\therefore',
    '∵': r'\because',
    '∹': r'\dashcolon',
    '∽': r'\backsim',
    '≀': r'\wr',
    '≁': r'\nsim',
    '≂': r'\eqsim',
    '≆': r'\ncong',
    '≇': r'\ncong',
    '≊': r'\approxeq',
    '≍': r'\asymp',
    '≎': r'\Bumpeq',
    '≏': r'\bumpeq',
    '≐': r'\doteq',
    '≑': r'\Doteq',
    '≒': r'\fallingdotseq',
    '≓': r'\risingdotseq',
    '≕': r'\eqcolon',
    '≖': r'\eqcirc',
    '≗': r'\circeq',
    '≦': r'\leqq',
    '≧': r'\geqq',
    '≨': r'\lneqq',
    '≩': r'\gneqq',
    '≬': r'\between',
    '≮': r'\nless',
    '≯': r'\ngtr',
    '≰': r'\nleq',
    '≱': r'\ngeq',
    '≲': r'\lesssim',
    '≳': r'\gtrsim',
    '≶': r'\lessgtr',
    '≷': r'\gtrless',
    '≺': r'\prec',
    '≻': r'\succ',
    '≼': r'\preccurlyeq',
    '≽': r'\succcurlyeq',
    '≾': r'\precsim',
    '≿': r'\succsim',
    '⊀': r'\nprec',
    '⊁': r'\nsucc',
    '⊈': r'\nsubseteq',
    '⊉': r'\nsupseteq',
    '⊋': r'\supsetneq',
    '⊎': r'\uplus',
    '⊏': r'\sqsubset',
    '⊐': r'\sqsupset',
    '⊑': r'\sqsubseteq',
    '⊒': r'\sqsupseteq',
    '⊓': r'\sqcap',
    '⊔': r'\sqcup',
    '⊖': r'\ominus',
    '⊘': r'\oslash',
    '⊙': r'\odot',
    '⊚': r'\circledcirc',
    '⊛': r'\circledast',
    '⊝': r'\circleddash',
    '⊞': r'\boxplus',
    '⊟': r'\boxminus',
    '⊠': r'\boxtimes',
    '⊡': r'\boxdot',
    '⊢': r'\vdash',
    '⊣': r'\dashv',
    '⊤': r'\top',
    '⊧': r'\models',
    '⊨': r'\vDash',
    '⊩': r'\Vdash',
    '⊪': r'\Vvdash',
    '⊬': r'\nvdash',
    '⊭': r'\nvDash',
    '⊮': r'\nVdash',
    '⊯': r'\nVDash',
    '⊲': r'\vartriangleleft',
    '⊳': r'\vartriangleright',
    '⊴': r'\trianglelefteq',
    '⊵': r'\trianglerighteq',
    '⊶': r'\origof',
    '⊷': r'\imageof',
    '⊸': r'\multimap',
    '⊺': r'\intercal',
    '⊻': r'\veebar',
    '⊼': r'\barwedge',
    '⋀': r'\bigwedge',
    '⋁': r'\bigvee',
    '⋂': r'\bigcap',
    '⋃': r'\bigcup',
    '⋇': r'\divideontimes',
    '⋈': r'\bowtie',
    '⋉': r'\ltimes',
    '⋊': r'\rtimes',
    '⋋': r'\leftthreetimes',
    '⋌': r'\rightthreetimes',
    '⋍': r'\backsimeq',
    '⋎': r'\curlyvee',
    '⋏': r'\curlywedge',
    '⋐': r'\Subset',
    '⋑': r'\Supset',
    '⋒': r'\Cap',
    '⋓': r'\Cup',
    '⋔': r'\pitchfork',
    '⋖': r'\lessdot',
    '⋗': r'\gtrdot',
    '⋘': r'\lll',
    '⋙': r'\ggg',
    '⋚': r'\lesseqgtr',
    '⋛': r'\gtreqless',
    '⋞': r'\curlyeqprec',
    '⋟': r'\curlyeqsucc',
    '⋠': r'\npreceq',
    '⋡': r'\nsucceq',
    '⋦': r'\lnsim',
    '⋧': r'\gnsim',
    '⋨': r'\precnsim',
    '⋩': r'\succnsim',
    '⋬': r'\ntrianglelefteq',
    '⋭': r'\ntrianglerighteq',
    '⌜': r'\ulcorner',
    '⌝': r'\urcorner',
    '⌞': r'\llcorner',
    '⌟': r'\lrcorner',
    '⌢': r'\frown',
    '⌣': r'\smile',
    '⌽': r'\obar',
    '⎰': r'\lmoustache',
    '⎱': r'\rmoustache',
    '┌': r'\ulcorner',
    '┐': r'\urcorner',
    '└': r'\llcorner',
    '┘': r'\lrcorner',
    '△': r'\bigtriangleup',
    '▴': r'\blacktriangle',
    '▵': r'\vartriangle',
    '▶': r'\blacktriangleright',
    '▷': r'\triangleright',
    '▽': r'\bigtriangledown',
    '▾': r'\blacktriangledown',
    '▿': r'\triangledown',
    '◀': r'\blacktriangleleft',
    '◁': r'\triangleleft',
    '◫': r'\boxbar',
    '★': r'\bigstar',
    '♠': r'\spadesuit',
    '♡': r'\heartsuit',
    '♢': r'\diamondsuit',
    '♣': r'\clubsuit',
    '♭': r'\flat',
    '♮': r'\natural',
    '♯': r'\sharp',
    '✓': r'\checkmark',
    '✠': r'\maltese',
    '⟂': r'\perp',
    '⟅': r'\lbag',
    '⟆': r'\rbag',
    '⟋': r'\diagup',
    '⟍': r'\diagdown',
    '⟦': r'\llbracket',
    '⟧': r'\rrbracket',
    '⟮': r'\lgroup',
    '⟯': r'\rgroup',
    '⟵': r'\longleftarrow',
    '⟷': r'\longleftrightarrow',
    '⟸': r'\Longleftarrow',
    '⟻': r'\longmapsfrom',
    '⟼': r'\longmapsto',
    '⟽': r'\Longmapsfrom',
    '⟾': r'\Longmapsto',
    '⤆': r'\Mapsfrom',
    '⤇': r'\Mapsto',
    '⦃': r'\lBrace',
    '⦄': r'\rBrace',
    '⦇': r'\llparenthesis',
    '⦈': r'\rrparenthesis',
    '⦸': r'\obslash',
    '⧀': r'\olessthan',
    '⧁': r'\ogreaterthan',
    '⧅': r'\boxbslash',
    '⧆': r'\boxast',
    '⧇': r'\boxcircle',
    '⧈': r'\boxbox',
    '⨀': r'\bigodot',
    '⨁': r'\bigoplus',
    '⨂': r'\bigotimes',
    '⨄': r'\biguplus',
    '⨅': r'\bigsqcap',
    '⨆': r'\bigsqcup',
    '⨉': r'\bigtimes',
    '⨌': r'\iiiint',
    '⨝': r'\Join',
    '⨿': r'\amalg',
    '⩞': r'\doublebarwedge',
    '⩴': r'\Coloneq',
    '⪅': r'\lessapprox',
    '⪆': r'\gtrapprox',
    '⪇': r'\lneq',
    '⪈': r'\gneq',
    '⪉': r'\lnapprox',
    '⪊': r'\gnapprox',
    '⪋': r'\lesseqqgtr',
    '⪌': r'\gtreqqless',
    '⪕': r'\eqslantless',
    '⪖': r'\eqslantgtr',
    '⪯': r'\preceq',
    '⪰': r'\succeq',
    '⪵': r'\precneqq',
    '⪶': r'\succneqq',
    '⪷': r'\precapprox',
    '⪸': r'\succapprox',
    '⪹': r'\precnapprox',
    '⪺': r'\succnapprox',
    '⪿': r'\subsetplus',
    '⫀': r'\supsetplus',
    '⫅': r'\subseteqq',
    '⫆': r'\supseteqq',
    '⫋': r'\subsetneqq',
    '⫌': r'\supsetneqq',
    '⫴': r'\interleave',
    '⫼': r'\biginterleave',
    '⫽': r'\sslash',
    '⫾': r'\talloblong',
    # generated: end
}

GREEK = {
    'ALPHA': 'alpha', 'BETA': 'beta', 'GAMMA': 'gamma', 'DELTA': 'delta', 'EPSILON': 'varepsilon',
    'ZETA': 'zeta', 'ETA': 'eta', 'THETA': 'theta', 'IOTA': 'iota', 'KAPPA': 'kappa', 'LAMDA': 'lambda',
    'MU': 'mu', 'NU': 'nu', 'XI': 'xi', 'OMICRON': None, 'PI': 'pi', 'RHO': 'rho', 'FINAL SIGMA': 'varsigma',
    'SIGMA': 'sigma', 'TAU': 'tau', 'UPSILON': 'upsilon', 'PHI': 'varphi', 'CHI': 'chi', 'PSI': 'psi',
    'OMEGA': 'omega', 'EPSILON SYMBOL': 'epsilon', 'THETA SYMBOL': 'vartheta', 'KAPPA SYMBOL': 'varkappa',
    'PHI SYMBOL': 'phi', 'RHO SYMBOL': 'varrho', 'PI SYMBOL': 'varpi', 'NABLA': 'nabla',
    'PARTIAL DIFFERENTIAL': 'partial', 'DOTLESS I': 'imath', 'DOTLESS J': 'jmath',
}
CAPITAL_GREEK = {'GAMMA', 'DELTA', 'THETA', 'LAMDA', 'XI', 'PI', 'SIGMA', 'UPSILON', 'PHI', 'PSI', 'OMEGA'}
CAPITAL_GREEK_LATIN = {'ALPHA': 'A', 'BETA': 'B', 'EPSILON': 'E', 'ZETA': 'Z', 'ETA': 'H', 'IOTA': 'I',
                       'KAPPA': 'K', 'MU': 'M', 'NU': 'N', 'OMICRON': 'O', 'RHO': 'P', 'TAU': 'T', 'CHI': 'X'}
DIGITS = 'ZERO ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE'.split()
# The style words of a name -> (Latin and digits, Greek), each '{}' for the letter.
STYLES = {
    'BOLD': (r'\mathbf{{{}}}', r'\boldsymbol{{{}}}'),
    'ITALIC': ('{}', '{}'),
    'BOLD ITALIC': (r'\boldsymbol{{{}}}', r'\boldsymbol{{{}}}'),
    'SCRIPT': (r'\mathscr{{{}}}', None),
    'BOLD SCRIPT': (r'\bm{{\mathscr{{{}}}}}', None),
    'FRAKTUR': (r'\mathfrak{{{}}}', None),
    'BLACK-LETTER': (r'\mathfrak{{{}}}', None),
    'BOLD FRAKTUR': (r'\bm{{\mathfrak{{{}}}}}', None),
    'DOUBLE-STRUCK': (r'\mathbb{{{}}}', None),
    'SANS-SERIF': (r'\mathsf{{{}}}', None),
    'SANS-SERIF BOLD': (r'\bm{{\mathsf{{{}}}}}', r'\boldsymbol{{{}}}'),
    'SANS-SERIF ITALIC': (r'\mathsfit{{{}}}', None),
    'SANS-SERIF BOLD ITALIC': (r'\bm{{\mathsfit{{{}}}}}', r'\boldsymbol{{{}}}'),
    'MONOSPACE': (r'\mathtt{{{}}}', None),
}


def alphanumeric(c):
    """TeX for a mathematical letter or digit (𝐀, 𝑥, 𝔄, 𝟙, 𝛂, ℬ...), or None."""
    code = ord(c)
    if not (0x1D400 <= code <= 0x1D7FF or 0x2100 <= code <= 0x214F):
        return None
    name = unicodedata.name(c, '')
    if code < 0x1D400:  # letter-like symbols: SCRIPT CAPITAL B, BLACK-LETTER CAPITAL H...
        name = 'MATHEMATICAL ' + name
    if name == 'MATHEMATICAL PLANCK CONSTANT':
        return 'h'
    for style in sorted(STYLES, key=len, reverse=True):
        head = f'MATHEMATICAL {style} '
        if not name.startswith(head):
            continue
        rest = name[len(head):]
        latin, greek = STYLES[style]
        if rest.startswith(('CAPITAL ', 'SMALL ')) and len(rest.split(' ', 1)[1]) == 1:
            capital, letter = rest.startswith('CAPITAL '), rest[-1]
            letter = letter if capital else letter.lower()
            if not capital and 'SCRIPT' in style:  # no script lower case in these fonts
                return latin.replace('mathscr', 'mathit').format(letter)
            if not capital and style == 'DOUBLE-STRUCK':
                return r'\Bbbk' if letter == 'k' else None
            return latin.format(letter)
        if rest.startswith('DIGIT '):
            digit = str(DIGITS.index(rest[6:])) if rest[6:] in DIGITS else None
            if digit is None:
                return None
            return r'\mathds{{{}}}'.format(digit) if style == 'DOUBLE-STRUCK' else latin.format(digit)
        if greek is None:
            return None
        capital = rest.startswith('CAPITAL ')
        key = rest.split(' ', 1)[1] if rest.startswith(('CAPITAL ', 'SMALL ')) else rest
        if capital and key in CAPITAL_GREEK_LATIN:
            return (r'\mathbf{{{}}}' if 'BOLD' in style else '{}').format(CAPITAL_GREEK_LATIN[key])
        if capital and key in CAPITAL_GREEK:
            word = '\\' + ('Lambda' if key == 'LAMDA' else key.capitalize())
            if style.endswith('ITALIC'):
                word = r'\var' + word[1:]
            return greek.format(word) if 'BOLD' in style or 'SANS' in style else word
        if capital and key == 'THETA SYMBOL':
            return greek.format(r'\Theta') if 'BOLD' in style else r'\Theta'
        word = GREEK.get(key)
        if word is None or capital:
            return None
        return greek.format('\\' + word)
    return None


def lookup(c):
    """TeX for a Unicode math character from these tables, or None."""
    return SYMBOLS.get(c) or alphanumeric(c)


# ---------------------------------------------------------------- generator

def generate():
    """Rewrite SYMBOLS in this file from unicode-math's table and the preamble."""
    import base64
    import io
    import os
    import re
    import subprocess
    import sys
    import tempfile

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import render
    from PIL import Image

    table = subprocess.run(['kpsewhich', 'unicode-math-table.tex'], capture_output=True, text=True).stdout.strip()
    with open(table, encoding='utf-8') as f:
        entries = re.findall(r'\\UnicodeMathSymbol\{"([0-9A-F]+)\}\{\\([A-Za-z]+)\s*\}\{\\(\w+)\}', f.read())
    skip_classes = {'mathaccent', 'mathaccentwide', 'mathbotaccent', 'mathbotaccentwide', 'mathaccentoverlay',
                    'mathradical', 'mathover', 'mathunder'}
    candidates = {}
    for code, command, kind in entries:
        c = chr(int(code, 16))
        if int(code, 16) < 0xA0 or kind in skip_classes or c in render.UNICODE_MATH or alphanumeric(c):
            continue
        candidates.setdefault(c, '\\' + command)
    for c, tex in EXTRA.items():
        candidates[c] = tex
    work = tempfile.mkdtemp(prefix='unimath-')
    keep = {}
    items = sorted(candidates.items())
    for start in range(0, len(items), 60):
        batch = items[start:start + 60]
        request = {'items': [{'latex': tex, 'display': False} for _, tex in batch],
                   'options': {'fg': '#000000', 'cell': '19x42', 'cacheDir': work}}
        results = render.render_request(request, cached=False)['results']
        for (c, tex), result in zip(batch, results):
            if 'png' not in result:
                continue
            with Image.open(io.BytesIO(base64.b64decode(result['png']))) as img:
                if img.convert('RGBA').getchannel('A').getbbox():
                    keep[c] = tex
    lines = []
    for c, tex in sorted(keep.items()):
        shown = c if unicodedata.category(c)[0] not in 'CZM' else f'\\u{ord(c):04x}'
        lines.append(f"    '{shown}': r'{tex}',")
    path = os.path.abspath(__file__)
    with open(path, encoding='utf-8') as f:
        text = f.read()
    head, rest = text.split('    # generated: begin\n', 1)
    _, tail = rest.split('    # generated: end\n', 1)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(head + '    # generated: begin\n' + '\n'.join(lines) + '\n    # generated: end\n' + tail)
    print(f'{len(keep)} of {len(candidates)} characters drawn')


if __name__ == '__main__':
    generate()
