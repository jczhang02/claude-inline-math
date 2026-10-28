"""How much of a formula corpus the renderer draws.

Usage:
    python3 bin/coverage.py [CORPUS ...] [--in-process] [--quiet] [--json]

CORPUS names files of bin/corpus/ (katex, mathjax, claude; default all).
A corpus file holds one formula per line; "$$ " starts a display formula,
and lines that are empty or start with "#" are skipped.

Every formula goes through the real backend as the mod calls it: batches
on stdin of `python3 -I -S client.py` (the daemon), or of
`client.py --in-process` with --in-process. The daemon gets a private run
folder and the pictures a private cache, so each run typesets every formula
afresh and nothing of the user's own setup is used; the format folder is
the user's (INLINE_MATH_FORMAT_DIR overrides it), so a built format is
reused.

Output: the pass rate of each corpus, then every failure with its error.
A failure listed in the allow-list of docs/compatibility.md (a fenced block
tagged "allow-list", lines "reason: formula") is counted apart, with its
reason; the exit status is 1 when any other formula fails.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS_DIR = os.path.join(HERE, 'corpus')
DOC = os.path.join(os.path.dirname(HERE), 'docs', 'compatibility.md')
CORPORA = ('katex', 'mathjax', 'claude')
BATCH = 40
WORKERS = 3
OPTIONS = {'fg': '#4c4f69', 'cell': '19x42'}


def read_corpus(name):
    """[(line number, latex, display)] of bin/corpus/NAME.txt."""
    out = []
    with open(os.path.join(CORPUS_DIR, name + '.txt'), encoding='utf-8') as f:
        for number, line in enumerate(f, 1):
            line = line.rstrip('\n')
            if not line.strip() or line.startswith('#'):
                continue
            display = line.startswith('$$ ')
            out.append((number, line[3:] if display else line, display))
    return out


def read_allow_list(path=DOC):
    """{latex: reason} from the allow-list block of docs/compatibility.md."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return {}
    allowed = {}
    for block in re.findall(r'^```allow-list\n(.*?)^```', text, re.S | re.M):
        for line in block.splitlines():
            reason, sep, latex = line.partition(': ')
            if sep and latex.strip():
                allowed[latex.strip()] = reason.strip()
    return allowed


class Backend:
    """client.py with a private run folder and picture cache."""

    def __init__(self, in_process=False):
        self.in_process = in_process
        self.root = tempfile.mkdtemp(prefix='inline-math-coverage-')
        self.run_dir = os.path.join(self.root, 'run')
        os.mkdir(self.run_dir, 0o700)
        self.cache = os.path.join(self.root, 'cache')
        self.env = dict(os.environ, INLINE_MATH_RUN_DIR=self.run_dir, INLINE_MATH_IDLE='30')
        self.env.pop('INLINE_MATH_NO_DAEMON', None)

    def render(self, items):
        """Results for [(latex, display)], in order."""
        argv = [sys.executable, '-I', '-S', os.path.join(HERE, 'client.py')]
        argv += ['--in-process'] if self.in_process else []
        request = {'items': [{'latex': latex, 'display': display} for latex, display in items],
                   'options': dict(OPTIONS, cacheDir=self.cache)}
        done = subprocess.run(argv, input=json.dumps(request), capture_output=True, text=True, env=self.env,
                              timeout=300)
        try:
            results = json.loads(done.stdout)['results']
        except (ValueError, KeyError, TypeError):
            results = None
        if not isinstance(results, list) or len(results) != len(items):
            reason = f'client.py failed: {(done.stderr or done.stdout)[-300:]}'
            return [{'error': reason} for _ in items]
        return results

    def close(self):
        """Stop the daemon of the private run folder, and remove the folder."""
        sys.path.insert(0, HERE)
        import client
        for name in os.listdir(self.run_dir):
            if not name.endswith('.sock'):
                continue
            try:
                sock = client.connect(os.path.join(self.run_dir, name))
                try:
                    reply = client.exchange(sock, {'id': name[8:-5], 'op': 'ping', 'request': {}}, 5)
                finally:
                    sock.close()
                os.kill(reply['response']['pid'], 15)
            except (OSError, TypeError, KeyError, client.TimedOut):
                continue
            for _ in range(100):
                if not os.path.exists(os.path.join(self.run_dir, name)):
                    break
                time.sleep(0.05)
        shutil.rmtree(self.root, ignore_errors=True)


def run(names=CORPORA, in_process=False):
    """{corpus: [(line number, latex, display, result)]}."""
    backend = Backend(in_process)
    try:
        backend.render([('x', False)])  # start the daemon (and build the format) once
        jobs = []
        for name in names:
            lines = read_corpus(name)
            for start in range(0, len(lines), BATCH):
                jobs.append((name, lines[start:start + BATCH]))
        with ThreadPoolExecutor(WORKERS) as pool:
            done = list(pool.map(lambda job: backend.render([(latex, d) for _, latex, d in job[1]]), jobs))
    finally:
        backend.close()
    out = {name: [] for name in names}
    for (name, lines), results in zip(jobs, done):
        out[name] += [(number, latex, display, result) for (number, latex, display), result in zip(lines, results)]
    return out


def summary(found, allowed):
    """{corpus: {"total", "passed", "allowed": [...], "failed": [...]}}; failures as (line, latex, error[, reason])."""
    out = {}
    for name, rows in found.items():
        passed, allow, failed = 0, [], []
        for number, latex, _, result in rows:
            if 'png' in result:
                passed += 1
            elif latex in allowed:
                allow.append((number, latex, result.get('error', '?'), allowed[latex]))
            else:
                failed.append((number, latex, result.get('error', '?')))
        out[name] = {'total': len(rows), 'passed': passed, 'allowed': allow, 'failed': failed}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('corpora', nargs='*', default=list(CORPORA))
    parser.add_argument('--in-process', action='store_true', help='render in client.py, without the daemon')
    parser.add_argument('--quiet', action='store_true', help='rates only, no failure list')
    parser.add_argument('--json', action='store_true', help='the summary as JSON')
    args = parser.parse_args()
    started = time.monotonic()
    report = summary(run(args.corpora, args.in_process), read_allow_list())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for name, row in report.items():
            total = max(row['total'], 1)
            print(f'{name:<8} {row["passed"]:>5}/{row["total"]:<5} {100 * row["passed"] / total:6.1f} % drawn'
                  f'   {len(row["allowed"])} allowed failures, {len(row["failed"])} other failures')
        for name, row in report.items():
            if args.quiet:
                break
            for number, latex, error, reason in row['allowed']:
                print(f'  {name}:{number}: ALLOWED ({reason}): {latex}\n      {error}')
            for number, latex, error in row['failed']:
                print(f'  {name}:{number}: FAILED: {latex}\n      {error}')
        print(f'{time.monotonic() - started:.1f} s')
    return 1 if any(row['failed'] for row in report.values()) else 0


if __name__ == '__main__':
    sys.exit(main())
