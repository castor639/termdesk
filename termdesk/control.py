"""Control socket shared by the running session (server) and `termdesk action` (client).

Protocol: one JSON object per line in each direction over a Unix socket.
"""
import json
import os
import socket
import subprocess
import sys
import time


def socket_dir():
    d = os.path.join(os.path.expanduser("~"), ".local", "state", "termdesk")
    os.makedirs(d, exist_ok=True)
    return d


def socket_path(name):
    return os.path.join(socket_dir(), name + ".sock")


def name_for_target(target):
    return target.replace(":", "-").replace("/", "_")


def _ping(path, timeout=1.0):
    """The session's info, {"busy": True} when it does not answer in time, or None when nobody listens."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(b'{"cmd":"info"}\n')
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        info = json.loads(data)
        return info if info.get("ok") else {"ok": True, "busy": True}
    except (ConnectionRefusedError, FileNotFoundError):
        return None
    except (OSError, ValueError):
        return {"ok": True, "busy": True}


def list_sessions():
    """Sessions as dicts, busy ones with busy=True. Only sockets nobody listens on are removed."""
    out = []
    for fn in sorted(os.listdir(socket_dir())):
        if not fn.endswith(".sock"):
            continue
        path = os.path.join(socket_dir(), fn)
        info = _ping(path)
        if info is None:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        info["name"] = fn[:-5]
        out.append(info)
    return out


def resolve(name=None):
    if name:
        path = socket_path(name)
        if not os.path.exists(path):
            raise RuntimeError(f"no session named {name!r}; run `termdesk ls`")
        return path
    live = list_sessions()
    if len(live) == 1:
        return socket_path(live[0]["name"])
    if not live:
        raise RuntimeError("no termdesk session is running; start one with `termdesk <host:port>`")
    names = ", ".join(s["name"] for s in live)
    raise RuntimeError(f"several sessions are running ({names}); pick one with --name")


def start_headless(target, name, timeout=20.0):
    """Run a background session on target and wait until it answers; returns its info."""
    log = os.path.join(socket_dir(), name + ".log")
    p = subprocess.run([sys.executable, "-m", "termdesk", target, "--headless", "--daemon", "--name", name,
                        "--log", log], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr.strip().replace("termdesk: ", "", 1) or f"session {name} did not start")
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _ping(socket_path(name)) if os.path.exists(socket_path(name)) else None
        if info and not info.get("busy"):
            info["name"] = name
            return info
        time.sleep(0.2)
    try:
        with open(log) as f:
            tail = f.read()[-400:].strip()
    except OSError:
        tail = ""
    raise RuntimeError(f"session {name} did not answer within {timeout:g}s" + (f"; its log says: {tail}" if tail else ""))


class SessionError(RuntimeError):
    """An error reply; permanent means retrying cannot help (e.g. no accessibility tree on this target)."""

    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


class Client:
    def __init__(self, name=None, timeout=60.0):
        self.path = resolve(name)
        self.timeout = timeout

    def call(self, cmd, **kw):
        req = dict(kw)
        req["cmd"] = cmd
        data = bytearray()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            try:
                s.connect(self.path)
                s.sendall(json.dumps(req).encode() + b"\n")
                while not data.endswith(b"\n"):
                    chunk = s.recv(1 << 20)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                raise RuntimeError(f"session did not answer {cmd} within {self.timeout:g}s")
        if not data:
            raise RuntimeError("session closed the connection without a reply")
        if not data.endswith(b"\n"):
            raise RuntimeError(f"session reply was cut off after {len(data)} bytes")
        try:
            resp = json.loads(data)
        except ValueError:
            raise RuntimeError(f"session reply is not valid JSON ({len(data)} bytes)")
        if not resp.get("ok"):
            raise SessionError(resp.get("error", "unknown error"), bool(resp.get("permanent")))
        return resp
