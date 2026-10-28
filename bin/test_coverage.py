"""The corpora of bin/corpus/ through the real backend (bin/coverage.py).

Run: python3 -I -m unittest discover -s bin   (from the mod folder)

Every formula of KaTeX's supported list must draw, but for the allow-list
of docs/compatibility.md, and so must the MathJax and Claude corpora. An
allow-list entry that draws now, or that is in no corpus, fails as well, so
the list stays true. The format goes to a private folder (built once here);
coverage.py gives the daemon its own run folder and picture cache.
"""

import importlib.util
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HAVE_TEX = bool(shutil.which('latex') and shutil.which('dvipng'))


def load_tool():
    """bin/coverage.py by its path (an installed 'coverage' package must not stand in for it)."""
    spec = importlib.util.spec_from_file_location('corpus_coverage', os.path.join(HERE, 'coverage.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class CoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.formats = tempfile.mkdtemp(prefix='inline-math-test-fmt-')
        cls.saved = os.environ.get('INLINE_MATH_FORMAT_DIR')
        os.environ['INLINE_MATH_FORMAT_DIR'] = cls.formats
        cls.tool = load_tool()
        cls.allowed = cls.tool.read_allow_list()
        cls.found = cls.tool.run()
        cls.report = cls.tool.summary(cls.found, cls.allowed)

    @classmethod
    def tearDownClass(cls):
        if cls.saved is None:
            os.environ.pop('INLINE_MATH_FORMAT_DIR', None)
        else:
            os.environ['INLINE_MATH_FORMAT_DIR'] = cls.saved
        shutil.rmtree(cls.formats, ignore_errors=True)

    def failures(self, name):
        return [f'{name}:{number}: {latex}\n    {error}' for number, latex, error in self.report[name]['failed']]

    def test_katex_supported_list(self):
        self.assertGreaterEqual(self.report['katex']['total'], 1000)
        self.assertEqual(self.failures('katex'), [])

    def test_mathjax_extensions(self):
        self.assertEqual(self.failures('mathjax'), [])

    def test_claude_formulas(self):
        self.assertGreaterEqual(self.report['claude']['total'], 150)
        self.assertEqual(self.failures('claude'), [])

    def test_allow_list_is_current(self):
        self.assertTrue(self.allowed)
        seen = {latex: result for rows in self.found.values() for _, latex, _, result in rows}
        for latex, reason in self.allowed.items():
            self.assertIn(latex, seen, f'allow-listed ({reason}) but in no corpus: {latex}')
            self.assertNotIn('png', seen[latex], f'allow-listed ({reason}) but drawn: {latex}')


if __name__ == '__main__':
    unittest.main()
