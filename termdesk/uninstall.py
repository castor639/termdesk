"""termdesk uninstall: remove termdesk and everything it created on this machine."""
import argparse
import os
import shutil
import subprocess
import sys

from . import sandbox
from .control import Client, list_sessions

STATE_DIR = os.path.expanduser("~/.local/state/termdesk")
DATA_DIR = os.path.expanduser("~/.local/share/termdesk")
SKILL_DIR = os.path.expanduser("~/.claude/skills/termdesk")
UV_MARKER = os.path.join(DATA_DIR, "installed-uv")


def _docker_images():
    """(id, label) for the desktop image and snapshots, any tag."""
    repos = {sandbox.DEFAULT_IMAGE.rsplit(":", 1)[0], sandbox.SNAP_REPO}
    p = sandbox._docker(["images", "--format", "{{.Repository}} {{.Tag}} {{.ID}}"])
    out, seen = [], set()
    for line in p.stdout.splitlines():
        repo, tag, iid = line.split()
        if repo in repos and iid not in seen:
            seen.add(iid)
            out.append((iid, repo if tag == "<none>" else f"{repo}:{tag}"))
    return out


def _how_installed():
    """'uv' for a uv tool, 'pip' for site-packages, None for a source checkout."""
    here = os.path.abspath(__file__)
    if os.sep + os.path.join("uv", "tools", "termdesk") + os.sep in here:
        return "uv"
    if os.sep + "site-packages" + os.sep in here:
        return "pip"
    return None


def _tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def _uv():
    return shutil.which("uv") or next((p for p in (os.path.expanduser("~/.local/bin/uv"),
                                                    os.path.expanduser("~/.cargo/bin/uv")) if os.path.exists(p)), None)


def _other_uv_tools(uv):
    p = subprocess.run([uv, "tool", "list"], capture_output=True, text=True)
    rows = [l.split() for l in p.stdout.splitlines()]
    return [r[0] for r in rows if len(r) >= 2 and r[1].startswith("v") and r[0] != "termdesk"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="termdesk uninstall",
                                 description="Remove termdesk and everything it created: sessions, sandboxes, "
                                             "desktop images, the Claude Code skill and termdesk itself.")
    ap.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--keep-uv", action="store_true", help="keep uv even if the termdesk installer added it")
    ap.add_argument("--skip-docker", action="store_true", help="leave sandboxes and images alone")
    args = ap.parse_args(argv)

    sessions = [s["name"] for s in list_sessions()]
    boxes, images = [], []
    if not args.skip_docker and shutil.which("docker"):
        try:
            sandbox.require_docker()
        except sandbox.SandboxError:
            raise RuntimeError("docker is installed but not running. Start it (`colima start` or Docker Desktop) "
                               "so its termdesk sandboxes and images can be removed, or pass --skip-docker")
        boxes = [e["name"] for e in sandbox.ls()]
        images = _docker_images()
    how = _how_installed()
    uv = _uv()
    drop_uv = os.path.exists(UV_MARKER) and not args.keep_uv and uv is not None
    keep_uv_for = _other_uv_tools(uv) if drop_uv else []
    if keep_uv_for:
        drop_uv = False

    plan = [f"session {n}" for n in sessions] + [f"sandbox {n}" for n in boxes] + [f"image {l}" for _, l in images]
    plan += [_tilde(p) for p in (SKILL_DIR, STATE_DIR, DATA_DIR) if os.path.lexists(p)]
    plan += {"uv": ["termdesk (uv tool)"], "pip": ["termdesk (pip package)"]}.get(how, [])
    if drop_uv:
        plan.append("uv, its cache and the Pythons it downloaded (added by the termdesk installer; --keep-uv keeps it)")
    print("termdesk uninstall will remove:")
    for item in plan:
        print("  " + item)
    if how is None:
        print("  (running from a source checkout, which stays)")
    sys.stdout.flush()
    if not args.yes:
        if not sys.stdin.isatty():
            raise RuntimeError("not a terminal; pass --yes to confirm")
        if input("Remove all of this? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing removed")
            return 1

    for n in sessions:
        try:
            Client(n).call(cmd="quit")
            print(f"closed session {n}")
        except Exception as e:
            print(f"could not close session {n}: {e}")
    if boxes:
        for n in sandbox.down(all=True):
            print(f"removed sandbox {n}")
    for iid, label in images:
        p = sandbox._docker(["rmi", "-f", iid], check=False)
        print(f"removed image {label}" if p.returncode == 0 else f"could not remove image {label}: {sandbox._clean(p.stderr)}")
    for path in (SKILL_DIR, STATE_DIR, DATA_DIR):
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        else:
            continue
        print(f"removed {_tilde(path)}")
    if keep_uv_for:
        print(f"kept uv, which also manages {', '.join(keep_uv_for)}")

    sys.stdout.flush()
    if how == "uv" and uv:
        subprocess.run([uv, "tool", "uninstall", "termdesk"], check=False)
    elif how == "uv":
        print("uv is not on PATH, so the termdesk tool itself stays; remove it with `uv tool uninstall termdesk`")
        return 1
    elif how == "pip":
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "termdesk"], check=False)
    if drop_uv:
        subprocess.run([uv, "cache", "clean"], check=False)
        for key in ("python", "tool"):
            p = subprocess.run([uv, key, "dir"], capture_output=True, text=True)
            if p.returncode == 0 and os.path.isdir(p.stdout.strip()):
                shutil.rmtree(p.stdout.strip())
        bindir = os.path.dirname(uv)
        for exe in ("uv", "uvx"):
            if os.path.exists(os.path.join(bindir, exe)):
                os.remove(os.path.join(bindir, exe))
        print("removed uv")
    print("termdesk is uninstalled")
    return 0
