"""Benchmark the renderer the way the mod calls it.

Usage:
    python3 bin/bench.py [--runs N] [--baseline DIR]

Each sample spawns `python3 -I -S client.py` with the request on stdin, as
$.process.run does, and times it from spawn to exit. Formulas are new in
every sample (a counter in them) and every sample has a fresh picture cache.
The daemon gets a private runtime folder and the format a private folder,
so nothing of the user's own setup is used or changed; the format is built
once at the start (timed separately).

Cases, each the median of N samples (default 9):
    one / ten      one new formula, a batch of ten new formulas
    warm           daemon up and warm (target: one < 50 ms, ten < 100 ms)
    warm-burst     the same, calls back to back (no pause for the pool to refill)
    warm-chem      warm, mhchem formulas (\\ce costs ~30 ms of TeX each; a
                   batch of them is split over the warm latex processes)
    cold           no daemon: the client starts it (format already built)
    in-process     client.py --in-process (the fallback without a daemon)
    v1             render.py of the baseline folder, when given
    hit            ten formulas that are all in the picture cache
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FORMULAS = [
    r'\frac{a+b}{c}', r'\int_0^\infty e^{-x^2}\,dx=\frac{\sqrt\pi}{2}', r'\sum_{k=1}^{n} k^2',
    r'\alpha+\beta=\gamma', r'\mathbb{E}[X]=\mu', r'\begin{pmatrix}a&b\\c&d\end{pmatrix}',
    r'\nabla\cdot\mathbf{E}=\rho/\varepsilon_0', r'x^2+y^2=r^2', r'\lim_{n\to\infty}(1+1/n)^n=e',
    r'\hat{\theta}=\arg\max_\theta L(\theta)',
]
CHEMISTRY = [
    r'\ce{CH4 + 2O2 -> CO2 + 2H2O}', r'\ce{Fe^3+ + e- -> Fe^2+}', r'\ce{H2SO4 -> 2H+ + SO4^2-}',
    r'\ce{Cu^2+ + 4NH3 -> [Cu(NH3)4]^2+}', r'\ce{2H2 + O2 -> 2H2O}', r'\ce{NaCl}', r'\ce{H2O}', r'\ce{A <=> B}',
    r'\ce{CO2 + C -> 2CO}', r'\ce{^{14}_{6}C -> ^{14}_{7}N + e^-}',
]
TARGETS = {('warm', 1): 50.0, ('warm', 10): 100.0}


class Bench:
    def __init__(self, runs, baseline):
        self.runs = runs
        self.baseline = baseline
        self.root = tempfile.mkdtemp(prefix='inline-math-bench-')
        self.counter = 0
        self.env = dict(os.environ, INLINE_MATH_FORMAT_DIR=os.path.join(self.root, 'fmt'),
                        INLINE_MATH_IDLE='120')
        self.env.pop('INLINE_MATH_NO_DAEMON', None)

    def request(self, count, cache_dir=None, formulas=FORMULAS):
        self.counter += 1
        items = [{'latex': f'{latex}+{self.counter}', 'display': n % 3 == 0}
                 for n, latex in enumerate(formulas[:count])]
        cache_dir = cache_dir or tempfile.mkdtemp(prefix='cache-', dir=self.root)
        return {'items': items, 'options': {'fg': '#4c4f69', 'cell': '19x42', 'cacheDir': cache_dir}}

    def call(self, request, run_dir, argv=None):
        """Milliseconds from spawn to exit, and the parsed response."""
        argv = argv or [sys.executable, '-I', '-S', os.path.join(HERE, 'client.py')]
        env = dict(self.env, INLINE_MATH_RUN_DIR=run_dir)
        data = json.dumps(request)
        started = time.perf_counter()
        done = subprocess.run(argv, input=data, capture_output=True, text=True, env=env, timeout=120)
        took = (time.perf_counter() - started) * 1000
        response = json.loads(done.stdout)
        bad = [r for r in response['results'] if 'png' not in r]
        if bad:
            raise SystemExit(f'render failed: {bad[0]} {done.stderr[-500:]}')
        return took, response

    def run_dir(self):
        path = tempfile.mkdtemp(prefix='run-', dir=self.root)
        os.chmod(path, 0o700)
        return path

    def stop_daemons(self, run_dir):
        """Stop the daemons listening in run_dir (asked for their pid; never starts one)."""
        sys.path.insert(0, HERE)
        import client
        for name in os.listdir(run_dir):
            if not name.endswith('.sock'):
                continue
            try:
                sock = client.connect(os.path.join(run_dir, name))
                try:
                    reply = client.exchange(sock, {'id': name[8:-5], 'op': 'ping', 'request': {}}, 5)
                finally:
                    sock.close()
                os.kill(reply['response']['pid'], 15)
            except (OSError, TypeError, KeyError, client.TimedOut):
                continue
            for _ in range(100):
                if not os.path.exists(os.path.join(run_dir, name)):
                    break
                time.sleep(0.05)

    def median(self, samples):
        return statistics.median(samples), min(samples), max(samples)

    def main(self):
        rows = []
        run_dir = self.run_dir()
        took, _ = self.call(self.request(1), run_dir, [sys.executable, '-I', '-S', os.path.join(HERE, 'client.py'),
                                                        '--in-process'])
        print(f'format build (first call ever, in-process): {took:.0f} ms', flush=True)

        if self.baseline:
            script = os.path.join(self.baseline, 'bin', 'render.py')
            for count in (1, 10):
                samples = [self.call(self.request(count), run_dir, [sys.executable, '-I', script])[0]
                           for _ in range(self.runs)]
                rows.append(('v1', count, samples))

        for count in (1, 10):
            samples = [self.call(self.request(count), run_dir,
                                 [sys.executable, '-I', '-S', os.path.join(HERE, 'client.py'), '--in-process'])[0]
                       for _ in range(self.runs)]
            rows.append(('in-process', count, samples))

        for count in (1, 10):
            samples = []
            for _ in range(self.runs):
                fresh = self.run_dir()
                samples.append(self.call(self.request(count), fresh)[0])
                self.stop_daemons(fresh)
            rows.append(('cold', count, samples))

        for _ in range(3):
            self.call(self.request(10), run_dir)  # start and warm the daemon
        for count in (1, 10):
            samples = []
            for _ in range(self.runs):
                samples.append(self.call(self.request(count), run_dir)[0])
                time.sleep(0.15)  # a reply's worth of time: the pool refills, as between real batches
            rows.append(('warm', count, samples))

        samples = [self.call(self.request(1), run_dir)[0] for _ in range(self.runs)]
        rows.append(('warm-burst', 1, samples))  # back to back: the pool may not have refilled

        for count in (1, 10):
            samples = []
            for _ in range(self.runs):
                time.sleep(0.3)  # three warm processes to refill after a split batch
                samples.append(self.call(self.request(count, formulas=CHEMISTRY), run_dir)[0])
            rows.append(('warm-chem', count, samples))

        cached = self.request(10)
        self.call(cached, run_dir)
        rows.append(('hit', 10, [self.call(cached, run_dir)[0] for _ in range(self.runs)]))
        self.stop_daemons(run_dir)

        print(f'\n{"case":<12}{"items":>6}{"median ms":>12}{"min":>8}{"max":>8}   target')
        for name, count, samples in rows:
            median, low, high = self.median(samples)
            target = TARGETS.get((name, count))
            verdict = '' if target is None else f'< {target:.0f} ms: {"PASS" if median < target else "FAIL"}'
            print(f'{name:<12}{count:>6}{median:>12.1f}{low:>8.1f}{high:>8.1f}   {verdict}')
        print(f'\n{self.runs} samples per case; spawn to exit of one client process each.')

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--runs', type=int, default=9)
    parser.add_argument('--baseline', help='a v1 folder with bin/render.py to compare against')
    args = parser.parse_args()
    bench = Bench(max(7, args.runs), args.baseline)
    try:
        bench.main()
    finally:
        bench.cleanup()


if __name__ == '__main__':
    main()
