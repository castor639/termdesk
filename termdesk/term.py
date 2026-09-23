"""Terminal side: raw mode, kitty keyboard protocol and SGR-pixel mouse decoding."""
import os
import re
import sys
import termios
import tty

from .keys import XK, CSIU, CSI_FINAL, CSI_TILDE, MODIFIER_SYMS

ESC = "\x1b"
RE_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
RE_CSIU = re.compile(rb"\x1b\[(\d+)(?::(\d*))?(?::(\d*))?(?:;(\d+)(?::(\d+))?)?(?:;(\d+))?u")
RE_CSI = re.compile(rb"\x1b\[(\d+)?(?:;(\d+)(?::(\d+))?)?([A-Z~])")
RE_GFX = re.compile(rb"\x1b_G([^\x1b]*)\x1b\\")
RE_DECRQM = re.compile(rb"\x1b\[\?(\d+);(\d)\$y")


class Term:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self.buf = b""
        self.pixel_mouse = False
        self.explicit_mods = False
        w = sys.stdout.write
        w(f"{ESC}[?1049h{ESC}[?25l{ESC}[H{ESC}[2J")
        w(f"{ESC}[?1003h{ESC}[?1006h{ESC}[?1016h")
        w(f"{ESC}[>15u")  # disambiguate + event types + alternate keys + all keys as escapes
        w(f"{ESC}[?1016$p")
        sys.stdout.flush()

    def close(self):
        w = sys.stdout.write
        w(f"{ESC}_Ga=d,d=A,q=2{ESC}\\{ESC}[<u{ESC}[?1016l{ESC}[?1006l{ESC}[?1003l{ESC}[?25h{ESC}[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def status(self, row, cols, text):
        sys.stdout.write(f"{ESC}[{row};1H{ESC}[2K{ESC}[7m {text[:max(0, cols - 2)]} {ESC}[0m")

    def events(self):
        """Yield ('mouse', b, x, y, pressed) | ('key', keysym, mods, event) | ('gfx', text) | ('quit',)."""
        try:
            self.buf += os.read(self.fd, 65536)
        except BlockingIOError:
            return
        while self.buf:
            m = RE_GFX.match(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                yield ("gfx", m.group(1).decode(errors="replace"))
                continue
            m = RE_DECRQM.match(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                if m.group(1) == b"1016":
                    self.pixel_mouse = m.group(2) in b"12"
                continue
            m = RE_MOUSE.match(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                yield ("mouse", int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == b"M")
                continue
            m = RE_CSIU.match(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                code = int(m.group(1))
                shifted = int(m.group(2)) if m.group(2) else None
                mods = int(m.group(4) or 1) - 1
                event = int(m.group(5) or 1)  # 1 press, 2 repeat, 3 release
                if code == ord("q") and mods & 4 and event != 3:
                    yield ("quit",)
                    return
                sym = CSIU.get(code)
                if sym is None:
                    cp = shifted if (shifted and mods & 1) else code
                    sym = cp if cp < 0x100 else 0x01000000 | cp
                if sym in MODIFIER_SYMS:
                    self.explicit_mods = True
                yield ("key", sym, mods, event)
                continue
            m = RE_CSI.match(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                final = m.group(4).decode()
                mods = int(m.group(2) or 1) - 1
                event = int(m.group(3) or 1)
                sym = CSI_TILDE.get(int(m.group(1) or 0)) if final == "~" else CSI_FINAL.get(final)
                if sym:
                    yield ("key", sym, mods, event)
                continue
            if self.buf[:1] == b"\x1b" and len(self.buf) > 1 and self.buf[1:2] in b"[_":
                if len(self.buf) > 4096:
                    self.buf = self.buf[1:]
                    continue
                return
            c = self.buf[0]
            self.buf = self.buf[1:]
            if c == 0x11:
                yield ("quit",)
                return
            if c in (0x0D, 0x0A):
                yield ("key", XK["ret"], 0, 1)
            elif c == 0x09:
                yield ("key", XK["tab"], 0, 1)
            elif c in (0x7F, 0x08):
                yield ("key", XK["bs"], 0, 1)
            elif c == 0x1B:
                yield ("key", XK["esc"], 0, 1)
            elif c < 0x20:
                yield ("key", c + 0x60, 4, 1)
            else:
                yield ("key", c, 0, 1)
