"""The running session: RFB loop, terminal rendering and input, control socket, recording."""
import base64
import io
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time

from .gfx import ESC, Graphics, term_geometry
from .keys import MOD_BITS, MODIFIER_SYMS, char_keysym, parse_combo
from .record import Recorder
from .control import socket_path

AGENT_BADGE_SECONDS = 4.0


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
        self.index_by_key = {}  # stable element indices across state calls
        self.elements = {}  # index -> element dict from the last state
        self.last_state_keys = None
        self.recorder = None
        self.last_damage = time.time()
        self.waiters = []  # (conn, deadline, idle_seconds)
        self.conns = {}  # fd -> (sock, buffer)
        self.listener = None
        if name:
            path = socket_path(name)
            if os.path.exists(path):
                os.unlink(path)
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(path)
            self.listener.listen(8)
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
        self.sandbox_checked = True
        try:
            from . import sandbox
            port = int((self.addr or "").rpartition(":")[2] or 0)
            for e in sandbox.ls():
                if e.get("port") == port:
                    self.sandbox_name = e["name"]
        except Exception:
            self.sandbox_name = None
        return self.sandbox_name

    def _dump_tree(self):
        from . import sandbox
        name = self._find_sandbox()
        if not name:
            raise RuntimeError("no accessibility tree: this session is not attached to a termdesk sandbox; use screenshot")
        cmd = sandbox._exec_argv(name, ["python3", "/home/guest/a11y_dump.py", "--max", "200"])
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            raise RuntimeError("accessibility dump failed: " + p.stderr.strip()[-300:])
        return json.loads(p.stdout)

    @staticmethod
    def _elem_key(e):
        return (e["app"], e["role"], e["name"], e["x"], e["y"], e["w"], e["h"])

    @staticmethod
    def _elem_content(e):
        return (e.get("value", ""), ",".join(e.get("states", [])))

    def _elem_line(self, idx, e):
        extra = f' value="{e["value"]}"' if e.get("value") else ""
        states = f" [{', '.join(e['states'])}]" if e.get("states") else ""
        return f'[{idx}] {e["role"]} "{e["name"]}"{extra} @{e["x"]},{e["y"]} {e["w"]}x{e["h"]}{states}'

    def get_state(self, full=False):
        tree = self._dump_tree()
        active = [w["title"] for w in tree["windows"] if w["active"]]
        current = {}
        self.elements = {}
        for e in tree["elements"]:
            key = self._elem_key(e)
            idx = self.index_by_key.get(key)
            if idx is None:
                idx = max(self.index_by_key.values(), default=0) + 1
                self.index_by_key[key] = idx
            current[key] = (idx, e)
            self.elements[idx] = e
        windows = [f'{w["app"]}: "{w["title"]}"' + (" (active)" if w["active"] else "")
                   for w in tree["windows"] if w["title"] or w["active"]]
        head = "windows: " + ("; ".join(windows) or "none")
        by_window = {}
        for key, (idx, e) in sorted(current.items(), key=lambda kv: kv[1][0]):
            by_window.setdefault(e["window"], []).append(self._elem_line(idx, e))
        if full or self.last_state_keys is None:
            body = []
            for win, lines in by_window.items():
                body.append(f'== {win or "(no title)"}' + (" (active)" if win in active else ""))
                body.extend(lines)
            text = head + "\n" + "\n".join(body)
            changed = True
        else:
            prev = self.last_state_keys
            added = [self._elem_line(i, e) for k, (i, e) in current.items() if k not in prev]
            removed = [self._elem_line(i, e) for k, (i, e) in prev.items() if k not in current]
            modified = [self._elem_line(i, e) for k, (i, e) in current.items()
                        if k in prev and self._elem_content(e) != self._elem_content(prev[k][1])]
            changed = bool(added or removed or modified)
            if not changed:
                text = head + "\nno change since last state"
            else:
                text = head + "\n" + "\n".join([f"+ {l}" for l in added] + [f"~ {l}" for l in modified]
                                                + [f"- {l}" for l in removed])
        self.last_state_keys = current
        return {"ok": True, "state": text, "count": len(current), "changed": changed, "diff": not full and self.last_state_keys is not None}

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
        if cmd == "state":
            return self.get_state(full=bool(req.get("full")))
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
            for sym, down in ((0xFFE3, True), (ord("a"), True), (ord("a"), False), (0xFFE3, False)):
                rfb.key(sym, down)
            for ch in req.get("text", ""):
                sym = char_keysym(ch)
                rfb.key(sym, True)
                rfb.key(sym, False)
            return {"ok": True, "chars": len(req.get("text", ""))}
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
            for ch in req.get("text", ""):
                sym = char_keysym(ch)
                rfb.key(sym, True)
                rfb.key(sym, False)
            return {"ok": True, "chars": len(req.get("text", ""))}
        if cmd == "key":
            mods, sym = parse_combo(req["combo"])
            for m in mods:
                rfb.key(m, True)
            rfb.key(sym, True)
            rfb.key(sym, False)
            for m in reversed(mods):
                rfb.key(m, False)
            return {"ok": True}
        if cmd == "paste":
            rfb.cut_text(req.get("text", ""))
            return {"ok": True}
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
        try:
            conn.sendall(json.dumps(resp).encode() + b"\n")
        except OSError:
            pass
        conn.close()

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
            resp = self.do_action(req)
        except Exception as e:
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if resp is None:
            now = time.time()
            if req.get("cmd") == "wait":
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
                fds = [rfb.sock] + ([term.fd] if term else []) + ([self.listener] if self.listener else [])
                fds += [c for c, _ in self.conns.values()]
                r, _, _ = select.select(fds, [], [], wait)
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
