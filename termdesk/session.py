"""The running session: RFB loop, terminal rendering and input, control socket, recording."""
import base64
import io
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time

from .gfx import ESC, Graphics, term_geometry
from .keys import MOD_BITS, MODIFIER_SYMS, char_keysym, off_keymap, parse_combo
from .record import Recorder
from .control import socket_path

AGENT_BADGE_SECONDS = 4.0
DUMP = ["timeout", "-k", "2", "20", "python3", "/home/guest/a11y_dump.py", "--max", "200"]
DUMP_TIMEOUT = 25
FOCUS_FILE = "/tmp/termdesk-focus.json"
VERIFY_SECONDS = 1.0
PASTE_SETTLE = 0.2  # let the app fetch a paste before more keys or a new clipboard follow
SLOW_COMMANDS = {"state", "type", "set_value", "paste", "clipboard", "open"}  # run off the event loop
EATS_LETTERS = {"push button", "toggle button", "menu", "menu item", "tool bar"}
URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# exo-open picks the default app; its output goes nowhere so docker exec returns once the app is launched
OPEN = 'exo-open "$1" </dev/null >/dev/null 2>/tmp/termdesk-open.log || { cat /tmp/termdesk-open.log >&2; exit 1; }'


class NoTree(RuntimeError):
    pass


def _q(text):
    """A JSON string that also escapes the separators str.splitlines() would break on."""
    return json.dumps(text, ensure_ascii=False).replace("\u2028", "\\u2028").replace(
        "\u2029", "\\u2029").replace("\x85", "\\u0085")


def _check_typed(text, got):
    """Compare what was typed with the focused element's text: (verified, want, first_bad_offset, note)."""
    segments = re.split(r"[\t\n\r]", text)
    if text in got:
        return True, text, None, None
    if len(segments) == 1:
        want, note = text, None
    else:
        want, note = segments[-1], "checked only the text after the last tab or newline"
        if not want:
            return None, want, None, "nothing typed after the last tab or newline to check"
        if want in got:
            return True, want, None, note
    if len(got) >= 4000:
        return None, want, None, "the focused text is longer than the 4000 characters the tracker keeps"
    lo, hi = 0, len(want)
    while lo < hi:  # longest prefix of want that shows up in got
        mid = (lo + hi + 1) // 2
        if want[:mid] in got:
            lo = mid
        else:
            hi = mid - 1
    return False, want, lo, note


class Session:
    def __init__(self, rfb, term=None, gfx=None, fps=60, log=None, name=None, addr=None):
        self.rfb, self.term, self.gfx, self.fps, self.log = rfb, term, gfx, fps, log
        self.name = name
        self.addr = addr
        self.headless = term is None
        self.mask = 0
        self.frames = self.patches = 0
        self.need_full = True
        self.resize_pending = False
        self.agent_until = 0.0
        self.actions = 0
        self.sandbox_name = None
        self.sandbox_checked = False
        self.container = None
        self.index_by_key = {}  # stable element indices across state calls
        self.elements = {}  # index -> element dict from the last state
        self.last_state_keys = None
        self.state_lock = threading.Lock()  # one dump at a time; guards the index state
        self.quit_requested = False
        self.recorder = None
        self.last_damage = time.time()
        self.waiters = []  # (conn, deadline, idle_seconds)
        self.conns = {}  # fd -> (sock, buffer)
        self.outgoing = {}  # fd -> (sock, reply bytes, bytes sent)
        self.finished = []  # (conn, reply) from worker threads
        self.finished_lock = threading.Lock()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.listener = None
        if name:
            path = socket_path(name)
            if os.path.exists(path):
                os.unlink(path)
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(path)
            self.listener.listen(64)
            self.listener.setblocking(False)
            self.sock_path = path
        if not self.headless:
            self.layout()
            signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "resize_pending", True))

    # ---------------------------------------------------------------- layout
    def layout(self):
        rows, cols, xpix, ypix = term_geometry()
        self.rows, self.term_cols = rows, cols
        self.cell_w, self.cell_h = xpix / cols, ypix / rows
        box_w, box_h = cols * self.cell_w, (rows - 1) * self.cell_h
        s = min(box_w / self.rfb.w, box_h / self.rfb.h)
        self.cols = max(1, int(self.rfb.w * s / self.cell_w))
        self.img_rows = max(1, int(self.rfb.h * s / self.cell_h))
        self.scale_x = self.cols * self.cell_w / self.rfb.w
        self.scale_y = self.img_rows * self.cell_h / self.rfb.h
        self.need_full = True
        sys.stdout.write(f"{ESC}[{rows};1H{ESC}[2K")
        self._log(f"layout cols={self.cols} rows={self.img_rows} cell={self.cell_w:.2f}x{self.cell_h:.2f} "
                  f"scale={self.scale_x:.4f},{self.scale_y:.4f}")

    def _log(self, text):
        if self.log:
            self.log.write(text + "\n")
            self.log.flush()

    def to_fb(self, x, y):
        if self.term.pixel_mouse:
            return x / self.scale_x, y / self.scale_y
        return (x - 0.5) * self.cell_w / self.scale_x, (y - 0.5) * self.cell_h / self.scale_y

    # ----------------------------------------------------------------- input
    def send_key(self, sym, mods, event):
        rfb = self.rfb
        if sym in MODIFIER_SYMS:
            if event != 2:
                rfb.key(sym, event != 3)
            return
        held = [] if self.term.explicit_mods else [ks for bit, ks in MOD_BITS if mods & bit]
        for ks in held:
            rfb.key(ks, True)
        if event == 1:
            rfb.key(sym, True)
        elif event == 2:
            rfb.key(sym, False)
            rfb.key(sym, True)
        else:
            rfb.key(sym, False)
        if event == 1 and not self.term.explicit_mods:
            rfb.key(sym, False)
        for ks in reversed(held):
            rfb.key(ks, False)

    def handle_input(self):
        motion = None
        for ev in self.term.events():
            kind = ev[0]
            if kind == "quit":
                return False
            if kind == "mouse":
                _, b, x, y, pressed = ev
                fx, fy = self.to_fb(x, y)
                if b & 64:
                    if motion:
                        self.rfb.pointer(*motion, self.mask)
                        motion = None
                    wb = {0: 8, 1: 16, 2: 32, 3: 64}[b & 3]
                    self.rfb.pointer(fx, fy, self.mask | wb)
                    self.rfb.pointer(fx, fy, self.mask)
                elif b & 32:
                    motion = (fx, fy)
                else:
                    if motion:
                        self.rfb.pointer(*motion, self.mask)
                        motion = None
                    bit = 1 << (b & 3)
                    self.mask = (self.mask | bit) if pressed else (self.mask & ~bit)
                    self.rfb.pointer(fx, fy, self.mask)
            elif kind == "key":
                self.send_key(ev[1], ev[2], ev[3])
            elif kind == "gfx":
                if "OK" not in ev[1]:
                    why = self.gfx.on_error(ev[1])
                    self.need_full = True
                    self._log(f"gfx error: {ev[1]} -> {why}")
        if motion:
            self.rfb.pointer(*motion, self.mask)
        return True

    # ---------------------------------------------------------------- render
    def render(self):
        rfb, gfx = self.rfb, self.gfx
        damage, rfb.damage = rfb.damage, []
        if rfb.resized:
            rfb.resized = False
            self.layout()
        area = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in damage)
        sys.stdout.write(f"{ESC}[H")
        full = self.need_full or area > 0.6 * rfb.w * rfb.h
        if full:
            gfx.full(rfb.fb, self.cols, self.img_rows)
            self.need_full = False
        else:
            if len(damage) > 12:
                damage = [(min(d[0] for d in damage), min(d[1] for d in damage),
                           max(d[2] for d in damage), max(d[3] for d in damage))]
            for x0, y0, x1, y1 in damage:
                gfx.patch(rfb.fb.crop((x0, y0, x1, y1)), x0, y0)
                self.patches += 1
        self.frames += 1
        self._log(f"frame {self.frames} rects={len(damage)} area={area} full={full} "
                  f"in={rfb.bytes_in} out={gfx.bytes_out} shm={gfx.shm_ok}")

    def status(self, t0, now):
        el = max(now - t0, 1e-6)
        gfx = self.gfx
        mode = "shm" if gfx.shm_ok else "zlib"
        badge = "  AGENT ACTING" if now < self.agent_until else ""
        rec = "  REC" if self.recorder else ""
        self.term.status(self.rows, self.term_cols, f"{self.rfb.name}{badge}{rec}  {self.rfb.w}x{self.rfb.h}  "
                         f"{self.frames / el:4.1f} fps  {self.rfb.bytes_in / el / 1e3:6.1f} KB/s in  "
                         f"{gfx.bytes_out / el / 1e3:6.1f} KB/s out  {mode}  patches {self.patches}  ctrl+q quits")

    # ------------------------------------------------------ accessibility
    def _find_sandbox(self):
        if self.sandbox_checked:
            return self.sandbox_name
        try:
            from . import sandbox
            port = int((self.addr or "").rpartition(":")[2] or 0)
            for e in sandbox.ls():
                if e.get("port") == port:
                    self.sandbox_name = e["name"]
                    self.container = sandbox._container(e["name"])
            self.sandbox_checked = True
        except Exception:
            self.sandbox_name = None
        return self.sandbox_name

    def _dump_tree(self, args=(), timeout=DUMP_TIMEOUT):
        from . import sandbox
        name = self._find_sandbox()
        if not name:
            raise NoTree("no accessibility tree: this session is not attached to a termdesk sandbox; use screenshot")
        try:
            p = subprocess.run(sandbox._exec_argv(name, DUMP + list(args)), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"accessibility dump gave no answer in {timeout}s (sandbox busy or an app is hung); "
                               "use screenshot and retry state")
        err = p.stderr.decode("utf-8", "replace").strip()[-300:]
        if not p.stdout.strip():
            if p.returncode in (124, 137):
                raise RuntimeError("accessibility dump was killed after 20s in the sandbox (sandbox busy or an app "
                                   "is hung); use screenshot and retry state")
            raise RuntimeError(f"accessibility dump failed (exit {p.returncode}): {err}")
        try:
            return json.loads(p.stdout)
        except ValueError:
            raise RuntimeError(f"accessibility dump returned invalid JSON ({len(p.stdout)} bytes): {err}")

    @staticmethod
    def _elem_key(e):
        return tuple(e.get(k, "") for k in ("app", "role", "name")) + tuple(e.get(k, 0) for k in "xywh")

    @staticmethod
    def _elem_content(e):
        return (e.get("value", ""), e.get("formula", ""), ",".join(e.get("states", [])))

    @staticmethod
    def _elem_line(idx, e):
        line = f'[{idx}] {e.get("role", "")} {_q(e.get("name", ""))}'
        for k in ("value", "formula"):
            if e.get(k):
                line += f" {k}={_q(e[k])}"
        line += f' @{e.get("x", 0)},{e.get("y", 0)} {e.get("w", 0)}x{e.get("h", 0)}'
        if e.get("states"):
            line += f" [{', '.join(e['states'])}]"
        return line

    def _index(self, key):
        idx = self.index_by_key.get(key)
        if idx is None:
            idx = self.index_by_key[key] = len(self.index_by_key) + 1
        return idx

    def get_state(self, full=False, window=None, focused=False, cells=None):
        args = []
        if window:
            args += ["--window", window]
        if focused:
            args.append("--focused-only")
        if cells is not None:
            args += ["--cells", str(int(cells))]
        with self.state_lock:
            return self._render_state(self._dump_tree(args), full, scoped=bool(window or focused))

    def _render_state(self, tree, full, scoped):
        """State text from one dump. Scoped dumps (--window, --focused) list everything and leave the diff
        baseline alone; partial dumps never report elements as removed."""
        windows = tree.get("windows") or []
        active = {w.get("title") for w in windows if w.get("active")}
        current = {}
        for e in tree.get("elements") or []:
            key = self._elem_key(e)
            current[key] = (self._index(key), e)
        elements = dict(self.elements) if scoped else {}
        elements.update(current.values())
        head = ["windows: " + ("; ".join(f'{w.get("app", "")}: "{w.get("title", "")}"' + (" (active)" if w.get("active") else "")
                                         for w in windows if w.get("title") or w.get("active")) or "none")]
        if "focused" in tree:
            f = tree["focused"]
            if isinstance(f, list):
                f = f[0] if f else None
            if f:
                idx = self._index(self._elem_key(f))
                elements[idx] = f
                line = f'[{idx}] {f.get("role", "")} {_q(f.get("name", ""))}'
                if f.get("value") is not None:
                    line += f' value={_q(f["value"])}'
                head.append("focused: " + line)
            else:
                head.append("focused: none")
        partial = bool(tree.get("partial"))
        if partial:
            head.append("note: partial tree: " + ("; ".join(tree.get("errors") or []) or "no reason given"))
        self.elements = elements
        prev = self.last_state_keys
        listing = full or scoped or prev is None
        if listing:
            by_window = {}
            for idx, e in sorted(current.values(), key=lambda ie: ie[0]):
                by_window.setdefault(e.get("window", ""), []).append(self._elem_line(idx, e))
            body = []
            for win, lines in by_window.items():
                body.append(f'== {win or "(no title)"}' + (" (active)" if win in active else ""))
                body.extend(lines)
            changed = True
        else:
            added = [self._elem_line(i, e) for k, (i, e) in current.items() if k not in prev]
            removed = [] if partial else [self._elem_line(i, e) for k, (i, e) in prev.items() if k not in current]
            modified = [self._elem_line(i, e) for k, (i, e) in current.items()
                        if k in prev and self._elem_content(e) != self._elem_content(prev[k][1])]
            changed = bool(added or removed or modified)
            body = ([f"+ {l}" for l in added] + [f"~ {l}" for l in modified] + [f"- {l}" for l in removed]
                    or ["no change since last state"])
        if not scoped:
            self.last_state_keys = {**prev, **current} if partial and prev else current
        return {"ok": True, "state": "\n".join(head + body), "count": len(current), "changed": changed,
                "diff": not listing, "partial": partial}

    def _point(self, req):
        if "index" in req:
            e = self.elements.get(int(req["index"]))
            if not e:
                raise ValueError(f"no element [{req['index']}] in the last state; call `state` again")
            return e["x"] + e["w"] / 2, e["y"] + e["h"] / 2
        return float(req["x"]), float(req["y"])

    # --------------------------------------------------------------- actions
    def _touch_agent(self):
        self.agent_until = time.time() + AGENT_BADGE_SECONDS
        self.actions += 1

    def _keys(self, text, delay=0.0):
        for ch in text:
            sym = char_keysym(ch)
            self.rfb.key(sym, True)
            self.rfb.key(sym, False)
            if delay:
                time.sleep(delay)

    def _combo(self, combo):
        mods, sym = parse_combo(combo)
        for m in mods:
            self.rfb.key(m, True)
        self.rfb.key(sym, True)
        self.rfb.key(sym, False)
        for m in reversed(mods):
            self.rfb.key(m, False)

    def _sandbox_sh(self, script, stdin=None):
        argv = ["docker", "exec"] + (["-i"] if stdin is not None else []) + [
            "-u", "guest", "-e", "DISPLAY=:1", self.container, "sh", "-c", script]
        return subprocess.run(argv, input=stdin, capture_output=True, timeout=10)

    def _set_clipboard(self, text):
        """Put text on the remote clipboard; returns the route: xclip, rfb, or latin-1 when text was squeezed."""
        if self._find_sandbox():
            p = self._sandbox_sh("command -v xclip >/dev/null || exit 127; "
                                 "exec xclip -selection clipboard -i >/dev/null 2>&1", text.encode())
            if p.returncode == 0:
                return "xclip"
            if p.returncode != 127:
                self._log(f"xclip -i exited {p.returncode}: {p.stderr[-200:]!r}")
        return "rfb" if self.rfb.cut_text(text) else "latin-1"

    def _get_clipboard(self):
        if self._find_sandbox():
            p = self._sandbox_sh("command -v xclip >/dev/null || exit 127; exec xclip -selection clipboard -o")
            if p.returncode == 0:
                return p.stdout.decode("utf-8", "replace"), "xclip"
            if b"not available" in p.stderr:  # the clipboard is empty
                return "", "xclip"
        return self.rfb.clipboard or "", "rfb"

    def _read_focus(self):
        """The sandbox focus tracker's record and its age in seconds, or (None, None) without one."""
        if not self._find_sandbox():
            return None, None
        try:
            p = self._sandbox_sh(f"cat {FOCUS_FILE} && echo && date +%s.%N")
            body, _, now = p.stdout.decode("utf-8", "replace").strip().rpartition("\n")
            f = json.loads(body)
            return f, float(now) - float(f.get("ts", 0))
        except (subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
            return None, None

    def _live_text(self, f):
        """The focused element's text from a fresh dump of its window. The tracker only hears text events,
        and some edits (a paste into a spreadsheet cell) send none."""
        try:
            tree = self._dump_tree(["--window", f.get("window") or "", "--deadline", "4"], timeout=10)
        except RuntimeError:
            return None
        same = [e for e in tree.get("elements") or []
                if (e.get("app"), e.get("role"), e.get("name")) == (f.get("app"), f.get("role"), f.get("name"))]
        same.sort(key=lambda e: "focused" not in e.get("states", []))
        return same[0].get("value", "") if same else None

    def _verify(self, text, t0):
        """Poll the focus tracker until the typed text shows up in the focused element, for up to a second."""
        deadline = time.time() + VERIFY_SECONDS
        while True:
            f, age = self._read_focus()
            if f is None:
                return {"verified": None}
            got = f.get("text")
            verified, want, bad, note = _check_typed(text, got or "")
            if (verified and age <= time.time() - t0) or time.time() >= deadline:
                break
            time.sleep(0.1)
        if verified is False and got is not None:
            live = self._live_text(f)
            if live is not None:
                got = live
                verified, want, bad, note = _check_typed(text, got)
        res = {"focused": {k: f.get(k) or "" for k in ("role", "name", "app")}, "verified": verified}
        if got is None:
            res["verified"], note = None, "the focused element exposes no text"
        elif verified is False:
            p = max(0, got.find(want[:bad]) - 60)
            res.update(want=want, got=got[p:p + 300], first_bad_offset=bad)
        if note:
            res["note"] = note
        if f.get("role") in EATS_LETTERS and re.split(r"[\t\n\r]", text)[-1]:
            res["warning"] = (f'focus is on {f["role"]} {_q(f.get("name") or "")}, where typed letters get lost; '
                              "click the text field first")
        return res

    def _type(self, req):
        """Type as keysyms, or through the clipboard when text is off the US keymap; verify it in a sandbox."""
        text = req.get("text", "").replace("\r\n", "\n")
        mode = req.get("mode")
        t0 = time.time()
        paste = mode == "paste" or (mode != "keys" and off_keymap(text))
        resp = {"ok": True, "chars": len(text), "via": "paste" if paste else "keys"}
        if paste and mode != "paste" and not self._find_sandbox() and not self.rfb.clip_ext \
                and any(ord(c) > 255 for c in text):
            paste, resp["via"] = False, "keys"
            resp["warning"] = "this VNC server has no UTF-8 clipboard, so characters off the keymap went as keysyms"
        if paste:
            f, _ = self._read_focus()
            terminal = f and (f.get("role") == "terminal" or "terminal" in (f.get("app") or "").lower())
            pieces = [p for p in re.split(r"([\t\n\r])", text) if p]
            for i, piece in enumerate(pieces):
                if piece in ("\t", "\n", "\r"):
                    self._keys(piece)
                    continue
                if self._set_clipboard(piece) == "latin-1":
                    resp["warning"] = "this VNC server only takes Latin-1 on the clipboard; other characters became ?"
                self._combo("ctrl+shift+v" if terminal else "ctrl+v")
                if i < len(pieces) - 1:
                    time.sleep(PASTE_SETTLE)
        else:
            self._keys(text, float(req.get("delay_ms") or 0) / 1000)
        if self._find_sandbox():
            resp.update(self._verify(text, t0))
        else:
            resp["verified"] = None
        return resp

    def _open(self, target):
        """Open a URL, a file in the sandbox, or a host file (copied to /home/guest first) in its default app."""
        from . import sandbox
        name = self._find_sandbox()
        if not name:
            raise RuntimeError("open needs a termdesk sandbox; on other desktops start the app from its menu")
        resp = {"ok": True}
        if not URL.match(target) and not target.startswith(sandbox.HOME + "/") and os.path.exists(target):
            resp["copied_from"] = target
            target = sandbox.cp(target, name + ":" + sandbox.HOME + "/")
        p = subprocess.run(sandbox._exec_argv(name, ["sh", "-c", OPEN, "sh", target]), capture_output=True, timeout=30)
        if p.returncode:
            raise RuntimeError(f"could not open {target}: " + p.stderr.decode("utf-8", "replace").strip()[-300:])
        resp["opened"] = target
        return resp

    def do_action(self, req):
        """Execute one control request; returns a response dict or None to keep the caller waiting."""
        cmd = req.get("cmd")
        rfb = self.rfb
        if cmd == "info":
            return {"ok": True, "target": rfb.name, "addr": self.addr, "width": rfb.w, "height": rfb.h, "frames": self.frames,
                    "headless": self.headless, "recording": bool(self.recorder), "actions": self.actions}
        if cmd == "done":
            self.agent_until = 0.0
            return {"ok": True}
        if cmd == "quit":
            self.quit_requested = True
            return {"ok": True}
        if cmd == "state":
            return self.get_state(full=bool(req.get("full")), window=req.get("window"),
                                  focused=bool(req.get("focused")), cells=req.get("cells"))
        if cmd == "clipboard":
            text, via = self._get_clipboard()
            return {"ok": True, "text": text, "via": via}
        if cmd == "screenshot":
            img = rfb.fb
            scale = float(req.get("scale") or 1)
            if scale != 1:
                img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
            path = req.get("path")
            if path:
                img.save(os.path.expanduser(path))
                return {"ok": True, "path": path, "width": img.width, "height": img.height, "scale": scale}
            buf = io.BytesIO()
            img.save(buf, "PNG", compress_level=3)
            return {"ok": True, "png_base64": base64.b64encode(buf.getvalue()).decode(),
                    "width": img.width, "height": img.height, "scale": scale}
        self._touch_agent()
        if cmd == "set_value":
            x, y = self._point(req)
            rfb.pointer(x, y, self.mask)
            rfb.pointer(x, y, self.mask | 1)
            rfb.pointer(x, y, self.mask)
            time.sleep(0.15)
            self._combo("ctrl+a")
            return self._type(req)
        if cmd in ("click", "move", "mousedown", "mouseup"):
            x, y = self._point(req)
            button = {"left": 1, "middle": 2, "right": 4}[req.get("button", "left")]
            rfb.pointer(x, y, self.mask)
            if cmd == "click":
                for _ in range(int(req.get("count", 1))):
                    rfb.pointer(x, y, self.mask | button)
                    rfb.pointer(x, y, self.mask)
            elif cmd == "mousedown":
                self.mask |= button
                rfb.pointer(x, y, self.mask)
            elif cmd == "mouseup":
                self.mask &= ~button
                rfb.pointer(x, y, self.mask)
            return {"ok": True}
        if cmd == "drag":
            x, y, tx, ty = (float(req[k]) for k in ("x", "y", "to_x", "to_y"))
            button = {"left": 1, "middle": 2, "right": 4}[req.get("button", "left")]
            rfb.pointer(x, y, self.mask)
            rfb.pointer(x, y, self.mask | button)
            steps = 12
            for i in range(1, steps + 1):
                rfb.pointer(x + (tx - x) * i / steps, y + (ty - y) * i / steps, self.mask | button)
            rfb.pointer(tx, ty, self.mask)
            return {"ok": True}
        if cmd == "scroll":
            x, y = self._point(req)
            dy, dx = int(req.get("dy", 0)), int(req.get("dx", 0))
            rfb.pointer(x, y, self.mask)
            for b, n in ((16 if dy > 0 else 8, abs(dy)), (64 if dx > 0 else 32, abs(dx))):
                for _ in range(n):
                    rfb.pointer(x, y, self.mask | b)
                    rfb.pointer(x, y, self.mask)
            return {"ok": True}
        if cmd == "type":
            return self._type(req)
        if cmd == "open":
            return self._open(req["target"])
        if cmd == "key":
            self._combo(req["combo"])
            return {"ok": True}
        if cmd == "paste":
            text = req.get("text", "")
            resp = {"ok": True, "chars": len(text), "via": self._set_clipboard(text)}
            if resp["via"] == "latin-1":
                resp["warning"] = "this VNC server only takes Latin-1 on the clipboard; other characters became ?"
            return resp
        if cmd == "wait":
            return None  # resolved by the loop after req["ms"]
        if cmd == "wait_idle":
            return None
        if cmd == "record":
            if req.get("action") == "start":
                if self.recorder:
                    return {"ok": False, "error": "already recording"}
                self.recorder = Recorder(os.path.expanduser(req.get("path") or "termdesk.gif"),
                                         fps=float(req.get("fps") or 10))
                self.recorder.tick(rfb.fb, force=True)
                return {"ok": True, "path": self.recorder.path}
            if req.get("action") == "stop":
                if not self.recorder:
                    return {"ok": False, "error": "not recording"}
                result = self.recorder.stop(rfb.fb)
                self.recorder = None
                result["ok"] = True
                return result
            return {"ok": False, "error": "record needs action start|stop"}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    # ------------------------------------------------------- control socket
    def _accept(self):
        try:
            conn, _ = self.listener.accept()
        except BlockingIOError:
            return
        conn.setblocking(False)
        self.conns[conn.fileno()] = (conn, bytearray())

    def _reply(self, conn, resp):
        self.outgoing[conn.fileno()] = (conn, json.dumps(resp).encode() + b"\n", 0)
        self._flush(conn.fileno())

    def _flush(self, fd):
        """Send what the socket takes now; the loop sends the rest when select says it is writable."""
        conn, data, sent = self.outgoing.pop(fd)
        try:
            while sent < len(data):
                sent += conn.send(memoryview(data)[sent:])
        except BlockingIOError:
            self.outgoing[fd] = (conn, data, sent)
            return
        except OSError as e:
            self._log(f"control reply cut off after {sent} of {len(data)} bytes: {e}")
        conn.close()

    def _run(self, req):
        try:
            return self.do_action(req)
        except Exception as e:
            resp = {"ok": False, "error": str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"}
            if isinstance(e, NoTree):
                resp["permanent"] = True
            return resp

    def _work(self, conn, req):
        resp = self._run(req)
        with self.finished_lock:
            self.finished.append((conn, resp))
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    def _collect(self):
        try:
            while os.read(self.wake_r, 4096):
                pass
        except BlockingIOError:
            pass
        with self.finished_lock:
            done, self.finished = self.finished, []
        for conn, resp in done:
            self._reply(conn, resp)

    def _serve(self, fd):
        conn, buf = self.conns[fd]
        try:
            data = conn.recv(1 << 20)
        except OSError:
            data = b""
        if not data:
            del self.conns[fd]
            conn.close()
            return
        buf += data
        if b"\n" not in buf:
            return
        line, _, _ = bytes(buf).partition(b"\n")
        del self.conns[fd]
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
        except (ValueError, AttributeError):
            self._reply(conn, {"ok": False, "error": "bad request: send one JSON object per line"})
            return
        if cmd in SLOW_COMMANDS:  # these shell out to docker; the loop keeps serving meanwhile
            threading.Thread(target=self._work, args=(conn, req), daemon=True).start()
            return
        resp = self._run(req)
        if resp is None:
            now = time.time()
            if cmd == "wait":
                self.waiters.append((conn, now + float(req.get("ms", 500)) / 1000, None))
            else:
                idle = float(req.get("idle_ms", 300)) / 1000
                timeout = float(req.get("timeout_ms", 5000)) / 1000
                self.last_damage = max(self.last_damage, now)  # idleness counts from the request
                self.waiters.append((conn, now + timeout, idle))
            return
        self._reply(conn, resp)

    def _check_waiters(self, now):
        keep = []
        for conn, deadline, idle in self.waiters:
            if idle is None:
                if now >= deadline:
                    self._reply(conn, {"ok": True})
                    continue
            elif now - self.last_damage >= idle:
                self._reply(conn, {"ok": True, "idle_ms": int((now - self.last_damage) * 1000)})
                continue
            elif now >= deadline:
                self._reply(conn, {"ok": True, "timed_out": True})
                continue
            keep.append((conn, deadline, idle))
        self.waiters = keep

    def _next_wait_deadline(self):
        if not self.waiters:
            return None
        return min(dl for _, dl, _ in self.waiters)

    # ------------------------------------------------------------------ loop
    def close(self):
        for fd in list(self.outgoing):
            conn, data, sent = self.outgoing.pop(fd)
            try:
                conn.settimeout(2)
                conn.sendall(memoryview(data)[sent:])
            except OSError as e:
                self._log(f"control reply dropped at exit: {e}")
            conn.close()
        if self.recorder:
            try:
                self.recorder.stop(self.rfb.fb)
            except Exception:
                pass
        if self.listener:
            self.listener.close()
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass

    def run(self):
        rfb, term = self.rfb, self.term
        rfb.request(incremental=False)
        t0 = last = time.time()
        last_stat = 0.0
        pending = True
        interval = 1 / self.fps
        try:
            while True:
                dirty = bool(rfb.damage) or self.need_full
                wait = max(0.0, last + interval - time.time()) if dirty and not self.headless else 1.0
                dl = self._next_wait_deadline()
                if dl is not None:
                    wait = min(wait, max(0.0, dl - time.time()))
                if self.waiters and any(idle is not None for _, _, idle in self.waiters):
                    wait = min(wait, 0.05)
                fds = [rfb.sock, self.wake_r] + ([term.fd] if term else []) + ([self.listener] if self.listener else [])
                fds += [c for c, _ in self.conns.values()]
                r, w, _ = select.select(fds, [c for c, _, _ in self.outgoing.values()], [], wait)
                if rfb.sock in r:
                    if rfb.handle_message():
                        pending = False
                    while rfb.pending():
                        if rfb.handle_message():
                            pending = False
                if term and term.fd in r and not self.handle_input():
                    return
                if self.listener in r:
                    self._accept()
                for c, _ in list(self.conns.values()):
                    if c in r:
                        self._serve(c.fileno())
                if self.wake_r in r:
                    self._collect()
                for c in w:
                    if c.fileno() in self.outgoing:
                        self._flush(c.fileno())
                if self.quit_requested:
                    return
                now = time.time()
                if rfb.damage:
                    self.last_damage = now
                if self.resize_pending:
                    self.resize_pending = False
                    self.layout()
                if self.recorder and rfb.damage:
                    self.recorder.tick(rfb.fb)
                if self.headless:
                    if rfb.damage:
                        rfb.damage = []
                        rfb.resized = False
                else:
                    if (rfb.damage or self.need_full) and now - last >= interval:
                        self.render()
                        if now - last_stat > 0.5:
                            last_stat = now
                            self.status(t0, now)
                        sys.stdout.flush()
                        self.gfx.reap_shm()
                        last = now
                self._check_waiters(now)
                if rfb.continuous and not rfb.continuous_on:
                    rfb.enable_continuous()
                elif not pending and not rfb.continuous_on:
                    rfb.request(incremental=True)
                    pending = True
        finally:
            self.close()
