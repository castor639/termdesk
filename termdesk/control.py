"""Control socket shared by the running session (server) and `termdesk action` (client).

Protocol: one JSON object per line in each direction over a Unix socket.
"""
import json
import os
import socket


def socket_dir():
    d = os.path.join(os.path.expanduser("~"), ".local", "state", "termdesk")
    os.makedirs(d, exist_ok=True)
    return d


def socket_path(name):
    return os.path.join(socket_dir(), name + ".sock")


def name_for_target(target):
    return target.replace(":", "-").replace("/", "_")


def _ping(path, timeout=1.0):
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
            return json.loads(data)
    except (OSError, ValueError):
        return None


def list_sessions():
    """Live sessions as dicts; stale sockets are removed."""
    out = []
    for fn in sorted(os.listdir(socket_dir())):
        if not fn.endswith(".sock"):
            continue
        path = os.path.join(socket_dir(), fn)
        info = _ping(path)
        if info and info.get("ok"):
            info["name"] = fn[:-5]
            out.append(info)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass
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


class Client:
    def __init__(self, name=None, timeout=60.0):
        self.path = resolve(name)
        self.timeout = timeout

    def call(self, cmd, **kw):
        req = dict(kw)
        req["cmd"] = cmd
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect(self.path)
            s.sendall(json.dumps(req).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                data += chunk
        if not data:
            raise RuntimeError("session closed the connection")
        resp = json.loads(data)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "unknown error"))
        return resp
