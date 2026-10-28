"""Write katex.txt from KaTeX's documentation pages.

Usage:
    curl -sLo support_table.html https://katex.org/docs/support_table.html
    curl -sLo supported.html https://katex.org/docs/supported.html
    python3 -I katex_from_docs.py support_table.html supported.html > katex.txt

Each line is one formula KaTeX rendered on those pages (the TeX of its
MathML annotation): first every one of the support table, in table order,
then those of the supported-functions page not seen yet. White space is
joined; the table's \\VERT (a macro the page defined for |) is written as |,
and its definition line dropped; formulas with environments that need a
whole equation, or \\tag, are display formulas ("$$ ").
"""

import html
import re
import sys

DISPLAY = re.compile(r'\\tag|\\begin\{(align|gather|equation|alignat|CD|multline|split|eqnarray)')
HEADER = '''# KaTeX: every formula of https://katex.org/docs/support_table.html (the
# KaTeX rendering of each supported command, in table order) and then those of
# https://katex.org/docs/supported.html not already listed. Fetched 2026-10-27;
# made by katex_from_docs.py.
# One formula per line, white space joined; "$$ " starts a display formula.
'''


def annotations(page):
    return [html.unescape(a) for a in re.findall(r'<annotation encoding="application/x-tex">(.*?)</annotation>',
                                                 page, re.S)]


def main(table_path, supported_path):
    with open(table_path, encoding='utf-8') as f:
        table = f.read()
    with open(supported_path, encoding='utf-8') as f:
        supported = f.read()
    seen, out = set(), []
    for formula in annotations(table) + annotations(supported):
        formula = ' '.join(formula.replace(' ', ' ').replace('\\VERT', '|').split())
        if not formula or formula in seen or formula == r'\gdef|{|}':
            continue
        seen.add(formula)
        out.append(('$$ ' if DISPLAY.search(formula) else '') + formula)
    sys.stdout.write(HEADER + '\n'.join(out) + '\n')


if __name__ == '__main__':
    main(*sys.argv[1:3])
