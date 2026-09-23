"""Session recording: throttled frame captures assembled into a GIF, and an MP4 when ffmpeg exists."""
import os
import shutil
import subprocess
import tempfile
import time

from PIL import Image


class Recorder:
    def __init__(self, path, fps=10, max_width=1024):
        self.path = path
        self.interval = 1 / fps
        self.max_width = max_width
        self.dir = tempfile.mkdtemp(prefix="termdesk-rec-")
        self.frames = []  # (timestamp, filename)
        self.last = 0.0
        self.started = time.time()

    def tick(self, fb, force=False):
        now = time.time()
        if not force and now - self.last < self.interval:
            return
        self.last = now
        fn = os.path.join(self.dir, f"{len(self.frames):06d}.png")
        img = fb
        if fb.width > self.max_width:
            img = fb.resize((self.max_width, fb.height * self.max_width // fb.width))
        img.save(fn, "PNG", compress_level=1)
        self.frames.append((now, fn))

    def stop(self, fb):
        self.tick(fb, force=True)
        result = {"frames": len(self.frames), "seconds": round(time.time() - self.started, 1)}
        if not self.frames:
            shutil.rmtree(self.dir, ignore_errors=True)
            return result
        base, ext = os.path.splitext(self.path)
        ext = ext.lower() or ".gif"
        if ext == ".mp4" and shutil.which("ffmpeg"):
            result["mp4"] = self._mp4(base + ".mp4")
        else:
            result["gif"] = self._gif(base + ".gif")
            if ext == ".mp4":
                result["note"] = "ffmpeg not found, wrote a gif instead"
        shutil.rmtree(self.dir, ignore_errors=True)
        return result

    def _gif(self, out):
        imgs = [Image.open(fn).convert("P", palette=Image.ADAPTIVE, colors=128) for _, fn in self.frames]
        durations = []
        for i, (t, _) in enumerate(self.frames):
            nxt = self.frames[i + 1][0] if i + 1 < len(self.frames) else t + 1.0
            durations.append(max(20, int((nxt - t) * 1000)))
        imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=durations, loop=0, optimize=False)
        return out

    def _mp4(self, out):
        concat = os.path.join(self.dir, "list.txt")
        with open(concat, "w") as f:
            for i, (t, fn) in enumerate(self.frames):
                nxt = self.frames[i + 1][0] if i + 1 < len(self.frames) else t + 1.0
                f.write(f"file '{fn}'\nduration {max(0.02, nxt - t):.3f}\n")
            f.write(f"file '{self.frames[-1][1]}'\n")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat,
                        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p", "-r", "30", out], check=True)
        return out
