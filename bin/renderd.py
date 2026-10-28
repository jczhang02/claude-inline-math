"""The long-lived renderer behind client.py.

Usage:
    renderd.py --socket-dir DIR     (client.py starts it; it detaches itself)

It answers the requests of render.py on an AF_UNIX socket in DIR (one JSON
line in, one JSON line out; see client.py), with what makes a request fast:
Pillow imported once, latex processes that have loaded the format already
and wait on a FIFO for their document (a fresh one for every run, so a TeX
error cannot leak into the next run), and one dvipng that keeps its fonts
and takes its files on a pty.

One daemon per renderer version: the socket's name holds a hash of the
renderer files (client.identity), a lock file keeps a second daemon of the
same version from starting, and a daemon whose files changed stops. It also
stops after IDLE seconds without a request, and removes its temporary
folders (and those of daemons that died) under DIR.
"""

import errno
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import client  # noqa: E402 - after the path
import render  # noqa: E402

IDLE = 1800.0  # seconds without a request before the daemon stops (INLINE_MATH_IDLE)
POOL = 3  # warm latex processes per format (one takes ~110 ms to load the format)
POOLS = 2  # formats with warm processes
ACCEPT_POLL = 0.2
IDENTITY_POLL = 2.0
READ_TIMEOUT = 10.0  # for the request line
MAX_REQUEST = 64 * 1024 * 1024
DVIPNG_USES = 500  # runs before the persistent dvipng is replaced (its page list grows)
# dvipng reads at most 254 characters and 9 words per command line (more
# overflows a buffer of its own), so options that never change go on its
# command line, and files are named relative to its folder.
DVIPNG_LINE = 250
DVIPNG_WORDS = 9
DVIPNG_START = 5.0
LOG_MAX = 256 * 1024
BEGIN = '\\begin{document}\n'
# The warm latex has loaded the format and done \begin{document}; it waits
# in the primitive \input (one open of the FIFO), then reads the document.
STUB = BEGIN + '\\csname @@input\\endcsname b \n'


def limit(pid, memory, files=None):
    """The caps render.py sets in preexec_fn, set from outside (a preexec_fn is not safe with threads)."""
    try:
        render.resource.prlimit(pid, render.resource.RLIMIT_AS, (memory, memory))
        if files:
            render.resource.prlimit(pid, render.resource.RLIMIT_FSIZE, (files, files))
    except (AttributeError, OSError, ValueError):
        pass


def kill(proc):
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class Warm:
    """One latex that has loaded a format and waits for its document on the FIFO b.tex."""

    def __init__(self, fmt, root):
        self.dir = tempfile.mkdtemp(prefix='tex-', dir=root)
        self.fifo = os.path.join(self.dir, 'b.tex')
        os.mkfifo(self.fifo, 0o600)
        with open(os.path.join(self.dir, 'f.tex'), 'w', encoding='utf-8') as f:
            f.write(STUB)
        self.proc = subprocess.Popen(render.latex_argv(fmt) + ['f.tex'], cwd=self.dir, env=render.tex_env(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        limit(self.proc.pid, render.LATEX_MEMORY, render.LATEX_LOG_MAX)

    def alive(self):
        return self.proc.poll() is None

    def run(self, body, timeout):
        """Send body (the document after \\begin{document}); the exit status, or None on timeout."""
        deadline = time.monotonic() + timeout
        fd = None
        while fd is None:
            try:
                fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as err:
                if err.errno != errno.ENXIO:
                    raise
                if self.proc.poll() is not None:
                    return self.proc.returncode  # it stopped before reading: its log tells why
                if time.monotonic() > deadline:
                    kill(self.proc)
                    return None
                time.sleep(0.001)  # not at the \input yet
        data = memoryview(body.encode('utf-8'))
        try:
            while data:
                left = deadline - time.monotonic()
                if left <= 0:
                    kill(self.proc)
                    return None
                _, ready, _ = select.select([], [fd], [], left)
                if ready:
                    data = data[os.write(fd, data):]
        except BrokenPipeError:
            pass  # latex stopped reading (an error with -halt-on-error)
        finally:
            os.close(fd)
        try:
            return self.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            kill(self.proc)
            return None

    def close(self):
        kill(self.proc)
        shutil.rmtree(self.dir, ignore_errors=True)


class Pool:
    """Warm latex processes for one format; a taken one is replaced at once."""

    def __init__(self, fmt, root):
        self.fmt = fmt
        self.root = root
        self.lock = threading.Lock()
        self.ready = []
        self.starting = 0  # being started by fill(), counted so two fills do not overshoot
        self.closed = False
        self.used = time.monotonic()
        self.fill()

    def fill(self):
        while True:
            with self.lock:
                dead = [warm for warm in self.ready if not warm.alive()]
                self.ready = [warm for warm in self.ready if warm not in dead]
                full = self.closed or len(self.ready) + self.starting >= POOL
                if not full:
                    self.starting += 1
            for warm in dead:
                warm.close()
            if full:
                return
            try:
                warm = Warm(self.fmt, self.root)
            except OSError as err:
                log(f'cannot start latex: {err}')
                warm = None
            with self.lock:
                self.starting -= 1
                if warm and not self.closed:
                    self.ready.append(warm)
                    continue
            if warm:
                warm.close()
            return

    def take(self):
        self.used = time.monotonic()
        with self.lock:
            while self.ready:
                warm = self.ready.pop(0)
                if warm.alive():
                    break
                warm.close()
            else:
                warm = None
        threading.Thread(target=self.fill, daemon=True).start()
        return warm or Warm(self.fmt, self.root)

    def close(self):
        with self.lock:
            self.closed = True
            ready, self.ready = self.ready, []
        for warm in ready:
            warm.close()


class Dvipng:
    """One dvipng in its interactive mode ("-") on a pty: each command line
    names a DVI file and dvipng answers with its pages and a "> " prompt.
    Fonts stay loaded between files."""

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.proc = None
        self.master = None
        self.uses, self.dpi, self.fg = 0, None, None

    def start(self):
        master, slave = os.openpty()
        tty.setraw(slave)
        try:
            self.proc = subprocess.Popen(['dvipng'] + render.DVIPNG_FIXED + ['-pp', '1-1000000', '-'], cwd=self.root,
                                         env=render.tex_env(),
                                         stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        except OSError:
            os.close(master)
            return False
        finally:
            os.close(slave)
        self.master = master
        self.uses, self.dpi, self.fg = 0, None, None
        limit(self.proc.pid, 1 << 30)
        if self.read(time.monotonic() + DVIPNG_START) is None:
            self.stop()
            return False
        return True

    def warm(self):
        with self.lock:
            if self.proc is None:
                self.start()

    def read(self, deadline):
        """Output up to the next prompt; None when dvipng stopped or took too long."""
        out = b''
        while not (out.endswith(b'> ') and b'\n' in out) and not out.endswith(b'\n> '):
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([self.master], [], [], left)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return None  # EIO: dvipng exited
                if not chunk:
                    return None
                out += chunk
        return out.decode('utf-8', 'replace')

    def stop(self):
        kill(self.proc)
        self.proc = None
        if self.master is not None:
            os.close(self.master)
            self.master = None

    def run(self, workdir, dpi, fg):
        """Like render.run_dvipng: {page: (path, depth)} for workdir's f.dvi."""
        where = os.path.relpath(workdir, self.root)
        if where.startswith('..') or any(c in where for c in " '\t\n\r"):
            return render.run_dvipng(workdir, dpi, fg)  # cannot be named on dvipng's command line
        with self.lock:
            for _ in range(2):
                if (self.proc is None or self.proc.poll() is not None) and not self.start():
                    break
                # Resolution and colour stay set; a new file needs its page list again.
                words = [] if self.dpi == dpi else ['-D', str(dpi)]
                words += [] if self.fg == fg else ['-fg', f"'{render.dvipng_colour(fg)}'"]
                words += ['-pp', '1-1000000', '-o', f'{where}/p%d.png', f'{where}/f.dvi']
                line = ' '.join(words) + '\n'
                if len(line) > DVIPNG_LINE or len(words) > DVIPNG_WORDS:
                    break
                try:
                    os.write(self.master, line.encode('utf-8'))
                except OSError:
                    self.stop()
                    continue
                self.dpi, self.fg = dpi, fg
                out = self.read(time.monotonic() + render.DVIPNG_TIMEOUT)
                if out is None:
                    timed_out = self.proc is not None and self.proc.poll() is None
                    self.stop()
                    if timed_out:
                        return {}  # as render.run_dvipng on a timeout
                    continue  # it stopped (a fatal error): once more with a new one
                self.uses += 1
                if self.uses >= DVIPNG_USES:
                    self.stop()
                return render.dvipng_pages(workdir, out)
        return render.run_dvipng(workdir, dpi, fg)


class WarmSession(render.Session):
    """A render.Session whose latex runs take a warm process, and whose
    dvipng runs go to the persistent dvipng."""

    def __init__(self, setup, daemon):
        super().__init__(setup)
        self.daemon = daemon
        self.dirs = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for path in self.dirs:
            shutil.rmtree(path, ignore_errors=True)

    def without_format(self):
        if self.setup.fmt:
            self.daemon.drop_pool(self.setup.fmt)
        return super().without_format()

    def workdir(self):
        path = tempfile.mkdtemp(prefix='run-', dir=self.daemon.work)
        self.dirs.append(path)
        return path

    def latex(self, body, timeout):
        if not self.setup.fmt or not body.startswith(BEGIN):
            return super().latex(body, timeout)  # no format: a fresh latex reads the preamble
        warm = self.daemon.pool(self.setup.fmt).take()
        self.dirs.append(warm.dir)
        status = warm.run(body[len(BEGIN):], timeout)
        return warm.dir, status

    def dvipng(self, workdir, dpi, fg):
        return self.daemon.dvipng.run(workdir, dpi, fg)


_log_path = None


def log(message):
    if not _log_path:
        return
    try:
        if os.path.getsize(_log_path) > LOG_MAX:
            os.replace(_log_path, _log_path + '.old')
    except OSError:
        pass
    try:
        with open(_log_path, 'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} [{os.getpid()}] {message}\n')
    except OSError:
        pass


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep(folder):
    """Remove the work folders of daemons that are gone. A warm latex of a
    daemon that was killed outright still waits on its FIFO: opening the
    FIFO for writing and closing it again gives it end of file, and it stops."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith('work-') and name[5:].isdigit() and not pid_alive(int(name[5:])):
            work = os.path.join(folder, name)
            try:
                tex = [os.path.join(work, entry, 'b.tex') for entry in os.listdir(work) if entry.startswith('tex-')]
            except OSError:
                tex = []
            for fifo in tex:
                try:
                    os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass
            shutil.rmtree(work, ignore_errors=True)


class Daemon:
    def __init__(self, folder, ident):
        self.folder = folder
        self.ident = ident
        self.started = time.monotonic()
        self.idle = float(os.environ.get('INLINE_MATH_IDLE') or IDLE)
        self.work = os.path.join(folder, f'work-{os.getpid()}')
        self.lock = threading.Lock()
        self.pools = {}
        self.active = 0
        self.served = 0
        self.last = time.monotonic()
        self.stopping = threading.Event()
        self.dvipng = None
        self.server = None
        self.socket_path = client.socket_path(folder, ident)

    # ------------------------------------------------------------ TeX

    def pool(self, fmt):
        with self.lock:
            pool = self.pools.get(fmt)
            if pool is None:
                pool = self.pools[fmt] = Pool(fmt, self.work)
                for old in sorted(self.pools.values(), key=lambda p: p.used)[:max(0, len(self.pools) - POOLS)]:
                    del self.pools[old.fmt]
                    old.close()
            return pool

    def drop_pool(self, fmt):
        with self.lock:
            pool = self.pools.pop(fmt, None)
        if pool:
            pool.close()

    def session(self, setup):
        return WarmSession(setup, self)

    # ------------------------------------------------------------ requests

    def answer(self, message):
        if not isinstance(message, dict) or message.get('id') != self.ident:
            self.stopping.set()  # a client of another version reached this socket: make way
            return {'id': self.ident, 'restart': True}
        op, request = message.get('op'), message.get('request')
        request = request if isinstance(request, dict) else {}
        if op == 'render':
            # A heavy batch (mhchem) is split over the warm processes of the pool.
            response = render.answer(request, new_session=self.session, parallel=POOL)
        elif op == 'warm':
            response = self.warm(request.get('options'))
        elif op == 'check':
            response = render.check(request.get('options'), new_session=self.session)
            response['daemon'] = self.status()
        elif op == 'ping':
            response = self.status()
        else:
            return {'id': self.ident, 'error': f'unknown op {op!r}'}
        return {'id': self.ident, 'response': response}

    def warm(self, options):
        """Make ready what the next request with these options needs (a turn
        is starting): the format, its warm latex processes, and dvipng."""
        try:
            setup = render.tex_setup(render.parse_options(options))
            if setup.fmt:
                self.pool(setup.fmt)
            self.dvipng.warm()
        except render.RendererError as err:
            log(f'warm failed: {err}')
        return self.status()

    def status(self):
        with self.lock:
            warm = sum(len(p.ready) for p in self.pools.values())
        return {'pid': os.getpid(), 'id': self.ident, 'uptime': round(time.monotonic() - self.started, 1),
                'served': self.served, 'warm': warm}

    def handle(self, conn):
        try:
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            if struct.unpack('3i', creds)[1] != os.getuid():
                return
            conn.settimeout(READ_TIMEOUT)
            data = b''
            while not data.endswith(b'\n'):
                chunk = conn.recv(1 << 20)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_REQUEST:
                    return
            try:
                message = json.loads(data.decode('utf-8'))
            except ValueError:
                message = None
            try:
                reply = self.answer(message)
            except Exception as err:  # noqa: BLE001 - one bad request must not stop the daemon
                log(f'request failed: {type(err).__name__}: {err}')
                reply = {'id': self.ident, 'error': f'renderer failed: {err}'[:300]}
            conn.settimeout(None)
            conn.sendall(json.dumps(reply).encode('utf-8') + b'\n')
        except OSError:
            pass  # the client went away
        finally:
            conn.close()
            with self.lock:
                self.active -= 1
                self.served += 1
                self.last = time.monotonic()

    # ------------------------------------------------------------ life

    def listen(self):
        """Bind the socket; False when another daemon of this version holds the lock."""
        self.lock_fd = os.open(self.socket_path[:-5] + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            render.fcntl.flock(self.lock_fd, render.fcntl.LOCK_EX | render.fcntl.LOCK_NB)
        except OSError:
            return False
        try:
            os.unlink(self.socket_path)  # stale: its daemon is gone, or it would hold the lock
        except FileNotFoundError:
            pass
        sweep(self.folder)
        self.open_work()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.server.listen(64)
        self.server.settimeout(ACCEPT_POLL)
        self.inode = os.stat(self.socket_path).st_ino
        return True

    def open_work(self):
        """The work folder and dvipng (all a daemon needs to render, also without a socket)."""
        os.mkdir(self.work, 0o700)
        self.dvipng = Dvipng(self.work)
        threading.Thread(target=self.dvipng.warm, daemon=True).start()  # beside the first latex

    def serve(self):
        checked = time.monotonic()
        while not self.stopping.is_set():
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                conn = None
            except OSError as err:
                log(f'accept failed: {err}')
                break
            now = time.monotonic()
            if conn is not None:
                conn.setblocking(True)
                with self.lock:
                    self.active += 1
                    self.last = now
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
                continue
            with self.lock:
                idle = self.active == 0 and now - self.last > self.idle
            if idle:
                break
            if now - checked > IDENTITY_POLL:
                checked = now
                try:
                    if client.identity() != self.ident:
                        log('renderer files changed: stopping')
                        break
                except OSError:
                    break

    def shutdown(self):
        """Stop taking requests, finish the ones in progress, and leave nothing behind."""
        self.stopping.set()
        try:
            if self.server and os.stat(self.socket_path).st_ino == self.inode:
                os.unlink(self.socket_path)
        except OSError:
            pass
        if self.server:
            self.server.close()
        end = time.monotonic() + render.DEADLINE + 5
        while time.monotonic() < end:
            with self.lock:
                if self.active <= 0:
                    break
            time.sleep(0.05)
        with self.lock:
            pools, self.pools = list(self.pools.values()), {}
        for pool in pools:
            pool.close()
        if self.dvipng:
            with self.dvipng.lock:
                self.dvipng.stop()
        shutil.rmtree(self.work, ignore_errors=True)


def main(argv):
    global _log_path
    if '--socket-dir' not in argv[:-1]:
        print('usage: renderd.py --socket-dir DIR', file=sys.stderr)
        return 2
    folder = argv[argv.index('--socket-dir') + 1]
    if not client.private_dir(folder):
        return 1
    render.REFRESH_BEHIND = True  # no request waits for kpsewhich
    _log_path = os.path.join(folder, 'renderd.log')
    try:
        ident = client.identity()
    except OSError:
        return 1
    daemon = Daemon(folder, ident)
    try:
        if not daemon.listen():
            return 0  # another daemon of this version is starting or running
    except OSError as err:
        log(f'cannot listen: {err}')
        return 1

    def stop(signum, frame):
        daemon.stopping.set()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    log(f'started {ident}')
    try:
        daemon.serve()
    finally:
        daemon.shutdown()
        log('stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
