"""The mod's entry to the renderer: render.py's interface, served by renderd.py.

Usage (the same as render.py, which documents the request and response):
    client.py              JSON request on stdin, JSON response on stdout
    client.py --check      JSON status, with the daemon's (options may come on stdin)
    client.py --detect     JSON {"cell", "fg"} the 'auto' options resolve to now
    client.py --warm       start the daemon if needed and make it ready for the
                           options on stdin (a turn is starting); JSON {"daemon"}
    client.py --in-process render here, without the daemon (tests, benchmarks)

The mod starts this once per batch (python3 -I -S), so it imports only the
standard library pieces it needs and leaves the work to renderd.py, a daemon
that keeps Pillow imported and TeX warm. It talks to the daemon over an
AF_UNIX socket in $XDG_RUNTIME_DIR/claude-inline-math/ (else
/tmp/claude-inline-math-UID/), a folder that must be a real folder of this
user with mode 0700, and only to a daemon of this user. The socket's name
holds a hash of the renderer files (identity), so a changed renderer gets a
new daemon. A missing daemon is started (detached, in its own session); one
that cannot be reached in time, or fails, leaves the request to render.py in
this process. 'auto' colour and cell size are resolved here, in the caller's
terminal, not in the daemon.

Protocol: one JSON line each way. Request {"id": identity, "op": "render" |
"check" | "warm" | "ping", "request": {...}}; reply {"id", "response"} or {"id",
"error"} or {"id", "restart": true} (a daemon of another version).
"""

import hashlib
import json
import os
import re
import socket
import stat
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('render.py', 'renderd.py', 'client.py', 'unimath.py')
CONNECT_TIMEOUT = 1.0
START_WAIT = 5.0  # a started daemon must listen within this
REPLY_TIMEOUT = 45.0  # one request; render.py stops typesetting after 30 s (the mod waits 90 s)
CHECK_TIMEOUT = 12.0  # the mod waits 15 s for --check
MAX_REPLY = 512 * 1024 * 1024


def identity():
    """Hash of this renderer's files (path, size, modification time)."""
    parts = [os.path.realpath(HERE)]
    for name in FILES:
        found = os.stat(os.path.join(HERE, name))
        parts.append([name, found.st_size, found.st_mtime_ns])
    return hashlib.sha256(json.dumps(parts).encode('utf-8')).hexdigest()[:16]


def private_dir(path):
    """Is path a folder (not a link) of this user that no one else may use?"""
    try:
        found = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid() and found.st_mode & 0o077 == 0


# Longest socket path AF_UNIX takes (sun_path is 108 bytes with its NUL).
SOCKET_PATH_MAX = 107
refused = []  # why run_dir() passed over each folder it did not take, for --check


def why_not_private(path):
    """Why path cannot hold the daemon's socket (None: it can)."""
    try:
        found = os.lstat(path)
    except OSError as err:
        return f'{path}: {err.strerror}'
    if not stat.S_ISDIR(found.st_mode):
        return f'{path} is not a folder'
    if found.st_uid != os.getuid():
        return f'{path} belongs to another user (uid {found.st_uid})'
    if found.st_mode & 0o077:
        return f'{path} is open to other users (mode {found.st_mode & 0o777:o}, needs 700)'
    return None


def run_dir():
    """The daemon's folder, made if missing; None when no safe one exists
    (then `refused` says why). After $XDG_RUNTIME_DIR and /tmp, a folder in
    the user's cache, which no other user can have made first."""
    override = os.environ.get('INLINE_MATH_RUN_DIR')
    if override:
        candidates = [override]
    else:
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        candidates = [os.path.join(runtime, 'claude-inline-math')] if runtime and os.path.isabs(runtime) else []
        candidates.append(f'/tmp/claude-inline-math-{os.getuid()}')
        cache = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
        if os.path.isabs(cache):
            candidates.append(os.path.join(cache, 'claude-inline-math', 'run'))
    refused.clear()
    for path in candidates:
        if len(socket_path(path, '0' * 16)) > SOCKET_PATH_MAX:
            refused.append(f'{path}: too long a path for a socket')
            continue
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as err:
            refused.append(f'{path}: {err.strerror}')
            continue
        reason = why_not_private(path)
        if reason is None:
            return path
        refused.append(reason)
    return None


def socket_path(folder, ident):
    return os.path.join(folder, f'renderd-{ident}.sock')


def start_daemon(folder):
    """Start renderd.py detached: its own session, no stdio of ours (the mod waits for those to close)."""
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        os.posix_spawn(sys.executable, [sys.executable, '-I', os.path.join(HERE, 'renderd.py'),
                                        '--socket-dir', folder],
                       os.environ, setsid=True,
                       file_actions=[(os.POSIX_SPAWN_DUP2, devnull, 0), (os.POSIX_SPAWN_DUP2, devnull, 1),
                                     (os.POSIX_SPAWN_DUP2, devnull, 2)])
    except (OSError, AttributeError, TypeError):
        return False
    finally:
        os.close(devnull)
    return True


def connect(path):
    """A socket connected to the daemon at path, which must run as this user."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect(path)
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        if struct.unpack('3i', creds)[1] != os.getuid():
            raise PermissionError('the daemon runs as another user')
    except BaseException:
        sock.close()
        raise
    return sock


class TimedOut(Exception):
    """The daemon took the request but gave no answer in time."""


def stop_daemon(sock):
    """Ask the daemon at the other end of sock to stop (SIGTERM: it finishes and cleans up)."""
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        pid, uid, _ = struct.unpack('3i', creds)
        if uid == os.getuid() and pid > 1:
            os.kill(pid, 15)
    except OSError:
        pass


def exchange(sock, message, timeout):
    """Send one line, read one line; the parsed reply, or None when the daemon
    closed without one. Raises TimedOut."""
    deadline = time.monotonic() + timeout
    sock.sendall(json.dumps(message).encode('utf-8') + b'\n')
    chunks, size = [], 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimedOut
        sock.settimeout(left)
        try:
            chunk = sock.recv(1 << 20)
        except socket.timeout:
            raise TimedOut from None
        except ConnectionError:
            break  # the daemon went away: no answer
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if chunk.endswith(b'\n') or size > MAX_REPLY:
            break
    data = b''.join(chunks)
    if not data.endswith(b'\n'):
        return None
    try:
        reply = json.loads(data.decode('utf-8'))
    except ValueError:
        return None
    return reply if isinstance(reply, dict) else None


def ask_daemon(op, request, timeout=REPLY_TIMEOUT):
    """The daemon's response to request, starting the daemon when needed;
    None when it cannot be reached or gave no answer. Raises TimedOut."""
    folder = run_dir()
    if folder is None:
        return None
    try:
        ident = identity()
    except OSError:
        return None
    path = socket_path(folder, ident)
    started, wait_until, pause = False, None, 0.002
    for _ in range(2):  # a second try when the daemon was stopping as this call reached it
        while True:
            try:
                sock = connect(path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if not started:
                    if not start_daemon(folder):
                        return None
                    started, wait_until = True, time.monotonic() + START_WAIT
                if time.monotonic() > wait_until:
                    return None
                time.sleep(pause)
                pause = min(pause * 1.5, 0.05)
            except OSError:
                return None
        try:
            reply = exchange(sock, {'id': ident, 'op': op, 'request': request}, timeout)
        except TimedOut:
            stop_daemon(sock)  # it hangs: the next call starts a new one once this one has gone
            raise
        except OSError:
            reply = None
        finally:
            sock.close()
        if reply is None or reply.get('restart'):
            if started:
                return None
            started = False  # start one (or reach the new one) once more
            continue
        if reply.get('id') != ident or not isinstance(reply.get('response'), dict):
            return None
        return reply['response']
    return None


def stdin_options(wait=0.2):
    """Request options given on stdin with --check, if any arrive in time (stdin may stay open)."""
    try:
        import select
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        ready, _, _ = select.select([sys.stdin], [], [], wait)
        text = sys.stdin.read(1 << 20) if ready else ''
        request = json.loads(text) if text.strip() else {}
    except (OSError, ValueError):
        return {}
    options = request.get('options') if isinstance(request, dict) else None
    return options if isinstance(options, dict) else {}


def load_render():
    """render.py, for work done in this process. -S left site-packages (Pillow) off the path."""
    if sys.flags.no_site and 'render' not in sys.modules:
        import site
        site.main()
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import render
    return render


def resolve_auto(options):
    """Fix 'auto' colour and cell size here: they belong to the caller's terminal and desktop."""
    options = dict(options) if isinstance(options, dict) else {}
    fg, cell = options.get('fg', 'auto'), options.get('cell', 'auto')
    concrete_fg = isinstance(fg, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', fg)
    found = re.fullmatch(r'\s*(\d+)x(\d+)\s*', cell) if isinstance(cell, str) else None
    concrete_cell = found and 2 <= int(found.group(1)) <= 400 and 4 <= int(found.group(2)) <= 800
    if not (concrete_fg and concrete_cell):
        render = load_render()
        if not concrete_fg:
            options['fg'] = render.detect_fg(fg)
        width, height = render.detect_cell(cell if isinstance(cell, str) else 'auto')
        options['cell'] = f'{width}x{height}'
    return options


def timed_out(request):
    """The answer when the daemon took a request and did not answer in time
    (rendering here as well would pass the mod's own time limit)."""
    items = request.get('items') if isinstance(request.get('items'), list) else []
    options = request['options']
    width, height = (int(n) for n in options['cell'].strip().split('x'))
    return {'cell': {'width': width, 'height': height}, 'fg': options['fg'].lower(),
            'results': [{'error': 'renderer timed out'} for _ in items]}


def main(argv):
    in_process = '--in-process' in argv or os.environ.get('INLINE_MATH_NO_DAEMON') == '1'
    if '--detect' in argv:
        return load_render().detect()
    if '--warm' in argv:
        options = stdin_options()
        try:
            status = None if in_process else ask_daemon('warm', {'options': options}, CHECK_TIMEOUT)
        except TimedOut:
            status = None
        return {'daemon': status}
    if '--check' in argv:
        options = resolve_auto(stdin_options())
        try:
            status = None if in_process else ask_daemon('check', {'options': options}, CHECK_TIMEOUT)
        except TimedOut:
            status = {'ok': False, 'problems': ['the renderer daemon did not answer --check in time']}
        if status is None:
            status = load_render().check(options)
            status['daemon'] = None
            if not in_process and refused:
                # Not a fault of the formulas (ok stays as it is), but every batch is slower.
                status['problems'] = status.get('problems', []) + [
                    f'no private folder for the render daemon, so none runs: {"; ".join(refused)}']
        return status
    text = sys.stdin.read()
    try:
        request = json.loads(text or '{}')
    except ValueError:
        request = None
    if isinstance(request, dict) and not in_process:
        request['options'] = resolve_auto(request.get('options'))
        try:
            response = ask_daemon('render', request)
        except TimedOut:
            return timed_out(request)
        if response is not None:
            return response
    render = load_render()
    return render.answer(request) if isinstance(request, dict) else render.answer_text(text)


if __name__ == '__main__':
    sys.stdout.write(json.dumps(main(sys.argv[1:])) + '\n')
