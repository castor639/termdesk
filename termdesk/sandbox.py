#!/usr/bin/env python3
"""Throwaway desktop containers for termdesk.

Each sandbox is one XFCE + VNC container bound to a free port on 127.0.0.1,
so `termdesk localhost:<port>` gets a private desktop instead of the shared one.
"""
import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time

DEFAULT_IMAGE = "ghcr.io/castor639/termdesk-desktop:latest"
SNAP_REPO = "termdesk-snap"
CONTAINER_PREFIX = "termdesk-"
SANDBOX_LABEL = "termdesk.sandbox=1"
READY_TIMEOUT = 30.0


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


def _port_open(port, timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _running(container):
    p = _docker(["inspect", "-f", "{{.State.Running}}", container], check=False)
    return p.returncode == 0 and p.stdout.strip() == "true"


def _have_image(image):
    return _docker(["image", "inspect", image], check=False).returncode == 0


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
def up(name=None, image=DEFAULT_IMAGE, geometry="1280x800", memory="2g", cpus="2", pull=False):
    require_docker()
    if name is None:
        name = "sb-" + secrets.token_hex(2)
    container = _container(name)
    if _docker(["inspect", container], check=False).returncode == 0:
        raise SandboxError("sandbox %s already exists, remove it with `down %s`" % (name, name))

    if pull:
        p = _docker(["pull", image], check=False)
        if p.returncode != 0:
            raise SandboxError(_clean(p.stderr) or "could not pull %s" % image)
    elif not _have_image(image):
        sys.stderr.write("image %s not present locally, docker will pull it\n" % image)

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
        image,
    ]
    p = _docker(args, check=False)
    if p.returncode != 0:
        raise SandboxError(_clean(p.stderr) or "docker run failed")
    cid = p.stdout.strip()[:12]

    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if _port_open(port):
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


def _exec_argv(name, cmd):
    if not cmd:
        raise SandboxError("nothing to run, pass a command")
    container = _container(name)
    if not _running(container):
        raise SandboxError("sandbox %s is not running" % name)
    env = ("export DBUS_SESSION_BUS_ADDRESS=$(cat /tmp/dbus-address 2>/dev/null) "
           "GTK_MODULES=gail:atk-bridge GNOME_ACCESSIBILITY=1 NO_AT_BRIDGE=0 QT_ACCESSIBILITY=1; exec \"$@\"")
    return ["docker", "exec", "-u", "guest", "-e", "DISPLAY=:1", container, "sh", "-c", env, "sh"] + list(cmd)


def exec(name, cmd):
    require_docker()
    return subprocess.run(
        _exec_argv(name, cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def exec_detached(name, cmd):
    require_docker()
    devnull = open(os.devnull, "wb")
    argv = _exec_argv(name, cmd)
    argv.insert(2, "-d")
    return subprocess.Popen(argv, stdout=devnull, stderr=devnull)


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
    ex_p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- cmd")

    res_p = sub.add_parser("resolve", help="print the host:port of a sandbox")
    res_p.add_argument("name", nargs="?")
    return p


def _print_up(info, as_json):
    if as_json:
        print(json.dumps(info))
    else:
        print("sandbox %s  %s  %s" % (info["name"], info["container"], info["image"]))
        print("target localhost:%d" % info["port"])
        print("connect with: termdesk localhost:%d" % info["port"])


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not args.command:
        parser.print_help()
        return 1
    try:
        if args.command == "up":
            info = up(name=args.name, image=args.image, geometry=args.geometry,
                      memory=args.memory, cpus=args.cpus, pull=args.pull)
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
                           memory=args.memory, cpus=args.cpus)
            _print_up(info, args.json)
        elif args.command == "exec":
            cmd = list(args.cmd)
            detach = args.detach
            while cmd and cmd[0] in ("--", "-d", "--detach"):
                detach = detach or cmd[0] != "--"
                cmd = cmd[1:]
            if detach:
                exec_detached(args.name, cmd)
                print("started %s in %s" % (" ".join(cmd), args.name))
            else:
                r = exec(args.name, cmd)
                if r.stdout:
                    sys.stdout.write(r.stdout)
                if r.stderr:
                    sys.stderr.write(r.stderr)
                return r.returncode
        elif args.command == "resolve":
            print(resolve(args.name))
    except SandboxError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    except KeyboardInterrupt:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
