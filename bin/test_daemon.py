"""Tests for client.py and renderd.py: the daemon's life, as the mod sees it.

Run: python3 -I -m unittest discover -s bin   (from the mod folder)

Every test has its own runtime folder (INLINE_MATH_RUN_DIR), so it starts
its own daemons and never meets the user's; it stops them all when done.
The client runs as the mod runs it: python3 -I -S client.py.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import client

HERE = os.path.dirname(os.path.abspath(__file__))
HAVE_TEX = bool(shutil.which('latex') and shutil.which('dvipng'))
FORMATS = None


def setUpModule():
    global FORMATS
    FORMATS = tempfile.mkdtemp(prefix='inline-math-test-fmt-')


def tearDownModule():
    shutil.rmtree(FORMATS, ignore_errors=True)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().split(') ')[1][0] != 'Z'
    except OSError:
        return True


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def processes_in(folder):
    """Pids of processes whose working folder is inside folder."""
    found = []
    for name in os.listdir('/proc'):
        if name.isdigit():
            try:
                if os.readlink(f'/proc/{name}/cwd').startswith(folder):
                    found.append(int(name))
            except OSError:
                pass
    return found


@unittest.skipUnless(HAVE_TEX, 'latex and dvipng are needed')
class DaemonCase(unittest.TestCase):
    bin = HERE

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='inline-math-test-')
        self.run_dir = os.path.join(self.tmp, 'run')
        os.mkdir(self.run_dir, 0o700)
        self.cache = os.path.join(self.tmp, 'cache')
        self.env = dict(os.environ, INLINE_MATH_RUN_DIR=self.run_dir, INLINE_MATH_FORMAT_DIR=FORMATS,
                        INLINE_MATH_IDLE='60')
        self.env.pop('INLINE_MATH_NO_DAEMON', None)
        self.pids = set()
        self.addCleanup(self.cleanup)
        self.count = 0

    def cleanup(self):
        for pid in self.pids | set(self.daemon_pids()):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for pid in self.pids:
            if not wait_for(lambda pid=pid: not alive(pid), 10):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def daemon_pids(self):
        pids = []
        for name in os.listdir('/proc'):
            if name.isdigit():
                try:
                    with open(f'/proc/{name}/cmdline', 'rb') as f:
                        argv = f.read().split(b'\0')
                except OSError:
                    continue
                if any(a.endswith(b'renderd.py') for a in argv) and self.run_dir.encode() in argv:
                    pids.append(int(name))
        return pids

    def request(self, *latex):
        self.count += 1
        items = [{'latex': f'{tex}+{self.count}', 'display': False} for tex in latex or ['x^2']]
        return {'items': items, 'options': {'fg': '#000000', 'cell': '19x42', 'cacheDir': self.cache}}

    def call(self, request=None, args=(), env=None, bin=None):
        done = subprocess.run([sys.executable, '-I', '-S', os.path.join(bin or self.bin, 'client.py'), *args],
                              input=json.dumps(request or self.request()), capture_output=True, text=True,
                              timeout=90, env=env or self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.pids.update(self.daemon_pids())
        return json.loads(done.stdout)

    def ping(self, bin=None):
        """The daemon's status, without starting one."""
        ident = self.identity(bin)
        path = client.socket_path(self.run_dir, ident)
        try:
            sock = client.connect(path)
        except OSError:
            return None
        try:
            reply = client.exchange(sock, {'id': ident, 'op': 'ping', 'request': {}}, 10)
        finally:
            sock.close()
        return reply and reply.get('response')

    def identity(self, bin=None):
        with mock.patch.object(client, 'HERE', bin or self.bin):
            return client.identity()

    def sockets(self):
        return sorted(name for name in os.listdir(self.run_dir) if name.endswith('.sock'))


class LifeTest(DaemonCase):
    def test_started_once_then_reused(self):
        first = self.call()
        self.assertIn('png', first['results'][0], first)
        status = self.ping()
        self.assertTrue(status and alive(status['pid']), status)
        second = self.call(self.request('a', 'b', r'\nosuch'))
        self.assertEqual(['png' in r for r in second['results']], [True, True, False])
        again = self.ping()
        self.assertEqual(again['pid'], status['pid'])
        self.assertGreaterEqual(again['served'], 3)
        self.assertEqual(self.sockets(), [f'renderd-{self.identity()}.sock'])

    def test_stale_socket_is_cleaned(self):
        path = client.socket_path(self.run_dir, self.identity())
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()  # the file stays, nobody listens
        response = self.call()
        self.assertIn('png', response['results'][0], response)
        self.assertTrue(self.ping())

    def test_killed_daemon_is_replaced_and_swept(self):
        self.call()
        old = self.ping()['pid']
        work = os.path.join(self.run_dir, f'work-{old}')
        self.assertTrue(wait_for(lambda: len(processes_in(work)) >= 2, 10))  # warm latex processes
        orphans = processes_in(work)
        os.kill(old, signal.SIGKILL)
        self.assertTrue(wait_for(lambda: not alive(old)))
        response = self.call()
        self.assertIn('png', response['results'][0], response)
        new = self.ping()['pid']
        self.assertNotEqual(new, old)
        self.assertFalse(os.path.exists(work))
        self.assertTrue(wait_for(lambda: not any(alive(pid) for pid in orphans), 10), orphans)

    def test_idle_daemon_stops_and_cleans_up(self):
        self.env['INLINE_MATH_IDLE'] = '1'
        self.call()
        pid = self.ping()['pid']
        work = os.path.join(self.run_dir, f'work-{pid}')
        self.assertTrue(wait_for(lambda: not alive(pid), 15))
        self.assertEqual(self.sockets(), [])
        self.assertFalse(os.path.exists(work))
        self.assertEqual(processes_in(self.run_dir), [])

    def test_sigterm_cleans_up(self):
        self.call()
        pid = self.ping()['pid']
        os.kill(pid, signal.SIGTERM)
        self.assertTrue(wait_for(lambda: not alive(pid), 15))
        self.assertEqual(self.sockets(), [])
        self.assertEqual([n for n in os.listdir(self.run_dir) if n.startswith('work-')], [])

    def test_new_renderer_version_gets_a_new_daemon(self):
        copy = os.path.join(self.tmp, 'bin')
        os.mkdir(copy)
        for name in client.FILES:
            shutil.copy2(os.path.join(HERE, name), copy)
        self.call(bin=copy)
        old = self.ping(copy)
        self.assertTrue(old)
        with open(os.path.join(copy, 'render.py'), 'a') as f:
            f.write('\n# a new version\n')
        response = self.call(bin=copy)
        self.assertIn('png', response['results'][0], response)
        new = self.ping(copy)
        self.assertNotEqual(new['pid'], old['pid'])
        self.assertTrue(wait_for(lambda: not alive(old['pid']), 15))  # the old one saw its files change
        self.assertEqual(self.sockets(), [f'renderd-{self.identity(copy)}.sock'])

    def test_concurrent_clients_share_one_daemon(self):
        procs, requests = [], []
        for n in range(6):
            requests.append(self.request(f'x_{n}', f'\\frac{{{n}}}{{2}}', f'y^{n}'))
            procs.append(subprocess.Popen([sys.executable, '-I', '-S', os.path.join(HERE, 'client.py')],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=self.env))
        outputs = [json.loads(p.communicate(json.dumps(r), timeout=60)[0]) for p, r in zip(procs, requests)]
        self.pids.update(self.daemon_pids())
        for output in outputs:
            self.assertTrue(all('png' in r for r in output['results']), output)
        self.assertEqual(len(self.daemon_pids()), 1)
        self.assertEqual(len(self.sockets()), 1)

    def test_check_goes_through_the_daemon(self):
        done = subprocess.run([sys.executable, '-I', '-S', os.path.join(HERE, 'client.py'), '--check'],
                              input=json.dumps({'options': {'cacheDir': self.cache, 'packages': ['no-such-xyz']}}),
                              capture_output=True, text=True, timeout=60, env=self.env)
        self.pids.update(self.daemon_pids())
        status = json.loads(done.stdout)
        self.assertTrue(status['ok'], status)
        self.assertTrue(status['daemon'] and alive(status['daemon']['pid']), status)
        self.assertTrue(status['format']['ok'], status)
        self.assertTrue(any('no-such-xyz' in p for p in status['problems']), status['problems'])

    def test_detect_and_auto_options_resolve_in_the_client(self):
        done = subprocess.run([sys.executable, '-I', '-S', os.path.join(HERE, 'client.py'), '--detect'],
                              capture_output=True, text=True, timeout=60, env=self.env)
        found = json.loads(done.stdout)
        self.assertRegex(found['fg'], r'^#[0-9a-f]{6}$')
        self.assertEqual(self.daemon_pids(), [])  # --detect never needs the daemon
        request = self.request()
        request['options'].update({'fg': 'auto', 'cell': '11x23'})
        response = self.call(request)
        self.assertEqual(response['cell'], {'width': 11, 'height': 23})
        self.assertRegex(response['fg'], r'^#[0-9a-f]{6}$')


    def test_warm_starts_the_daemon_and_loads_the_format(self):
        done = subprocess.run([sys.executable, '-I', '-S', os.path.join(HERE, 'client.py'), '--warm'],
                              input=json.dumps({'options': {'cacheDir': self.cache}}),
                              capture_output=True, text=True, timeout=60, env=self.env)
        self.pids.update(self.daemon_pids())
        status = json.loads(done.stdout)['daemon']
        self.assertTrue(status and alive(status['pid']), done.stdout + done.stderr)
        self.assertTrue(wait_for(lambda: (self.ping() or {}).get('warm', 0) >= 1, 10), self.ping())
        self.assertEqual(self.call(self.request('y'))['results'][0].keys(), {'png', 'columns', 'rows', 'textRow'})


class FallbackTest(DaemonCase):
    def test_unsafe_run_folder_renders_in_process(self):
        os.chmod(self.run_dir, 0o755)
        response = self.call()
        self.assertIn('png', response['results'][0], response)
        self.assertEqual(self.daemon_pids(), [])
        self.assertEqual(os.listdir(self.run_dir), [])
        # --check says why no daemon runs (the formulas still draw: ok stays true).
        done = subprocess.run([sys.executable, '-I', '-S', os.path.join(HERE, 'client.py'), '--check'],
                              input=json.dumps({'options': {'cacheDir': self.cache}}),
                              capture_output=True, text=True, timeout=60, env=self.env)
        status = json.loads(done.stdout)
        self.assertTrue(status['ok'], status)
        self.assertIsNone(status['daemon'])
        self.assertTrue(any('open to other users' in p and self.run_dir in p for p in status['problems']), status)

    def test_linked_run_folder_is_refused(self):
        link = os.path.join(self.tmp, 'link')
        os.symlink(self.run_dir, link)
        self.assertFalse(client.private_dir(link))
        self.assertTrue(client.private_dir(self.run_dir))
        self.env['INLINE_MATH_RUN_DIR'] = link
        response = self.call()
        self.assertIn('png', response['results'][0], response)
        self.assertEqual(os.listdir(self.run_dir), [])

    def test_daemon_that_cannot_start_renders_in_process(self):
        copy = os.path.join(self.tmp, 'bin')
        os.mkdir(copy)
        for name in client.FILES:
            shutil.copy2(os.path.join(HERE, name), copy)
        with open(os.path.join(copy, 'renderd.py'), 'w') as f:
            f.write('raise SystemExit(1)\n')
        started = time.monotonic()
        response = self.call(bin=copy)
        self.assertIn('png', response['results'][0], response)
        self.assertLess(time.monotonic() - started, 15)

    def test_in_process_flag(self):
        response = self.call(args=['--in-process'])
        self.assertIn('png', response['results'][0], response)
        self.assertEqual(self.daemon_pids(), [])

    def test_bad_request_is_answered(self):
        done = subprocess.run([sys.executable, '-I', '-S', os.path.join(HERE, 'client.py')], input='not json',
                              capture_output=True, text=True, timeout=60, env=self.env)
        response = json.loads(done.stdout)
        self.assertEqual(response['results'], [])
        self.assertIn('bad request', response['error'])


class ProtocolTest(unittest.TestCase):
    def test_a_silent_daemon_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 's.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            held = []
            thread = threading.Thread(target=lambda: held.append(server.accept()[0]), daemon=True)
            thread.start()
            sock = client.connect(path)
            started = time.monotonic()
            with self.assertRaises(client.TimedOut):
                client.exchange(sock, {'op': 'ping'}, 0.3)
            self.assertLess(time.monotonic() - started, 2)
            thread.join(2)
            for conn in held + [sock, server]:
                conn.close()

    def test_a_closed_connection_is_no_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 's.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            thread = threading.Thread(target=lambda: server.accept()[0].close(), daemon=True)
            thread.start()
            sock = client.connect(path)
            try:
                self.assertIsNone(client.exchange(sock, {'op': 'ping'}, 5))
            except BrokenPipeError:
                pass  # closed before the request was sent: ask_daemon treats both alike
            thread.join(2)
            sock.close()
            server.close()

    def test_identity_follows_the_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in client.FILES:
                shutil.copy2(os.path.join(HERE, name), tmp)
            with mock.patch.object(client, 'HERE', tmp):
                before = client.identity()
                with open(os.path.join(tmp, 'client.py'), 'a') as f:
                    f.write('\n')
                self.assertNotEqual(client.identity(), before)


if __name__ == '__main__':
    unittest.main()
