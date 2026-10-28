"""Tests for render.py, and for render.py behind renderd.py (warm TeX).

Run: python3 -I -m unittest discover -s bin   (from the mod folder)

Every test fixes the cell size, the colour and a private cache folder, so
results do not depend on this desktop. All tests share one private format
folder (the format is built once). Tests that need TeX are skipped when
latex or dvipng is missing. The client and daemon tests are in
test_daemon.py.
"""

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import render
import renderd
import unimath

HAVE_TEX = bool(shutil.which('latex') and shutil.which('dvipng'))
CW, CH = 19, 42
FORMATS = None  # the shared format folder


def setUpModule():
    global FORMATS
    FORMATS = tempfile.mkdtemp(prefix='inline-math-test-fmt-')
    os.environ['INLINE_MATH_FORMAT_DIR'] = FORMATS
    if HAVE_TEX:
        with tempfile.TemporaryDirectory() as tmp:
            render.tex_setup(render.parse_options({'cacheDir': tmp}))


def tearDownModule():
    os.environ.pop('INLINE_MATH_FORMAT_DIR', None)
    shutil.rmtree(FORMATS, ignore_errors=True)


def options(cache_dir, **more):
    return dict({'cell': f'{CW}x{CH}', 'fg': '#000000', 'cacheDir': cache_dir}, **more)


class Spy:
    """Counts the programs render.py runs, and still runs them."""

    def __init__(self):
        self.calls = []
        self.argvs = []
        self.real = subprocess.run

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(argv[0])
        self.argvs.append(list(argv))
        return self.real(argv, *args, **kwargs)

    def builds(self):
        return sum(1 for argv in self.argvs if argv[0] == 'latex' and '-ini' in argv)

    def count(self, program):
        return self.calls.count(program)


def refuse(*args, **kwargs):
    raise AssertionError(f'unexpected program run: {args[0] if args else kwargs}')


class Case(unittest.TestCase):
    """Renders with render.py's own Session: a fresh latex and dvipng per run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='inline-math-test-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def session(self, setup):
        return render.Session(setup)

    def render(self, *items, **more):
        request = {
            'items': [{'latex': latex, 'display': display} for latex, display in items],
            'options': options(self.tmp, **more),
        }
        return render.render_request(request, new_session=self.session)

    def image(self, result):
        from PIL import Image
        return Image.open(io.BytesIO(base64.b64decode(result['png'])))

    def count_latex(self):
        """A context that counts latex runs (in .calls) and still runs them."""
        spy = Spy()
        return mock.patch.object(render.subprocess, 'run', spy), spy

    def no_tex(self):
        return mock.patch.object(render.subprocess, 'run', refuse)

    def patch_dvipng(self, wrap):
        """Run dvipng through wrap(real)(workdir, dpi, fg)."""
        return mock.patch.object(render, 'run_dvipng', wrap(render.run_dvipng))


class WarmCase(Case):
    """Renders as renderd.py does: warm latex processes and one dvipng (no socket)."""

    @classmethod
    def setUpClass(cls):
        folder = tempfile.mkdtemp(prefix='inline-math-test-run-')
        cls.daemon = renderd.Daemon(folder, 'test')
        cls.daemon.open_work()

    @classmethod
    def tearDownClass(cls):
        cls.daemon.shutdown()
        shutil.rmtree(cls.daemon.folder, ignore_errors=True)

    def session(self, setup):
        return self.daemon.session(setup)

    def count_latex(self):
        spy = Spy()
        real = renderd.Warm.run

        def run(warm, body, timeout):
            spy.calls.append('latex')
            return real(warm, body, timeout)

        return mock.patch.object(renderd.Warm, 'run', run), spy

    def no_tex(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(render.subprocess, 'run', refuse))
        stack.enter_context(mock.patch.object(renderd.Warm, 'run', refuse))
        stack.enter_context(mock.patch.object(renderd.Dvipng, 'run', refuse))
        return stack

    def patch_dvipng(self, wrap):
        unpatched = renderd.Dvipng.run
        real = wrap(lambda workdir, dpi, fg: unpatched(self.daemon.dvipng, workdir, dpi, fg))
        return mock.patch.object(renderd.Dvipng, 'run', lambda me, workdir, dpi, fg: real(workdir, dpi, fg))

    def image(self, result):
        from PIL import Image
        return Image.open(io.BytesIO(base64.b64decode(result['png'])))


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class BatchTest(Case):
    def test_results_in_item_order_from_one_latex_run(self):
        patch, spy = self.count_latex()
        with patch:
            response = self.render(('x', False), ('x+y+z+w+v', False), (r'\frac{a+b}{c}', False),
                                   ('x+y', False))
        results = response['results']
        self.assertEqual(spy.count('latex'), 1)
        self.assertTrue(all('png' in r for r in results), results)
        self.assertLess(results[0]['columns'], results[3]['columns'])
        self.assertLess(results[3]['columns'], results[1]['columns'])
        self.assertEqual(results[2]['rows'], 2)
        self.assertEqual(response['cell'], {'width': CW, 'height': CH})
        self.assertEqual(response['fg'], '#000000')

    def test_bad_formula_fails_alone(self):
        patch, spy = self.count_latex()
        with patch:
            response = self.render(('x', False), (r'\nosuchmacro{1}', False), ('y^2', False),
                                   (r'\frac{1}{2', False), ('z', False))
        results = response['results']
        self.assertIn('png', results[0])
        self.assertTrue(results[1]['error'].startswith('! Undefined control sequence'), results[1])
        self.assertIn(r'\nosuchmacro', results[1]['error'])
        self.assertIn('png', results[2])
        self.assertIn('error', results[3])
        self.assertIn('png', results[4])
        # The batch, the bad item alone (an item before it might have caused
        # its error), then the good items again under both sentinels.
        self.assertEqual(spy.count('latex'), 3)

    def test_two_bad_formulas(self):
        results = self.render((r'\bad', False), ('a', False), (r'x^^', False), (r'\worse', False),
                              ('b', True))['results']
        self.assertEqual(['error' in r for r in results], [True, False, True, True, False])

    def test_bad_neighbour_does_not_change_a_picture(self):
        alone = self.render(('a+b', False))['results'][0]
        shutil.rmtree(self.tmp)
        mixed = self.render((r'\oops', False), ('a+b', False))['results'][1]
        self.assertEqual(alone, mixed)

    def test_duplicates_are_typeset_once(self):
        patch, spy = self.count_latex()
        with patch:
            results = self.render(('q', False), ('q', False), ('q', True))['results']
        self.assertEqual(results[0], results[1])
        self.assertEqual(spy.count('latex'), 1)


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class CacheTest(Case):
    def test_hit_runs_no_tex(self):
        items = [('x^2', False), (r'\sum_{i=1}^n i', True), (r'\undefinedthing', False)]
        first = self.render(*items)
        with self.no_tex():
            second = self.render(*items)
        self.assertEqual(first, second)
        self.assertIn('error', second['results'][2])

    def test_key_depends_on_colour_and_cell(self):
        black = self.render(('x', False))['results'][0]
        white = self.render(('x', False), fg='#ffffff')['results'][0]
        small = self.render(('x', False), cell='10x20')['results'][0]
        self.assertNotEqual(black['png'], white['png'])
        self.assertEqual(self.image(small).size, (small['columns'] * 10, small['rows'] * 20))

    def test_files_are_complete_json(self):
        self.render(('a', False), ('b', False))
        names = [name for name in os.listdir(self.tmp) if name.endswith('.json')]
        self.assertEqual(len(names), 2)
        for name in names:
            self.assertTrue(name.endswith('.json'))
            with open(os.path.join(self.tmp, name)) as f:
                self.assertIn('png', json.load(f))

    def test_timeouts_are_not_cached(self):
        self.assertFalse(render.cacheable({'error': 'timeout'}))
        self.assertFalse(render.cacheable({'error': 'latex not found (install TeX Live with dvipng)'}))
        self.assertTrue(render.cacheable({'error': '! Missing } inserted.'}))
        self.assertTrue(render.cacheable({'png': '', 'columns': 1, 'rows': 1, 'textRow': 0}))


class PruneTest(Case):
    def test_oldest_entries_go_first(self):
        now = time.time()
        for n in range(30):
            path = os.path.join(self.tmp, f'{n:02d}.json')
            with open(path, 'w') as f:
                f.write('{}')
            os.utime(path, (now - 1000 + n, now - 1000 + n))
        render.cache_prune(self.tmp, max_entries=10)
        left = sorted(os.listdir(self.tmp))
        self.assertEqual(len(left), 8)
        self.assertEqual(left[0], '22.json')

    def test_size_bound(self):
        for n in range(5):
            with open(os.path.join(self.tmp, f'{n}.json'), 'w') as f:
                f.write('x' * 1000)
        render.cache_prune(self.tmp, max_entries=100, max_bytes=2500)
        self.assertLessEqual(len(os.listdir(self.tmp)), 2)


class SecurityTest(Case):
    UNSAFE = [
        r'\input{/etc/passwd}', r'\include{x}', r'\openin1=/etc/passwd', r'\read16 to\x',
        r'\immediate\write18{rm -rf ~}', r'\openout1=x.tex', r'\catcode`\@=11',
        r'\def\x{\x}\x', r'\let\frac\sqrt', r'\global\let\a\b', r'\newcommand\x{1}',
        r'\renewcommand\frac{}', r'\csname input\endcsname{x}', r'^^5cinput{x}',
        r'\expandafter\input', r'\scantokens{x}', r'\special{psfile=x.ps}',
        r'x \end{preview}\begin{preview} y', r'\end{document}', r'a $ b', r'\begin{CJK}{UTF8}{x}',
        r'\pdfprimitive\input', r'\everymath{x}', r'\usepackage{x}', r'x}\input{y', r'{x',
        r'\loop\iftrue\repeat', r'\fontdimen2\font=0pt', r'\(x\)', 'x' * 4001, '', '   ',
        r'x\endpreview\preview y', r'x\endCJK y', r'\enddocument',
        # Commands that run another command by its name, or change things for
        # the formulas after this one.
        r'\begin{gdef}\pi{3}\end{gdef}x', r'\UseName{gdef}\pi{3}', r'\ExpandArgs{c}\gdef{pi}{3}',
        r'\AddToHook{env/preview/begin}{x}', r'\NewCommandCopy\pi\infty', r'\RenewCommandCopy\pi\infty',
        r'\NewDocumentEnvironment{x}{}{}{}', r'\DeclarePairedDelimiterX\x[1]{(}{)}{#1}',
        r'\AtEndDocument{x}', r'\xglobal\color{red}', r'\globaldefs=1', r'\ifcsname x\endcsname\fi',
        r'\newgathered{x}{}{}{}', r'\pagecolor{red}', r'\setcounter{page}{3}', r'x\egroup y',
        'x \\end{pre%\nview}\\begin{preview} y', r'\begin x', r'\begin {foo}x\end{foo}',
        # What the KaTeX and MathJax rewrites let through must still be checked.
        r'\href{x}{\input{/etc/passwd}}', r'\htmlClass{c}{\write18{x}}', r'\text{$\input{x}$}',
        r'\text{$}\input{x}{$}', r'\text{\(x}\)', r'\colorbox{red}{$\openout1=x$}', r'\scalebox{2}{\input{x}}',
        r'\verb|x|\special{x}', r'\url{x}\input{y}', r'\pgfsys@invoke{x}', r'\tikz \draw (0,0);',
        r'\tikzset{x/.code={y}}', r'\pgfkeys{/x/.code=y}', r'\bbox[red]{\input{x}}', r'\enclose{box}{\read16}',
        r'\begin{tikzcd}[execute at begin picture={x}] A \end{tikzcd}',
        r'\begin{tikzcd} A \arrow[r, "/.code={x}"] & B \end{tikzcd}',
        r'\begin{tikzcd}[remember picture] A \end{tikzcd}', r'\begin{tikzcd}[overlay] A \end{tikzcd}',
        r'\toggle{\input{x}}{y}\endtoggle', r'\matrix{\input x}', r'\begin{empheq}[box=\input]{align}a\end{empheq}',
    ]

    def test_rejected_before_tex(self):
        with mock.patch.object(render.subprocess, 'run', refuse):
            results = self.render(*[(latex, False) for latex in self.UNSAFE])['results']
        for latex, result in zip(self.UNSAFE, results):
            self.assertIn('error', result, latex[:40])

    def test_ordinary_formulas_pass(self):
        for latex in [r'\frac{a}{b}', r'\{x\}', r'50\%', r'\$5', r'\text{if } x \\ y',
                      r'\left\{ \begin{array}{cc} 1 & 2 \end{array} \right.', r'\sqrt[3]{x}',
                      r'\mathrm{d}x', r'\operatorname{Var}(X)', r'\\', r'\begin{pmatrix*}[r] 1 \end{pmatrix*}',
                      r'\begin{dcases} 1 & x \end{dcases}', 'a % a comment\n + b', r'\newline']:
            render.prepare(latex, True)

    def test_web_arguments_never_reach_tex(self):
        # A URL, CSS or HTML class is dropped (or shown as plain characters) before TeX sees anything.
        for latex in [r'\href{\input{/etc/passwd}}{x}', r'\htmlStyle{\write18{rm}}{x}', r'\class{\catcode}{x}',
                      r'\require{\input{x}} x', r'\texttip{x}{\input{y}}', r'\data{\def}{x}']:
            tex = render.prepare(latex, False)
            self.assertNotIn(('cs', 'input'), render.tokens(tex), latex)
            self.assertIn('x', tex)
        for latex in [r'\url{\input{/etc/passwd}%#&_^~$}', r'\verb!\input{x}$%!', r'\unicode{x5C}']:
            tex = render.prepare(latex, False)
            names = {value for kind, value in render.tokens(tex) if kind == 'cs'}
            self.assertLessEqual(names, {'text', 'texttt', 'char'}, latex)

    def test_dollars_only_inside_text(self):
        self.assertIn(r'\ensuremath{x>0}', render.prepare(r'\text{if $x>0$}', False).replace(' ', ''))
        self.assertIn(r'\ensuremath{y}', render.prepare(r'\colorbox{red}{\(y\)}', False).replace(' ', ''))
        for latex in [r'a $ b', r'{a $ b}', r'\text{$x}', r'\text{a} $x$', r'\mathrm{$x$}']:
            with self.assertRaises(render.Unsafe, msg=latex):
                render.prepare(latex, False)

    def test_diagram_options_that_run_code(self):
        render.prepare(r'\begin{tikzcd}[column sep=large] A \arrow[r, "f", bend left] & B \end{tikzcd}', True)
        for latex in [r'\begin{tikzcd}[execute at end picture=x] A \end{tikzcd}',
                      r'\begin{tikzcd} A \arrow[r, a/.style={b}] & B \end{tikzcd}',
                      r'\begin{tikzcd}[/utils/exec=x] A \end{tikzcd}']:
            with self.assertRaises(render.Unsafe, msg=latex):
                render.prepare(latex, True)

    def test_comments_are_gone_before_the_checks(self):
        self.assertEqual(render.strip_comments('a % x\n   + b'), 'a + b')
        self.assertEqual(render.strip_comments(r'50\% of x'), r'50\% of x')
        self.assertEqual(render.strip_comments('a % to the end'), 'a ')

    def test_broken_unicode_fails_alone(self):
        with mock.patch.object(render.subprocess, 'run', refuse):
            results = self.render(('a\ud83d', False))['results']
        self.assertIn('error', results[0])

    def test_latex_runs_sandboxed(self):
        env = render.tex_env()
        self.assertEqual(env['openin_any'], 'p')
        self.assertEqual(env['openout_any'], 'p')
        self.assertEqual(env['shell_escape'], 'f')
        self.assertNotIn('TEXINPUTS', env)


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class IsolationTest(Case):
    """What one formula does must not reach another, even past check_safe."""

    def test_sentinel_catches_a_global_change(self):
        clean = self.render((r'\pi r^2', False))['results'][0]
        shutil.rmtree(self.tmp)
        with mock.patch.object(render, 'check_safe', lambda toks, latex: None):
            results = self.render((r'\gdef\pi{3}x', False), (r'\pi r^2', False), ('y', False))['results']
        self.assertEqual(results[1], clean)
        with self.no_tex():
            again = self.render((r'\pi r^2', False))['results'][0]
        self.assertEqual(again, clean)

    def test_an_error_caused_by_another_item_is_not_kept(self):
        with mock.patch.object(render, 'check_safe', lambda toks, latex: None):
            results = self.render(('a', False), (r'\gdef\frac{\undefined}x', False), (r'\frac12', False))['results']
        self.assertIn('png', results[2])

    def test_colour_definitions_stay_with_their_formula(self):
        clean = self.render((r'\textcolor{red}{y}', False))['results'][0]
        shutil.rmtree(self.tmp)
        results = self.render((r'\definecolor{red}{HTML}{0000FF}x', False), (r'\textcolor{red}{y}', False))['results']
        self.assertEqual(results[1], clean)

    def test_log_flood_is_cut_short(self):
        flood = r'\loop\message{' + 'x' * 200 + r'}\iftrue\repeat'
        started = time.monotonic()
        with mock.patch.object(render, 'check_safe', lambda toks, latex: None):
            results = self.render((flood, False), ('a', False), ('b', False))['results']
        self.assertLess(time.monotonic() - started, 8)
        self.assertIn('error', results[0])
        self.assertIn('png', results[1])
        self.assertIn('png', results[2])

    def test_huge_area_fails_alone_or_shrinks(self):
        results = self.render(('a', False), (r'\rule{1400pt}{3100pt}', False), ('b', False))['results']
        self.assertIn('png', results[0])
        self.assertIn('png', results[2])
        self.assertTrue('png' in results[1] or results[1] == {'error': 'too large'}, results[1])

    def test_one_failing_page_fails_alone(self):
        real = render.picture

        def broken(path, depth, ratio, display, cfg, cell):
            if path.endswith('p3.png'):
                raise OSError('broken page')
            return real(path, depth, ratio, display, cfg, cell)

        with mock.patch.object(render, 'picture', broken):
            results = self.render(('a', False), ('b', False), ('c', False))['results']
        self.assertEqual(['png' in r for r in results], [True, False, True])
        self.assertTrue(results[1]['error'].startswith('renderer'))


class KpseRefreshTest(unittest.TestCase):
    """In the daemon, an expired kpsewhich answer is used once more while a thread asks again."""

    def test_refresh_behind(self):
        files = ('amsmath.sty',)
        calls = []

        def query(found, cache_dir, now, memo_ok=True):
            calls.append(memo_ok)
            with render._found_lock:
                render._found[found] = (now, {})
            return {}

        with mock.patch.object(render, '_kpse_query', query), mock.patch.dict(render._found, clear=True):
            render._found[files] = (time.time() - render.PACKAGES_TTL - 1, {'amsmath.sty': render.__file__})
            with mock.patch.object(render, 'REFRESH_BEHIND', True):
                self.assertEqual(render.kpse_find(files), {'amsmath.sty': render.__file__})
                self.assertTrue(wait(lambda: calls == [False]), calls)
            render._found[files] = (time.time() - render.PACKAGES_TTL - 1, {'amsmath.sty': render.__file__})
            self.assertEqual(render.kpse_find(files), {})  # outside the daemon: asked at once
            self.assertEqual(calls, [False, True])


def wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not predicate():
        time.sleep(0.01)
    return predicate()


class PackagesTest(Case):
    def setUp(self):
        super().setUp()
        saved = dict(render._found)
        render._found.clear()
        self.addCleanup(lambda: (render._found.clear(), render._found.update(saved)))

    def test_one_missing_package_keeps_the_others(self):
        sty = {name: os.path.join(self.tmp, name) for name in ('amsmath.sty', 'preview.sty')}
        for path in sty.values():
            open(path, 'w').close()
        found = subprocess.CompletedProcess([], 1, stdout='\n'.join(sty.values()) + '\n', stderr='')
        with mock.patch.object(render.subprocess, 'run', return_value=found):
            self.assertEqual(render.available_packages(self.tmp), ['amsmath', 'preview'])
        render._found.clear()
        with mock.patch.object(render.subprocess, 'run', refuse):  # from the memo file
            self.assertEqual(render.available_packages(self.tmp), ['amsmath', 'preview'])
        os.remove(sty['amsmath.sty'])  # a remembered file that is gone: ask again
        render._found.clear()
        found = subprocess.CompletedProcess([], 1, stdout=sty['preview.sty'] + '\n', stderr='')
        with mock.patch.object(render.subprocess, 'run', return_value=found):
            self.assertEqual(render.available_packages(self.tmp), ['preview'])

    def test_no_kpsewhich_is_a_renderer_error(self):
        with mock.patch.object(render.subprocess, 'run', side_effect=FileNotFoundError('kpsewhich')):
            results = self.render(('x', False))['results']
        self.assertTrue(results[0]['error'].startswith('renderer'), results[0])
        self.assertFalse(render.cacheable(results[0]))


class PrepareTest(unittest.TestCase):
    def test_unicode_symbols(self):
        tex = render.prepare('α ≤ β − γ × 2 ∈ ℝ', False)
        for part in (r'\alpha', r'\le', r'\beta', '-', r'\times', r'\in', r'\mathbb{R}'):
            self.assertIn(part, tex)
        self.assertTrue(tex.isascii())

    def test_combining_accents(self):
        self.assertEqual(render.prepare('x\u0302 + v\u20d7', False).replace(' ', ''), r'\hat{x}+\vec{v}')

    def test_scripts(self):
        self.assertEqual(render.prepare('x²³', False), 'x^{23}')
        self.assertEqual(render.prepare('a₁', False), 'a_{1}')

    def test_bare_cjk_goes_into_text(self):
        self.assertEqual(render.prepare('速度 = v', False).replace(' ', ''), r'\text{速度}=v')
        self.assertIn(r'\text {时间}', render.prepare(r'\text{时间}', False))

    def test_greek_inside_text(self):
        self.assertIn(r'\ensuremath{\alpha}', render.prepare(r'\text{angle α}', False))

    def test_whole_environments(self):
        self.assertIn(r'\begin {aligned}', render.prepare(r'\begin{align*} a &= b \end{align*}', True))
        self.assertIn(r'\begin {gathered}', render.prepare(r'\begin{gather} a \\ b \end{gather}', True))
        self.assertEqual(render.prepare(r'\begin{equation} e=mc^2 \end{equation}', True).strip(), 'e=mc^2')
        self.assertIn('(3)', render.prepare(r'x \tag{3}', True))
        self.assertNotIn('label', render.prepare(r'x \label{eq:1}', True))

    def test_nested_whole_environments(self):
        tex = render.prepare(r'\begin{equation}\begin{split} a &= b \\ c &= d \end{split}\end{equation}', True)
        self.assertTrue(tex.startswith(r'\begin {aligned}'), tex)
        self.assertNotIn('split', tex)
        two = r'\begin{align}a\end{align}\begin{align}b\end{align}'
        self.assertIn(r'\begin {align}', render.prepare(two, True))

    def test_whole_environment_with_trailing_punctuation(self):
        # layout.ts moves a sentence's "." after $$...$$ into the formula.
        tex = render.prepare(r'\begin{align}a&=b\end{align}\,{.}', True)
        self.assertTrue(tex.startswith(r'\begin {aligned}'), tex)
        self.assertTrue(tex.rstrip().endswith('{.}'), tex)
        self.assertEqual(render.prepare(r'\begin{equation}x\end{equation}\text{。}', True).replace(' ', ''), r'x\text{。}')

    def test_units_are_not_definitions(self):
        self.assertIn(r'\newton', render.prepare(r'\SI{5}{\newton}', False))

    def test_indicator_one(self):
        if 'dsfont' not in render.available_packages():
            self.skipTest('dsfont is not installed')
        self.assertIn(r'\mathds {1}', render.prepare(r'\mathbb{1}_A', False))
        self.assertIn(r'\mathbb {R}', render.prepare(r'\mathbb{R}', False))

    def test_display_lines(self):
        self.assertIn('gathered', render.prepare(r'a = b \\ c = d', True))
        self.assertIn('aligned', render.prepare(r'a &= b \\ c &= d', True))
        self.assertNotIn('gathered', render.prepare(r'\begin{pmatrix} a \\ b \end{pmatrix}', True))

    def test_blank_lines_do_not_end_the_formula(self):
        self.assertNotIn('\n\n', render.prepare('a\n\n\nb', True))


class KatexSyntaxTest(unittest.TestCase):
    """What prepare() makes of KaTeX's and MathJax's own syntax."""

    def prep(self, latex, display=False):
        return render.prepare(latex, display).replace(' ', '')

    def test_web_commands_keep_their_content(self):
        self.assertEqual(self.prep(r'\href{https://katex.org/}{\KaTeX}'), r'{\KaTeX}')
        self.assertEqual(self.prep(r'\htmlClass{foo}{x}+\cssId{i}{y}'), '{x}+{y}')
        self.assertEqual(self.prep(r'\mathtip{x}{tip}'), '{x}')
        self.assertEqual(self.prep(r'\require{cancel}\cancel{x}'), r'\cancel{x}')
        with self.assertRaises(render.Unsafe):
            render.prepare(r'\require{cancel}', False)

    def test_url_and_verb_are_literal(self):
        tex = self.prep(r'\url{https://a.b/%20_#}')
        self.assertIn(r'\texttt', tex)
        self.assertNotIn('%', tex)
        self.assertIn(r'\char37', tex)
        self.assertIn(r'\char95', self.prep(r'\verb|a_b|'))

    def test_hex_colours(self):
        self.assertIn('[HTML]{0000FF}', self.prep(r'\color{#00F}x'))
        self.assertIn('[HTML]{228B22}{F', self.prep(r'\textcolor{#228B22}{F}'))
        self.assertIn('[HTML]{FF0000}{00FFFF}', self.prep(r'\fcolorbox{red}{#0ff}{A}'))
        self.assertEqual(self.prep(r'\color{red}x'), r'\color{red}x')

    def test_braced_dimensions_and_mu_rules(self):
        self.assertIn(r'\mskip10mu\relax', self.prep(r'a\mskip{10mu}b'))
        self.assertIn(r'\above2pt\relax', self.prep(r'{a \above{2pt} b}'))
        self.assertIn('{1.0000em}', self.prep(r'\rule{18mu}{18mu}'))

    def test_renamed_commands(self):
        self.assertEqual(self.prep(r'\empty'), r'\emptyset')
        self.assertEqual(self.prep(r'\reflectbox{ab}'), r'\imcontent{ab}')
        self.assertEqual(self.prep(r'\scalebox{2}[3]{ab}'), r'\imcontent{ab}')
        self.assertEqual(self.prep(r'a+\vcenter{b}'), r'a+\imvcenter{b}')
        self.assertIn(r'\imqty', render.prepare(r'\qty(\frac12)', False))
        self.assertNotIn('imqty', render.prepare(r'\qty{5}{\meter}', False))

    def test_sizes(self):
        self.assertEqual(self.prep(r'\Huge AB \tiny c'), r'\imsize{\Huge}{AB}\imsize{\tiny}{c}')
        self.assertEqual(self.prep(r'\left(\LARGE{AB}\right)'), r'\left(\imsize{\LARGE}{{AB}}\right)')
        self.assertEqual(self.prep(r'\text{\Huge x}'), r'\text{\Hugex}')

    def test_plain_tex_forms(self):
        self.assertEqual(self.prep(r'\pmatrix{a & b \cr c & d}'), r'\begin{pmatrix}a&b\\c&d\end{pmatrix}')
        self.assertTrue(self.prep(r'\begin{numcases}{f(x)=} 1 & x>0 \end{numcases}', True).startswith(
            r'f(x)=\begin{cases}'))
        tex = self.prep(r'\begin{empheq}[left=\empheqlbrace]{align} a&=b \end{empheq}', True)
        self.assertTrue(tex.startswith(r'\left\{\begin{aligned}'), tex)
        tex = self.prep(r'\begin{align} a&=b \\ \intertext{so} c&=d \end{align}', True)
        self.assertIn(r'\text{so}', tex)
        self.assertNotIn('intertext', tex)

    def test_char_codes(self):
        self.assertEqual(self.prep(r'\char"3B1'), r'\alpha')
        self.assertEqual(self.prep(r'\char"41'), r'\char"41')
        self.assertEqual(self.prep(r'\unicode{x221E}'), r'\infty')

    def test_mathematical_alphanumerics(self):
        tex = self.prep('𝐀𝑥𝔄𝟙𝛂ℬ𝒙𝖠𝚊')
        for part in (r'\mathbf{A}', 'x', r'\mathfrak{A}', r'\mathds{1}', r'\boldsymbol{\alpha}', r'\mathscr{B}',
                     r'\boldsymbol{x}', r'\mathsf{A}', r'\mathtt{a}'):
            self.assertIn(part, tex)
        self.assertIn(r'\ensuremath{\mathbf{A}}', self.prep(r'\text{𝐀}'))

    def test_unicode_table(self):
        self.assertEqual(self.prep('∔⟦⇢'), r'\dotplus\llbracket\dashrightarrow')
        self.assertTrue(set(render.UNICODE_MATH).isdisjoint(set(unimath.SYMBOLS)) or all(
            render.unicode_tex(c) == render.UNICODE_MATH[c] for c in render.UNICODE_MATH))

    def test_cjk_font(self):
        every = ['CJKutf8'] + [f'cjk-{font}' for font in render.CJK_FONTS]
        self.assertEqual(render.cjk_family(r'\text{概率}', every), 'gbsn')
        self.assertEqual(render.cjk_family(r'\text{機率與統計}', every), 'bsmi')
        self.assertEqual(render.cjk_family(r'\text{日本語のテキスト}', every), 'ipxm')
        self.assertEqual(render.cjk_family(r'\text{확률}', every), 'mj')
        self.assertEqual(render.cjk_family(r'\text{機率}', ['CJKutf8']), 'gbsn')
        # Japanese without kana: neither GB2312 nor Big5 holds 学習 or 変.
        self.assertEqual(render.cjk_family(r'\text{学習率} \eta', every), 'ipxm')
        self.assertEqual(render.cjk_family(r'\text{変数}', every), 'ipxm')
        self.assertEqual(render.cjk_family(r'\text{温度}', every), 'gbsn')

    def test_dashed_rules_are_solid(self):
        # arydshln is not loaded (it dropped the rules of an array's first row).
        self.assertEqual(self.prep(r'\begin{array}{c:c|c}a&b&c\\\hdashline d&e&f\end{array}'),
                         r'\begin{array}{c|c|c}a&b&c\\\hlined&e&f\end{array}')
        self.assertEqual(self.prep(r'\begin{array}[t]{@{:}c:c}a\cdashline{1-2}\end{array}'),
                         r'\begin{array}[t]{@{:}c|c}a\cline{1-2}\end{array}')
        self.assertEqual(self.prep(r'a:b \coloneqq c'), r'a:b\coloneqqc')  # a ratio stays

    def test_physics_power_and_text_symbols(self):
        self.assertEqual(self.prep(r'\sin[2](x)+\cos[n]\theta+\sin[x]'), r'\sin^{2}(x)+\cos^{n}\theta+\sin[x]')
        self.assertEqual(self.prep(r'25\,\text{°C}'), r'25\,\text{\ensuremath{{}^\circ}C}')
        self.assertEqual(self.prep(r'\text{"free" and "x"}'), "\\text{``free''and``x''}")
        self.assertEqual(self.prep(r'\texttt{"x"}'), r'\texttt{"x"}')
        self.assertEqual(self.prep(r'\text{a 5" screen}'), "\\text{a5''screen}")
        self.assertIsNone(render.OVERHANG.search(r'a \hspace{1em} b \kern 2pt c'))
        self.assertIsNotNone(render.OVERHANG.search(r'a \hspace{-1em} b'))
        self.assertIsNotNone(render.OVERHANG.search(r'\mathclap{x}'))


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class CompatTest(Case):
    """Formulas that need the additions to the preamble, drawn for real."""

    DRAWN = [
        r'\begin{CD} A @>a>> B \\ @VbVV @AAcA \\ C @= D \end{CD}', r'\begin{darray}{cc} \frac12 & b \end{darray}',
        r'\oiint_V f \, \oiiint_V g', r'\widecheck{abc} \utilde{AB} \overgroup{AB} \undergroup{AB}',
        r'\xtwoheadrightarrow{abc} \xlongequal{x} \xtofrom[b]{a}', r'\Overrightarrow{AB} \overlinesegment{AB}',
        r'\text{\th} \th \LaTeX \KaTeX', r'\Braket{\phi | A | \psi} \Bra{x} \Ket{y}', r'\lBrace x \rBrace',
        r'\begin{array}{c:c} a & b \\ \hdashline c & d \end{array}', r'\text{\sout{abc} \sect}',
        r'\fcolorbox{red}{aqua}{A} \colorbox{#ffcc00}{$b$}', r'\text{if $x>0$, \(y\)}', r'\url{https://a.b/%_#}',
        r'\Huge A \tiny b', r'\mathsfit{Ab} \upalpha \Upgamma', r'\pu{123 kJ/mol} \ce{H2O}', r'\qty(\frac{a}{b})',
        r'\bbox[yellow, border: 1px solid red]{x}', r'\enclose{updiagonalstrike}{x}', r'𝔄 ⇢ ∔ ⟦x⟧ ↺',
        r'\text{繁體中文：機率與統計}', r'\text{한국어 수학}', r'\text{日本語のテキスト}', r'\text{简体中文}',
        # Every alphabet in one formula: the additions must leave TeX's 16 math families enough room.
        r'\mathsf{a}\mathtt{b}\mathit{c}\mathbf{d}\bm{x}\mathscr{A}\mathfrak{A}\mathbb{A}\mathds{1}\mathcal{A}'
        r'\boldsymbol{\alpha}\llbracket\upalpha\oiint\widecheck{x}',
    ]

    def test_drawn(self):
        results = self.render(*[(latex, True) for latex in self.DRAWN])['results']
        for latex, result in zip(self.DRAWN, results):
            self.assertIn('png', result, f'{latex}: {result.get("error")}')

    def test_a_missing_glyph_is_an_error(self):
        # 这 (simplified) and 個 (traditional) are in no one font: TeX used to drop one without a word.
        result = self.render((r'\text{这個}', False))['results'][0]
        self.assertIn('error', result)
        self.assertIn('Missing character', result['error'])

    def ink_columns(self, result, rows):
        """For each pixel column of the picture, the longest vertical run of ink within pixel rows `rows`."""
        img = self.image(result).getchannel('A')
        out = []
        for x in range(img.width):
            run = best = 0
            for y in rows:
                run = run + 1 if img.getpixel((x, y)) > 128 else 0
                best = max(best, run)
            out.append(best)
        return out

    def test_an_array_rule_reaches_its_first_row(self):
        # arydshln (no longer loaded) left the first row of {c|c} without its rule.
        result = self.render((r'\begin{array}{c|c} a & b \\ c & d \end{array}', True))['results'][0]
        top = self.ink_columns(result, range(0, result['rows'] * CH // 2))
        self.assertGreater(max(top), CH * 0.6, 'no vertical rule in the top half')
        one = self.render((r'\begin{array}{l|r} x & y \end{array}', False))['results'][0]
        self.assertGreater(max(self.ink_columns(one, range(one['rows'] * CH))), CH * 0.5)

    def test_ink_outside_the_box_is_kept(self):
        # A CD arrow's side labels sit outside the diagram's box: with them it is wider.
        labelled, bare = self.render((r'\begin{CD} A @>>> B \\ @VgVV @VVhV \\ C @>>> D \end{CD}', True),
                                     (r'\begin{CD} A @>>> B \\ @VVV @VVV \\ C @>>> D \end{CD}', True))['results']
        self.assertGreater(self.image(labelled).width, self.image(bare).width)
        clap = self.render((r'\underbrace{a+b}_{\mathclap{\text{a long explanatory label}}}', True))['results'][0]
        self.assertGreaterEqual(clap['columns'], 12)

    def test_kernel_symbols_and_physics_forms(self):
        # stmaryrd must not turn \bigtriangleup into a big operator.
        self.assertEqual(self.render((r'a \bigtriangleup b', False))['results'][0]['rows'], 1)
        sub = self.render((r'\bigtriangleup_{i} x', True))['results'][0]
        self.assertEqual(sub['rows'], 1)
        results = self.render((r'\pdv{f}{x}{y}', True), (r'\pdv{f}{x}', True), (r'\dd[3]{x}', False),
                              (r'\dd{x}', False), (r'\vb*{x} \vb{x}', False), (r'\abs{\frac{a}{b}}', True),
                              (r'\abs{x}', True), (r'\vdot \cross \ip{a}{b} \op{a}{b} \dyad{a}', False),
                              (r'\text{25 °C}', False), (r'\text{学習率} \eta', False))['results']
        self.assertTrue(all('png' in r for r in results), results)
        mixed, single, cubed, plain, _, grown, small = results[:7]
        self.assertGreater(mixed['columns'], single['columns'])  # \partial^2 f / \partial x \partial y
        ink = [self.image(r).getchannel('A').getbbox() for r in (cubed, plain)]
        self.assertGreater(ink[0][2] - ink[0][0], ink[1][2] - ink[1][0])  # d^3 x, not d[3]x nor d x
        self.assertGreater(grown['rows'], small['rows'])  # bars that grow with a fraction

    @unittest.skipUnless(shutil.which('dvips') and shutil.which('gs'), 'dvips and gs are needed')
    def test_tikz_cd_diagram(self):
        if 'tikz-cd.sty' not in render.kpse_find(['tikz-cd.sty']):
            self.skipTest('tikz-cd is not installed')
        diagram = (r'\begin{tikzcd} A \arrow[r, "f"] \arrow[d] & B \arrow[d, "h"] \\'
                   r' C \arrow[r, "k" swap] & D \end{tikzcd}')
        results = self.render((diagram, True), ('x', False))['results']
        self.assertIn('png', results[0], results[0].get('error'))
        self.assertGreaterEqual(results[0]['rows'], 3)
        self.assertIn('png', results[1])


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class FitTest(Case):
    def test_rows(self):
        results = self.render(('x^2', False), (r'\frac{a+b}{c}', False),
                              (r'\int_0^\infty e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}', True))['results']
        self.assertEqual(results[0]['rows'], 1)
        self.assertEqual(results[1]['rows'], 2)
        self.assertGreaterEqual(results[2]['rows'], 2)
        for result in results:
            self.assertTrue(0 <= result['textRow'] < result['rows'])

    def test_canvas_is_whole_cells(self):
        results = self.render(('x^2', False), (r'\frac{a+b}{c}', False), (r'\sum_{k=0}^{n} k^2', True),
                              (r'\text{速度}', False))['results']
        for result in results:
            img = self.image(result)
            self.assertEqual(img.size, (result['columns'] * CW, result['rows'] * CH))
            self.assertEqual(img.mode, 'RGBA')
            self.assertEqual(img.getpixel((0, 0))[3], 0)

    def test_baseline(self):
        result = self.render(('x', False))['results'][0]
        alpha = self.image(result).getchannel('A').point(lambda a: 255 if a > 96 else 0)
        bottom = alpha.getbbox()[3]
        baseline = result['textRow'] * CH + 0.773 * CH
        self.assertLessEqual(abs(bottom - baseline), 2)

    def test_too_wide_is_scaled_or_refused(self):
        results = self.render((r'\rule{3000pt}{1pt}', False), (r'\hspace{15000pt}x', False),
                              ('x', False))['results']
        self.assertEqual(results[0]['columns'], 255)
        self.assertEqual(results[1], {'error': 'too large'})
        self.assertIn('png', results[2])

    def test_guard_stays_inside_tex_limits(self):
        results = self.render(('x', False), (r'\frac{a}{b}', True), ptPx=0.5, cell='19x400')['results']
        self.assertTrue(all('png' in r for r in results), results)

    def test_pages_must_match_items(self):
        def wrap(real):
            def extra_page(workdir, dpi, fg):
                found = real(workdir, dpi, fg)
                if len(found) > 1:
                    found[len(found) + 1] = found[1]
                return found
            return extra_page

        with self.patch_dvipng(wrap):
            results = self.render(('a', False), ('b+c+d+e', False))['results']
        self.assertTrue(all('png' in r for r in results), results)
        self.assertLess(results[0]['columns'], results[1]['columns'])

    def test_max_columns(self):
        latex = r'a+b+c+d+e+f+g+h+i+j+k+l'
        full = self.render((latex, False))['results'][0]['columns']
        limit = full * 2 // 3
        result = self.render((latex, False), maxColumns=limit)['results'][0]
        self.assertLessEqual(result['columns'], limit)
        self.assertEqual(self.render((latex, False), maxColumns=2)['results'][0], {'error': 'too large'})

    def test_cjk_text(self):
        if 'CJKutf8' not in render.available_packages():
            self.skipTest('CJKutf8 is not installed')
        result = self.render((r'v = \frac{\text{距离}}{\text{时间}}', False))['results'][0]
        self.assertIn('png', result)


class DetectTest(unittest.TestCase):
    def test_cell_option(self):
        with mock.patch.object(render.subprocess, 'run', refuse):
            self.assertEqual(render.detect_cell('10x21'), (10, 21))
        self.assertIsNone(render.parse_cell('0x0'))
        self.assertIsNone(render.parse_cell('abc'))

    def test_cell_from_tmux(self):
        with mock.patch.dict(os.environ, {'TMUX': '/tmp/x,1,0'}), \
                mock.patch.object(render, 'quiet_run', return_value='17x38\n'):
            self.assertEqual(render.detect_cell('auto'), (17, 38))

    def test_cell_default(self):
        with mock.patch.dict(os.environ, {'TMUX': ''}), mock.patch.object(render, 'tty_cell', return_value=None):
            self.assertEqual(render.detect_cell('auto'), (19, 42))

    def test_fg(self):
        self.assertEqual(render.detect_fg('#ABCDEF'), '#abcdef')
        answers = {
            '(<uint32 1>,)': render.DARK_FG, '(<uint32 0>,)': render.LIGHT_FG,
            '(<uint32 2>,)': render.LIGHT_FG,
        }
        for portal, fg in answers.items():
            with mock.patch.object(render, 'quiet_run', return_value=portal):
                self.assertEqual(render.detect_fg('auto'), fg)
        with mock.patch.object(render, 'quiet_run', side_effect=['', "'prefer-dark'\n"]):
            self.assertEqual(render.detect_fg('auto'), render.DARK_FG)
        with mock.patch.object(render, 'quiet_run', side_effect=['', "'default'\n"]):
            self.assertEqual(render.detect_fg('auto'), render.LIGHT_FG)


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class FormatTest(Case):
    """The precompiled preamble: built once per key, used, and left when it fails."""

    def fresh_formats(self):
        folder = os.path.join(self.tmp, 'formats')
        patch = mock.patch.dict(os.environ, {'INLINE_MATH_FORMAT_DIR': folder})
        patch.start()
        self.addCleanup(patch.stop)
        return folder

    def test_built_once_then_used(self):
        folder = self.fresh_formats()
        spy = Spy()
        with mock.patch.object(render.subprocess, 'run', spy):
            first = self.render(('x^2', False), ('y', False))['results']
            shutil.rmtree(self.tmp + '-none', ignore_errors=True)
            second = self.render(('z_1', False), ('w', True), cacheDir=os.path.join(self.tmp, 'other'))['results']
        self.assertTrue(all('png' in r for r in first + second), first + second)
        self.assertEqual(spy.builds(), 1)
        runs = [argv for argv in spy.argvs if argv[0] == 'latex' and '-ini' not in argv]
        self.assertTrue(runs and all(any(a.startswith('-fmt=') for a in argv) for argv in runs), runs)
        self.assertEqual(len([n for n in os.listdir(folder) if n.endswith('.fmt')]), 1)

    def test_same_pictures_without_the_format(self):
        items = [('x^2', False), (r'\frac{a}{b}', True), (r'\text{速度} = \mathbb{R}', False), (r'\ce{H2O}', False)]
        with_format = self.render(*items)['results']
        shutil.rmtree(self.tmp)
        self.session = lambda setup: render.Session(setup.plain())
        without = self.render(*items)['results']
        self.assertTrue(all('png' in r for r in with_format), with_format)
        self.assertEqual(with_format, without)

    def test_user_macros_and_packages(self):
        cfg = render.parse_options({'cacheDir': self.tmp, 'macros': r'\newcommand\vect[1]{\mathbf{#1}}',
                                    'packages': ['esint', 'no-such-package-xyz', 'bad name!']})
        setup = render.tex_setup(cfg)
        self.assertIn(r'\newcommand\vect', setup.preamble)
        self.assertNotIn('no-such-package-xyz}', setup.preamble)
        self.assertTrue(any('no-such-package-xyz' in p for p in setup.problems), setup.problems)
        self.assertTrue(any('bad name' in p for p in setup.problems), setup.problems)
        plain = render.tex_setup(render.parse_options({'cacheDir': self.tmp}))
        self.assertNotEqual(setup.key, plain.key)
        self.assertNotEqual(setup.fmt, plain.fmt)
        result = self.render((r'\vect{v}', False), macros=r'\newcommand\vect[1]{\mathbf{#1}}')['results'][0]
        self.assertIn('png', result)
        self.assertIn('error', self.render((r'\vect{v}', False))['results'][0])  # another cache key

    def test_macros_that_break_the_preamble_are_left_out(self):
        self.fresh_formats()
        broken = r'\newcommand\frac{x}'  # \frac exists: an error
        spy = Spy()
        with mock.patch.object(render.subprocess, 'run', spy):
            setup = render.tex_setup(render.parse_options({'cacheDir': self.tmp, 'macros': broken}))
            again = render.tex_setup(render.parse_options({'cacheDir': self.tmp, 'macros': broken}))
        self.assertNotIn(broken, setup.preamble)
        self.assertTrue(setup.fmt)
        self.assertTrue(any('left out' in p for p in setup.problems), setup.problems)
        self.assertEqual(spy.builds(), 2)  # the broken one, then the plain one; the failure is remembered
        self.assertEqual(again.fmt, setup.fmt)
        self.assertIn('png', self.render(('x', False), macros=broken)['results'][0])

    def test_a_format_that_does_not_load_is_dropped(self):
        self.fresh_formats()
        setup = render.tex_setup(render.parse_options({'cacheDir': self.tmp}))
        with open(setup.fmt, 'wb') as f:
            f.write(b'not a format')
        results = self.render(('x', False), ('y', False))['results']
        self.assertTrue(all('png' in r for r in results), results)
        self.assertFalse(os.path.exists(setup.fmt))
        self.assertTrue(os.path.exists(setup.fmt[:-4] + '.err'))
        later = render.tex_setup(render.parse_options({'cacheDir': self.tmp}))
        self.assertIsNone(later.fmt)  # not built again at once
        self.assertTrue(any(p.startswith('format:') for p in later.problems), later.problems)

    def test_key_follows_the_tex_installation(self):
        found = render.kpse_find(render.tex_files(), self.tmp)
        cfg = render.parse_options({'cacheDir': self.tmp})
        setup = render.tex_setup(cfg)
        path = sorted(found.values())[0]
        with mock.patch.object(render, 'tex_identity', lambda paths: [[path, 1]]):
            self.assertIsNone(render.format_for(setup.preamble, found, self.tmp, build=False)[0])

    def test_physics_keeps_the_user_macros(self):
        # physics defines \abs; the built-in one gives way instead of breaking the preamble.
        self.fresh_formats()
        more = {'packages': ['physics'], 'macros': r'\newcommand{\RR}{\mathbb{R}}'}
        setup = render.tex_setup(render.parse_options(dict(more, cacheDir=self.tmp)))
        self.assertFalse(any('left out' in p for p in setup.problems), setup.problems)
        results = self.render((r'\RR', False), (r'\abs{x}', False), (r'\norm{v}', False), **more)['results']
        self.assertTrue(all('png' in r for r in results), results)

    def test_a_package_that_breaks_the_preamble_is_left_out_alone(self):
        self.fresh_formats()
        more = {'packages': ['fontspec', 'tensor'], 'macros': r'\newcommand{\RR}{\mathbb{R}}'}
        if not {'fontspec.sty', 'tensor.sty'} <= set(render.kpse_find(['fontspec.sty', 'tensor.sty'])):
            self.skipTest('fontspec and tensor are needed')
        spy = Spy()
        with mock.patch.object(render.subprocess, 'run', spy):
            setup = render.tex_setup(render.parse_options(dict(more, cacheDir=self.tmp)))
            again = render.tex_setup(render.parse_options(dict(more, cacheDir=self.tmp)))
        self.assertNotIn('{fontspec}', setup.preamble)  # needs XeTeX or LuaTeX
        self.assertIn('{tensor}', setup.preamble)
        self.assertIn(r'\RR', setup.preamble)
        self.assertTrue(setup.fmt)
        self.assertTrue(any('fontspec' in p and 'left out' in p for p in setup.problems), setup.problems)
        self.assertEqual(again.fmt, setup.fmt)
        self.assertEqual(again.problems, setup.problems)
        latex_runs = spy.count('latex')
        self.assertLessEqual(latex_runs, 6)  # two builds, the preamble tries once; then nothing
        self.assertIn('png', self.render((r'\RR', False), **more)['results'][0])

    def test_check_reports_the_format(self):
        status = render.check(options(self.tmp, packages=['no-such-package-xyz']))
        self.assertTrue(status['ok'], status['problems'])
        self.assertTrue(status['format']['ok'])
        self.assertTrue(any('no-such-package-xyz' in p for p in status['problems']), status['problems'])


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class WarmBatchTest(WarmCase, BatchTest):
    pass


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class WarmCacheTest(WarmCase, CacheTest):
    pass


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class WarmIsolationTest(WarmCase, IsolationTest):
    pass


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class WarmFitTest(WarmCase, FitTest):
    pass


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class WarmTest(WarmCase):
    """What only the warm processes do."""

    def test_no_run_folders_are_left(self):
        self.render(('a', False), (r'\nosuch', False), ('b', False), (r'\rule{1400pt}{3100pt}', False))
        left = os.listdir(self.daemon.work)
        self.assertFalse([name for name in left if not name.startswith('tex-')], left)
        self.assertLessEqual(len(left), renderd.POOL + 1)

    def test_a_dead_dvipng_is_replaced(self):
        self.assertIn('png', self.render(('a', False))['results'][0])
        with self.daemon.dvipng.lock:
            self.daemon.dvipng.proc.kill()
            self.daemon.dvipng.proc.wait()
        self.assertIn('png', self.render(('b', False))['results'][0])

    def test_a_dead_warm_latex_is_replaced(self):
        self.assertIn('png', self.render(('a', False))['results'][0])
        for pool in self.daemon.pools.values():
            for warm in list(pool.ready):
                warm.proc.kill()
                warm.proc.wait()
        self.assertIn('png', self.render(('b', False))['results'][0])

    def test_latex_time_limit(self):
        with mock.patch.object(render, 'check_safe', lambda toks, latex: None), \
                mock.patch.object(render, 'LATEX_TIMEOUT', 1.0):
            started = time.monotonic()
            results = self.render((r'\def\x{\x}\x', False))['results']
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(results[0], {'error': 'timeout'})

    def test_a_heavy_batch_is_split_and_keeps_its_order(self):
        items = [(r'\ce{H2O}', False), ('a', False), (r'\ce{CO2 + C -> 2 CO}', False), ('b^2', True),
                 (r'\pu{1.2 kJ/mol}', False), (r'\nosuch', False), (r'\ce{NaCl}', False)]
        self.assertGreater(len(render.split_batch([(latex, d) for latex, d in items], renderd.POOL)), 1)
        request = {'items': [{'latex': latex, 'display': d} for latex, d in items], 'options': options(self.tmp)}
        split = render.render_request(request, new_session=self.session, parallel=renderd.POOL)['results']
        shutil.rmtree(self.tmp)
        alone = render.render_request(request, new_session=self.session)['results']
        self.assertEqual(split, alone)
        self.assertIn('error', split[5])
        self.assertTrue(all('png' in r for k, r in enumerate(split) if k != 5), split)

    def test_concurrent_requests(self):
        import threading
        out = {}

        def one(n):
            out[n] = self.render((f'x^{{{n}}}+{n}', False), (f'y_{n}', True))['results']

        threads = [threading.Thread(target=one, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(out), list(range(6)))
        self.assertTrue(all('png' in r for results in out.values() for r in results), out)


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class CommandLineTest(Case):
    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'render.py')

    def test_stdin_to_stdout(self):
        request = {'items': [{'latex': 'x', 'display': False}, {'latex': r'\input{x}', 'display': False}],
                   'options': options(self.tmp)}
        done = subprocess.run([sys.executable, '-I', self.SCRIPT], input=json.dumps(request),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(len(done.stdout.strip().splitlines()), 1)
        response = json.loads(done.stdout)
        self.assertIn('png', response['results'][0])
        self.assertIn('error', response['results'][1])

    def test_detect(self):
        done = subprocess.run([sys.executable, '-I', self.SCRIPT, '--detect'],
                              capture_output=True, text=True, timeout=60)
        found = json.loads(done.stdout)
        self.assertRegex(found['fg'], r'^#[0-9a-f]{6}$')
        self.assertGreater(found['cell']['width'], 0)

    def test_failure_echoes_only_what_the_request_fixed(self):
        out = io.StringIO()
        request = json.dumps({'items': [{'latex': 'x'}], 'options': {'fg': '#123456', 'cell': 'auto'}})
        with mock.patch.object(render, 'render_request', side_effect=RuntimeError('boom')), \
                mock.patch.object(sys, 'stdin', io.StringIO(request)), mock.patch.object(sys, 'stdout', out):
            render.main()
        response = json.loads(out.getvalue())
        self.assertEqual(response['fg'], '#123456')
        self.assertNotIn('cell', response)
        self.assertTrue(response['results'][0]['error'].startswith('renderer failed'))

    def test_bad_request(self):
        done = subprocess.run([sys.executable, '-I', self.SCRIPT], input='not json',
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout)['results'], [])

    def test_check(self):
        done = subprocess.run([sys.executable, '-I', self.SCRIPT, '--check'],
                              capture_output=True, text=True, timeout=60)
        status = json.loads(done.stdout)
        for key in ('ok', 'latex', 'dvipng', 'pil', 'cell', 'fg', 'problems'):
            self.assertIn(key, status)
        self.assertTrue(status['ok'], status['problems'])


if __name__ == '__main__':
    unittest.main()
