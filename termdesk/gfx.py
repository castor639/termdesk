"""kitty graphics protocol output: one base image, in-place patches, shm transport."""
import base64
import fcntl
import struct
import sys
import termios
import zlib

ESC = "\x1b"


def term_geometry():
    rows, cols, xpix, ypix = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
    if not xpix or not ypix:
        raise RuntimeError("terminal does not report its pixel size; termdesk needs kitty, ghostty or wezterm")
    return rows, cols, xpix, ypix


class Graphics:
    IMAGE_ID = 31

    def __init__(self, use_shm):
        self.shm_ok = use_shm
        self.shm_mod = None
        if use_shm:
            try:
                from multiprocessing import shared_memory
                self.shm_mod = shared_memory
            except ImportError:
                self.shm_ok = False
        self.pending_shm = []
        self.bytes_out = 0

    def _emit(self, ctl, payload):
        data = base64.b64encode(payload)
        out = []
        first = True
        while True:
            chunk, data = data[:4096], data[4096:]
            head = ctl + "," if first else ""
            out.append(f"{ESC}_G{head}m={1 if data else 0};".encode() + chunk + f"{ESC}\\".encode())
            first = False
            if not data:
                break
        blob = b"".join(out)
        self.bytes_out += len(blob)
        sys.stdout.flush()  # text-layer escapes must reach the terminal before the image bytes
        sys.stdout.buffer.write(blob)

    def _transport(self, ctl, pixels):
        if self.shm_ok:
            try:
                seg = self.shm_mod.SharedMemory(create=True, size=len(pixels))
                seg.buf[:len(pixels)] = pixels
                self.pending_shm.append(seg)
                self._emit(ctl + ",t=s", ("/" + seg.name).encode())
                return
            except Exception:
                self.shm_ok = False
        self._emit(ctl + ",o=z", zlib.compress(pixels, 1))

    def full(self, img, cols, rows):
        w, h = img.size
        sys.stdout.flush()
        sys.stdout.buffer.write(b"\x1b[H")  # anchor the placement at the top-left cell
        self._transport(f"a=T,f=24,s={w},v={h},i={self.IMAGE_ID},q=1,c={cols},r={rows},C=1", img.tobytes())

    def patch(self, img, x, y):
        w, h = img.size
        self._transport(f"a=f,i={self.IMAGE_ID},r=1,x={x},y={y},f=24,s={w},v={h},X=1,q=1", img.tobytes())

    def reap_shm(self):
        # kitty unlinks the segment once it has read it; we only drop our handle
        for seg in self.pending_shm:
            try:
                seg.close()
            except Exception:
                pass
        self.pending_shm = []

    def on_error(self, text):
        if "EBADF" in text or "file" in text.lower():
            self.shm_ok = False
            return "shm disabled"
        return text
