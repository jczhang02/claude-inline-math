"""Tests for render.py.

Run: python3 -I -m unittest discover -s bin   (from the mod folder)

Every test fixes the cell size, the colour and a private cache folder, so
results do not depend on this desktop. Tests that need TeX are skipped when
latex or dvipng is missing.
"""

import base64
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

HAVE_TEX = bool(shutil.which('latex') and shutil.which('dvipng'))
CW, CH = 19, 42


def options(cache_dir, **more):
    return dict({'cell': f'{CW}x{CH}', 'fg': '#000000', 'cacheDir': cache_dir}, **more)


class Spy:
    """Counts the programs render.py runs, and still runs them."""

    def __init__(self):
        self.calls = []
        self.real = subprocess.run

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(argv[0])
        return self.real(argv, *args, **kwargs)

    def count(self, program):
        return self.calls.count(program)


def refuse(*args, **kwargs):
    raise AssertionError(f'unexpected program run: {args[0] if args else kwargs}')


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='inline-math-test-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def render(self, *items, **more):
        request = {
            'items': [{'latex': latex, 'display': display} for latex, display in items],
            'options': options(self.tmp, **more),
        }
        return render.render_request(request)

    def image(self, result):
        from PIL import Image
        return Image.open(io.BytesIO(base64.b64decode(result['png'])))


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class BatchTest(Case):
    def test_results_in_item_order_from_one_latex_run(self):
        spy = Spy()
        with mock.patch.object(render.subprocess, 'run', spy):
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
        spy = Spy()
        with mock.patch.object(render.subprocess, 'run', spy):
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
        spy = Spy()
        with mock.patch.object(render.subprocess, 'run', spy):
            results = self.render(('q', False), ('q', False), ('q', True))['results']
        self.assertEqual(results[0], results[1])
        self.assertEqual(spy.count('latex'), 1)


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class CacheTest(Case):
    def test_hit_runs_no_tex(self):
        items = [('x^2', False), (r'\sum_{i=1}^n i', True), (r'\undefinedthing', False)]
        first = self.render(*items)
        with mock.patch.object(render.subprocess, 'run', refuse):
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
        names = [name for name in os.listdir(self.tmp) if name != 'packages.txt']
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
        with mock.patch.object(render.subprocess, 'run', refuse):
            again = self.render((r'\pi r^2', False))['results'][0]
        self.assertEqual(again, clean)

    def test_an_error_caused_by_another_item_is_not_kept(self):
        with mock.patch.object(render, 'check_safe', lambda toks, latex: None):
            results = self.render(('a', False), (r'\gdef\frac{\undefined}x', False), (r'\frac12', False))['results']
        self.assertIn('png', results[2])

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


class PackagesTest(Case):
    def setUp(self):
        super().setUp()
        saved = render._available
        render._available = None
        self.addCleanup(setattr, render, '_available', saved)

    def test_one_missing_package_keeps_the_others(self):
        found = subprocess.CompletedProcess([], 1, stdout='/x/amsmath.sty\n/x/preview.sty\n', stderr='')
        with mock.patch.object(render.subprocess, 'run', return_value=found):
            self.assertEqual(render.available_packages(self.tmp), ['amsmath', 'preview'])
        render._available = None
        with mock.patch.object(render.subprocess, 'run', refuse):
            self.assertEqual(render.available_packages(self.tmp), ['amsmath', 'preview'])

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
        real = render.run_dvipng

        def extra_page(workdir, dpi, fg):
            found = real(workdir, dpi, fg)
            if len(found) > 1:
                found[len(found) + 1] = found[1]
            return found

        with mock.patch.object(render, 'run_dvipng', extra_page):
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
