#!/usr/bin/env python3
"""Throwaway desktop containers for termdesk.

Each sandbox is one XFCE + VNC container bound to a free port on 127.0.0.1,
so `termdesk localhost:<port>` gets a private desktop instead of the shared one.
"""
import argparse
import json
import os
import posixpath
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

DEFAULT_IMAGE = "ghcr.io/castor639/termdesk-desktop:latest"
IMAGE_VERSION = 2  # the desktop/ this code expects; the Dockerfile's termdesk.image label
SNAP_REPO = "termdesk-snap"
CONTAINER_PREFIX = "termdesk-"
SANDBOX_LABEL = "termdesk.sandbox=1"
READY_TIMEOUT = 30.0
HOME = "/home/guest"
SHARE_DIR = HOME + "/shared"


class SandboxError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# docker plumbing
# --------------------------------------------------------------------------
def _docker(args, timeout=None, check=True):
    try:
        p = subprocess.run(
            ["docker"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SandboxError("docker %s timed out after %ss" % (args[0], timeout))
    if check and p.returncode != 0:
        raise SandboxError(_clean(p.stderr) or "docker %s failed" % " ".join(args))
    return p


def _clean(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


def require_docker():
    if not shutil.which("docker"):
        raise SandboxError("docker not found on PATH, install Docker Desktop or colima")
    p = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=20, check=False)
    if p.returncode != 0:
        raise SandboxError(
            "docker daemon is not answering, start it with `colima start` or open Docker Desktop"
        )


def _container(name):
    return CONTAINER_PREFIX + name


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _vnc_ready(port, timeout=0.5):
    """True once the VNC server greets. Docker's port proxy accepts connections before the server
    inside listens, then drops them, so an open port alone is not enough."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return s.recv(4) == b"RFB "
    except OSError:
        return False
    finally:
        s.close()


def _running(container):
    p = _docker(["inspect", "-f", "{{.State.Running}}", container], check=False)
    return p.returncode == 0 and p.stdout.strip() == "true"


def _image_info(image):
    """(id, termdesk.image label as int) of a local image, or (None, 0)."""
    p = _docker(["image", "inspect", image], check=False)
    if p.returncode != 0:
        return None, 0
    info = json.loads(p.stdout)[0]
    version = ((info.get("Config") or {}).get("Labels") or {}).get("termdesk.image", "")
    return info.get("Id"), int(version) if version.isdigit() else 0


def _pull(image, pull=False):
    """Pull when asked, when missing, or when the cached default image is older than this termdesk."""
    old, version = _image_info(image)
    stale = old is not None and image == DEFAULT_IMAGE and version < IMAGE_VERSION
    if not (pull or stale or old is None):
        return
    sys.stderr.write("%s %s\n" % ("updating the desktop image for this termdesk:" if stale else "pulling", image))
    sys.stderr.flush()
    if subprocess.run(["docker", "pull", image], stdout=sys.stderr).returncode != 0:
        if not stale:
            raise SandboxError("could not pull %s" % image)
        sys.stderr.write("could not update it; using the old image, which lacks newer features\n")
        return
    if old and old != _image_info(image)[0]:
        _docker(["rmi", old], check=False)  # the replaced copy, unless a sandbox still runs on it


def _parse_port(ports):
    # "127.0.0.1:52341->5900/tcp, :::0->5900/tcp"
    for chunk in (ports or "").split(","):
        chunk = chunk.strip()
        if "->5900" not in chunk or "->" not in chunk:
            continue
        hostside = chunk.split("->", 1)[0]
        if ":" in hostside:
            try:
                return int(hostside.rsplit(":", 1)[1])
            except ValueError:
                continue
    return None


def _parse_labels(labels):
    out = {}
    for item in (labels or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------
def up(name=None, image=DEFAULT_IMAGE, geometry="1280x800", memory="2g", cpus="2", pull=False, share=None):
    require_docker()
    if name is None:
        name = "sb-" + secrets.token_hex(2)
    container = _container(name)
    if _docker(["inspect", container], check=False).returncode == 0:
        raise SandboxError("sandbox %s already exists, remove it with `down %s`" % (name, name))

    _pull(image, pull)
    mount = _share_args(share, image) if share else []
    port = _free_port()
    args = [
        "run", "-d",
        "--name", container,
        "--label", SANDBOX_LABEL,
        "--label", "termdesk.name=" + name,
        "-p", "127.0.0.1:%d:5900" % port,
        "--shm-size", "512m",
        "--memory", memory,
        "--cpus", str(cpus),
        "-e", "GEOMETRY=" + geometry,
    ] + mount + [image]
    p = _docker(args, check=False)
    if p.returncode != 0:
        raise SandboxError(_clean(p.stderr) or "docker run failed")
    cid = p.stdout.strip()[:12]

    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if _vnc_ready(port):
            break
        if not _running(container):
            logs = _docker(["logs", "--tail", "20", container], check=False)
            _docker(["rm", "-f", container], check=False)
            raise SandboxError("container exited before VNC came up: " + _clean(logs.stderr or logs.stdout))
        time.sleep(0.25)
    else:
        _docker(["rm", "-f", container], check=False)
        raise SandboxError("VNC on port %d did not come up within %gs" % (port, READY_TIMEOUT))

    return {
        "name": name,
        "container": cid,
        "port": port,
        "target": "localhost:%d" % port,
        "image": image,
    }


def ls():
    require_docker()
    p = _docker(["ps", "-a", "--filter", "label=" + SANDBOX_LABEL, "--format", "{{json .}}"])
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        labels = _parse_labels(row.get("Labels"))
        cname = row.get("Names", "")
        name = labels.get("termdesk.name") or (
            cname[len(CONTAINER_PREFIX):] if cname.startswith(CONTAINER_PREFIX) else cname
        )
        out.append({
            "name": name,
            "container": row.get("ID", "")[:12],
            "status": row.get("Status", ""),
            "port": _parse_port(row.get("Ports")),
            "image": row.get("Image", ""),
        })
    out.sort(key=lambda r: r["name"])
    return out


def _only(entries, action):
    if not entries:
        raise SandboxError("no sandboxes, start one with `up`")
    if len(entries) > 1:
        names = ", ".join(e["name"] for e in entries)
        raise SandboxError("several sandboxes (%s), name the one to %s" % (names, action))
    return entries[0]


def down(name=None, all=False):
    require_docker()
    entries = ls()
    if all:
        targets = [e["name"] for e in entries]
    elif name is None:
        targets = [_only(entries, "remove")["name"]]
    else:
        if name not in [e["name"] for e in entries]:
            raise SandboxError("no sandbox named %s" % name)
        targets = [name]
    removed = []
    for t in targets:
        p = _docker(["rm", "-f", _container(t)], check=False)
        if p.returncode != 0:
            raise SandboxError(_clean(p.stderr) or "could not remove %s" % t)
        removed.append(t)
    return removed


def snapshot(name, tag):
    require_docker()
    container = _container(name)
    if _docker(["inspect", container], check=False).returncode != 0:
        raise SandboxError("no sandbox named %s" % name)
    ref = "%s:%s" % (SNAP_REPO, tag)
    p = _docker(["commit", container, ref], check=False)
    if p.returncode != 0:
        raise SandboxError(_clean(p.stderr) or "could not commit %s" % name)
    return ref


def restore(tag, name=None, **up_kwargs):
    up_kwargs.pop("image", None)
    return up(name=name, image="%s:%s" % (SNAP_REPO, tag), **up_kwargs)


def _exec_argv(name, cmd, user="guest"):
    if not cmd:
        raise SandboxError("nothing to run, pass a command")
    container = _container(name)
    if not _running(container):
        raise SandboxError("sandbox %s is not running" % name)
    env = ("export DBUS_SESSION_BUS_ADDRESS=$(cat /tmp/dbus-address 2>/dev/null) "
           "GTK_MODULES=gail:atk-bridge GNOME_ACCESSIBILITY=1 NO_AT_BRIDGE=0 QT_ACCESSIBILITY=1; exec \"$@\"")
    return ["docker", "exec", "-u", user, "-w", HOME, "-e", "DISPLAY=:1", container, "sh", "-c", env, "sh"] + list(cmd)


def exec(name, cmd, user="guest"):
    require_docker()
    return subprocess.run(
        _exec_argv(name, cmd, user),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def exec_detached(name, cmd, user="guest"):
    require_docker()
    devnull = open(os.devnull, "wb")
    argv = _exec_argv(name, cmd, user)
    argv.insert(2, "-d")
    return subprocess.Popen(argv, stdout=devnull, stderr=devnull)


def _split(spec):
    """(sandbox name, absolute path inside it) for NAME:PATH, or (None, host path)."""
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):(.*)$", spec)
    if not m or os.path.exists(spec):
        return None, os.path.abspath(os.path.expanduser(spec))
    if _docker(["inspect", _container(m.group(1))], check=False).returncode != 0:
        raise SandboxError("no sandbox named %s" % m.group(1))
    path = m.group(2) or HOME
    return m.group(1), path if path.startswith("/") else posixpath.join(HOME, path)


def cp(src, dst):
    """Copy between the host and a sandbox; NAME:PATH is the sandbox side, relative to /home/guest.
    What goes in belongs to guest. Returns the path of the copy."""
    require_docker()
    sname, spath = _split(src)
    dname, dpath = _split(dst)
    if bool(sname) == bool(dname):
        raise SandboxError("copy between the host and a sandbox: one side NAME:PATH, the other a host path")
    base = posixpath.basename(spath.rstrip("/")) if sname else os.path.basename(os.path.normpath(spath))
    if sname:
        final = os.path.join(dpath, base) if os.path.isdir(dpath) else dpath
        _docker(["cp", "%s:%s" % (_container(sname), spath), dpath])
        return final
    if not os.path.exists(spath):
        raise SandboxError("no such file on the host: %s" % spath)
    container = _container(dname)
    into = dpath.endswith("/") or _docker(["exec", container, "test", "-d", dpath], check=False).returncode == 0
    final = posixpath.join(dpath, base) if into else dpath
    _docker(["cp", spath, "%s:%s" % (container, dpath)])
    _docker(["exec", "-u", "root", container, "chown", "-R", "guest:guest", "--", final])
    return final


def _share_args(spec, image):
    """docker run arguments that mount a host folder at /home/guest/shared, read-only unless DIR:rw."""
    path, mode = spec, "ro"
    if spec.endswith((":rw", ":ro")):
        path, mode = spec[:-3], spec[-2:]
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise SandboxError("--share needs an existing folder, not %s" % path)
    # Docker in a VM (colima, Docker Desktop) sees only the host folders the VM shares, and
    # mounts any other folder as an empty one without an error
    probe = ["run", "--rm", "--entrypoint", "ls", "-v", "%s:/x:ro" % path, image, "-A", "/x"]
    try:
        fd, marker = tempfile.mkstemp(prefix=".termdesk-", dir=path)
        os.close(fd)
    except OSError:
        marker = None
    try:
        seen = _docker(probe, check=False, timeout=120).stdout.split()
    finally:
        if marker:
            os.unlink(marker)
    expect = [os.path.basename(marker)] if marker else os.listdir(path)[:1]
    if expect and not set(expect) & set(seen):
        raise SandboxError("docker cannot see %s: its VM does not share that folder, so the sandbox would get an "
                           "empty one. Share a folder inside your home directory, which colima and Docker Desktop "
                           "share by default, or add this one to the VM's mounts" % path)
    return ["-v", "%s:%s:%s" % (path, SHARE_DIR, mode)]


def resolve(name=None):
    require_docker()
    entries = [e for e in ls() if e["port"]]
    if name is None:
        entry = _only(entries, "resolve")
    else:
        match = [e for e in entries if e["name"] == name]
        if not match:
            raise SandboxError("no running sandbox named %s" % name)
        entry = match[0]
    return "localhost:%d" % entry["port"]


def resolve_name(name=None):
    require_docker()
    entries = [e for e in ls() if e["port"]]
    if name is None:
        return _only(entries, "use")["name"]
    if not any(e["name"] == name for e in entries):
        raise SandboxError("no running sandbox named %s" % name)
    return name


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------
def _add_up_args(p):
    p.add_argument("--name")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--geometry", default="1280x800")
    p.add_argument("--memory", default="2g")
    p.add_argument("--cpus", default="2")
    p.add_argument("--pull", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--share", metavar="DIR[:rw]", help="mount a host folder at %s, read-only unless :rw" % SHARE_DIR)
    p.add_argument("--session", action="store_true",
                   help="also start a background session named after the sandbox, ready for `termdesk action`")


def build_parser():
    p = argparse.ArgumentParser(prog="termdesk sandbox", description="disposable desktop containers")
    sub = p.add_subparsers(dest="command")

    up_p = sub.add_parser("up", help="start a sandbox")
    _add_up_args(up_p)

    ls_p = sub.add_parser("ls", help="list sandboxes")
    ls_p.add_argument("--json", action="store_true")

    down_p = sub.add_parser("down", help="remove sandboxes")
    down_p.add_argument("name", nargs="?")
    down_p.add_argument("--all", action="store_true")

    snap_p = sub.add_parser("snapshot", help="commit a sandbox to an image")
    snap_p.add_argument("name")
    snap_p.add_argument("tag")

    rest_p = sub.add_parser("restore", help="start a sandbox from a snapshot")
    rest_p.add_argument("tag")
    _add_up_args(rest_p)

    ex_p = sub.add_parser("exec", help="run a command inside a sandbox")
    ex_p.add_argument("name")
    ex_p.add_argument("-d", "--detach", action="store_true")
    ex_p.add_argument("--root", action="store_true", help="run as root instead of guest")
    ex_p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- cmd")

    res_p = sub.add_parser("resolve", help="print the host:port of a sandbox")
    res_p.add_argument("name", nargs="?")

    cp_p = sub.add_parser("cp", help="copy files between the host and a sandbox",
                          description="NAME:PATH is the sandbox side; relative paths start at %s. "
                                      "Files copied in belong to guest." % HOME)
    cp_p.add_argument("src")
    cp_p.add_argument("dst")
    return p


def _attach(info):
    from .control import start_headless
    info["session"] = start_headless(info["target"], info["name"])["name"]


def _print_up(info, as_json):
    if as_json:
        print(json.dumps(info))
        return
    looks_like_host = "." in info["name"] or ":" in info["name"] or info["name"] == "localhost"
    target = info["target"] if looks_like_host else info["name"]
    print("sandbox %s  %s  %s" % (info["name"], info["container"], info["image"]))
    print("target localhost:%d" % info["port"])
    if info.get("share"):
        print("shared %s at %s" % (info["share"], SHARE_DIR))
    if info.get("session"):
        s = info["session"]
        print("session %s is running in the background" % s)
        print("  watch it:  termdesk window %s" % target)
        print("  drive it:  termdesk action --name %s state" % s)
    else:
        print("watch it in a new window: termdesk window %s" % target)
        print("or in this pane: termdesk %s" % target)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not args.command:
        parser.print_help()
        return 1
    try:
        if args.command == "up":
            info = up(name=args.name, image=args.image, geometry=args.geometry,
                      memory=args.memory, cpus=args.cpus, pull=args.pull, share=args.share)
            info["share"] = args.share
            if args.session:
                _attach(info)
            _print_up(info, args.json)
        elif args.command == "ls":
            rows = ls()
            if args.json:
                print(json.dumps(rows))
            else:
                for r in rows:
                    print("%-12s %-12s %-6s %s  %s" % (
                        r["name"], r["container"],
                        r["port"] or "-", r["status"], r["image"]))
        elif args.command == "down":
            for n in down(name=args.name, all=args.all):
                print("removed %s" % n)
        elif args.command == "snapshot":
            print(snapshot(args.name, args.tag))
        elif args.command == "restore":
            info = restore(args.tag, name=args.name, geometry=args.geometry,
                           memory=args.memory, cpus=args.cpus, share=args.share)
            info["share"] = args.share
            if args.session:
                _attach(info)
            _print_up(info, args.json)
        elif args.command == "exec":
            cmd = list(args.cmd)
            detach = args.detach
            user = "root" if args.root else "guest"
            while cmd and cmd[0] in ("--", "-d", "--detach", "--root"):
                detach = detach or cmd[0] in ("-d", "--detach")
                user = "root" if cmd[0] == "--root" else user
                cmd = cmd[1:]
            if detach:
                exec_detached(args.name, cmd, user)
                print("started %s in %s" % (" ".join(cmd), args.name))
            else:
                r = exec(args.name, cmd, user)
                if r.stdout:
                    sys.stdout.write(r.stdout)
                if r.stderr:
                    sys.stderr.write(r.stderr)
                return r.returncode
        elif args.command == "resolve":
            print(resolve(args.name))
        elif args.command == "cp":
            print(cp(args.src, args.dst))
    except (SandboxError, RuntimeError) as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    except KeyboardInterrupt:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
