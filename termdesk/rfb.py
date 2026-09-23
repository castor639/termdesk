"""RFB (VNC) client: handshake, framebuffer, encodings, input messages."""
import select
import socket
import struct
import zlib

from PIL import Image

try:
    import numpy as np
except ImportError:  # ZRLE needs numpy; everything else works without it
    np = None

ENC_RAW, ENC_COPYRECT, ENC_ZLIB, ENC_ZRLE = 0, 1, 6, 16
ENC_DESKTOPSIZE, ENC_CONTINUOUS = -223, -313


class RFB:
    def __init__(self, host, port, encodings=None):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = bytearray()
        self.pos = 0
        self.bytes_in = 0
        ver = self._read(12)
        if not ver.startswith(b"RFB "):
            raise RuntimeError(f"not an RFB server: {ver!r}")
        self.sock.sendall(b"RFB 003.008\n")
        n = self._read(1)[0]
        if n == 0:
            raise RuntimeError(self._read_reason())
        types = self._read(n)
        if 1 not in types:
            raise RuntimeError(
                f"server requires authentication (types {list(types)}); tunnel over ssh to a "
                "server started without a password, e.g. Xvnc -SecurityTypes None")
        self.sock.sendall(b"\x01")
        if struct.unpack(">I", self._read(4))[0] != 0:
            raise RuntimeError("security handshake failed: " + self._read_reason())
        self.sock.sendall(b"\x01")  # ClientInit: shared session
        self.w, self.h = struct.unpack(">HH", self._read(4))
        self._read(16)
        self.name = self._read(struct.unpack(">I", self._read(4))[0]).decode(errors="replace")
        # 32bpp depth 24 little-endian true colour, R<<16 G<<8 B: memory order B G R X
        pf = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self.sock.sendall(b"\x00\x00\x00\x00" + pf)
        if encodings is None:
            encodings = ([ENC_ZRLE] if np is not None else []) + [ENC_ZLIB, ENC_COPYRECT, ENC_RAW]
        encs = list(encodings) + [ENC_DESKTOPSIZE, ENC_CONTINUOUS]
        self.sock.sendall(struct.pack(">BxH", 2, len(encs)) + struct.pack(f">{len(encs)}i", *encs))
        self.zstream = zlib.decompressobj()
        self.fb = Image.new("RGB", (self.w, self.h), "black")
        self.damage = []  # list of (x0, y0, x1, y1)
        self.resized = False
        self.continuous = False  # server supports it
        self.continuous_on = False

    def _fill(self):
        chunk = self.sock.recv(1 << 20)
        if not chunk:
            raise ConnectionError("server closed connection")
        if self.pos > (1 << 20):
            del self.buf[:self.pos]
            self.pos = 0
        self.buf += chunk
        self.bytes_in += len(chunk)

    def _read(self, n):
        while len(self.buf) - self.pos < n:
            self._fill()
        data = bytes(self.buf[self.pos:self.pos + n])
        self.pos += n
        return data

    def pending(self):
        """True when another message can be read without blocking."""
        return len(self.buf) > self.pos or bool(select.select([self.sock], [], [], 0)[0])

    def _read_reason(self):
        return self._read(struct.unpack(">I", self._read(4))[0]).decode(errors="replace")

    def request(self, incremental=True):
        self.sock.sendall(struct.pack(">BBHHHH", 3, int(incremental), 0, 0, self.w, self.h))

    def enable_continuous(self):
        self.sock.sendall(struct.pack(">BBHHHH", 150, 1, 0, 0, self.w, self.h))
        self.continuous_on = True

    def pointer(self, x, y, mask):
        x = min(max(int(x), 0), self.w - 1)
        y = min(max(int(y), 0), self.h - 1)
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))

    def key(self, keysym, down):
        self.sock.sendall(struct.pack(">BBxxI", 4, int(down), keysym))

    def cut_text(self, text):
        data = text.encode("latin-1", "replace")
        self.sock.sendall(struct.pack(">BxxxI", 6, len(data)) + data)

    def _paste(self, x, y, w, h, data):
        self.fb.paste(Image.frombuffer("RGB", (w, h), data, "raw", "BGRX", 0, 1), (x, y))
        self.damage.append((x, y, x + w, y + h))

    def handle_message(self):
        """Consume one server message. Returns True when a framebuffer update finished."""
        t = self._read(1)[0]
        if t == 0:
            self._read(1)
            for _ in range(struct.unpack(">H", self._read(2))[0]):
                x, y, w, h, enc = struct.unpack(">HHHHi", self._read(12))
                if enc == ENC_RAW:
                    self._paste(x, y, w, h, self._read(w * h * 4))
                elif enc == ENC_ZLIB:
                    n = struct.unpack(">I", self._read(4))[0]
                    self._paste(x, y, w, h, self.zstream.decompress(self._read(n)))
                elif enc == ENC_ZRLE:
                    n = struct.unpack(">I", self._read(4))[0]
                    tile_data = self.zstream.decompress(self._read(n))
                    self.fb.paste(Image.fromarray(zrle_decode(tile_data, w, h)), (x, y))
                    self.damage.append((x, y, x + w, y + h))
                elif enc == ENC_COPYRECT:
                    sx, sy = struct.unpack(">HH", self._read(4))
                    self.fb.paste(self.fb.crop((sx, sy, sx + w, sy + h)), (x, y))
                    self.damage.append((x, y, x + w, y + h))
                elif enc == ENC_DESKTOPSIZE:
                    self.w, self.h = w, h
                    self.fb = Image.new("RGB", (w, h), "black")
                    self.damage = [(0, 0, w, h)]
                    self.resized = True
                else:
                    raise RuntimeError(f"unsupported encoding {enc}")
            return True
        if t == 1:
            self._read(1)
            _, n = struct.unpack(">HH", self._read(4))
            self._read(n * 6)
        elif t == 2:
            pass  # bell
        elif t == 3:
            self._read(3)
            self._read(struct.unpack(">I", self._read(4))[0])
        elif t == 150:
            self.continuous = True
        else:
            raise RuntimeError(f"unknown server message {t}")
        return False


def zrle_decode(d, w, h):
    """Decode one ZRLE rectangle (already inflated) into an RGB uint8 array.

    Pixels arrive as 3-byte CPIXELs in B,G,R order for our pixel format.
    """
    out = np.empty((h, w, 3), np.uint8)
    o = 0
    for ty in range(0, h, 64):
        th = min(64, h - ty)
        for tx in range(0, w, 64):
            tw = min(64, w - tx)
            npx = tw * th
            sub = d[o]
            o += 1
            if sub == 0:
                tile = np.frombuffer(d, np.uint8, npx * 3, o).reshape(th, tw, 3)
                o += npx * 3
            elif sub == 1:
                tile = np.frombuffer(d, np.uint8, 3, o)
                o += 3
            elif sub <= 16:
                pal = np.frombuffer(d, np.uint8, sub * 3, o).reshape(sub, 3)
                o += sub * 3
                bpp = 1 if sub == 2 else 2 if sub <= 4 else 4
                rowbytes = (tw * bpp + 7) // 8
                bits = np.unpackbits(np.frombuffer(d, np.uint8, rowbytes * th, o).reshape(th, rowbytes), axis=1)
                o += rowbytes * th
                if bpp == 1:
                    idx = bits[:, :tw]
                elif bpp == 2:
                    idx = bits[:, 0:2 * tw:2] * 2 + bits[:, 1:2 * tw:2]
                else:
                    idx = (bits[:, 0:4 * tw:4] * 8 + bits[:, 1:4 * tw:4] * 4
                           + bits[:, 2:4 * tw:4] * 2 + bits[:, 3:4 * tw:4])
                tile = pal[idx]
            elif sub == 128:
                colors, lens, total = [], [], 0
                while total < npx:
                    colors.append(d[o:o + 3])
                    o += 3
                    n = 1
                    while True:
                        b = d[o]
                        o += 1
                        n += b
                        if b != 255:
                            break
                    lens.append(n)
                    total += n
                tile = np.repeat(np.frombuffer(b"".join(colors), np.uint8).reshape(-1, 3), lens, axis=0).reshape(th, tw, 3)
            elif sub >= 130:
                psize = sub - 128
                pal = np.frombuffer(d, np.uint8, psize * 3, o).reshape(psize, 3)
                o += psize * 3
                idxs, lens, total = [], [], 0
                while total < npx:
                    b = d[o]
                    o += 1
                    if b & 0x80:
                        n = 1
                        while True:
                            c = d[o]
                            o += 1
                            n += c
                            if c != 255:
                                break
                    else:
                        n = 1
                    idxs.append(b & 0x7F)
                    lens.append(n)
                    total += n
                tile = np.repeat(pal[idxs], lens, axis=0).reshape(th, tw, 3)
            else:
                raise RuntimeError(f"bad ZRLE subencoding {sub}")
            out[ty:ty + th, tx:tx + tw] = tile
    return out[:, :, ::-1]
