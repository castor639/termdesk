"""termdesk command line: connect, window, action, ls, sandbox, setup, uninstall."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

from . import __version__

SUBCOMMANDS = ("connect", "window", "action", "ls", "sandbox", "setup", "record", "uninstall")

USAGE = """termdesk <host[:port]>                connect this terminal pane to a VNC server
termdesk window <host[:port]|sandbox> open it in a new terminal window, so people can watch
termdesk action [--name N] <cmd...>   drive the open session (see `termdesk action --help`)
termdesk ls                           list running sessions
termdesk sandbox up|ls|down|...       disposable desktop containers
termdesk setup                        install the Claude Code skill
termdesk uninstall                    remove termdesk and everything it created
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    cmd = argv[0] if argv[0] in SUBCOMMANDS else "connect"
    rest = argv[1:] if cmd == argv[0] else argv
    try:
        return {"connect": cmd_connect, "window": cmd_window, "action": cmd_action, "ls": cmd_ls,
                "sandbox": cmd_sandbox, "setup": cmd_setup, "record": cmd_record,
                "uninstall": cmd_uninstall}[cmd](rest) or 0
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError) as e:
        sys.stderr.write(f"termdesk: {e}\n")
        return 1


# ---------------------------------------------------------------- connect
def cmd_connect(argv):
    ap = argparse.ArgumentParser(prog="termdesk", description="Show a VNC desktop in this terminal pane.")
    ap.add_argument("target", help="host[:port] of a VNC server, or a sandbox name")
    ap.add_argument("--name", help="session name for `termdesk action --name` (default: derived from target)")
    ap.add_argument("--fps", type=float, default=60)
    ap.add_argument("--no-shm", action="store_true", help="never use shared memory, even locally")
    ap.add_argument("--headless", action="store_true", help="no terminal output; only the control socket")
    ap.add_argument("--daemon", action="store_true", help="with --headless: fork into the background")
    ap.add_argument("--log", help="append debug info to this file")
    args = ap.parse_args(argv)

    from .rfb import RFB
    from .session import Session
    from .control import list_sessions, name_for_target

    target = _resolve_target(args.target)
    host, _, port = target.partition(":")
    name = args.name or name_for_target(args.target)
    if any(s["name"] == name for s in list_sessions()):
        raise RuntimeError(f"a session named {name} is already running; drive it with "
                           f"`termdesk action --name {name}` or start another with --name")
    if args.headless and args.daemon:
        _daemonize(args.log)
    rfb = RFB(host or "localhost", int(port or 5900))
    log = open(args.log, "a") if args.log else None
    if args.headless:
        Session(rfb, None, None, args.fps, log, name, target).run()
        return 0
    from .gfx import Graphics
    from .term import Term
    remote = bool(os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"))
    gfx = Graphics(use_shm=not args.no_shm and not remote)
    term = Term()
    try:
        Session(rfb, term, gfx, args.fps, log, name, target).run()
    finally:
        term.close()
    return 0


def _resolve_target(target):
    """A sandbox name becomes its localhost:port."""
    if ":" in target or "." in target or target == "localhost":
        return target
    try:
        from . import sandbox
        return sandbox.resolve(target)
    except Exception:
        return target


# ----------------------------------------------------------------- window
def cmd_window(argv):
    ap = argparse.ArgumentParser(prog="termdesk window",
                                 description="Open a session in a new terminal window so people can watch it.")
    ap.add_argument("target", help="host[:port] of a VNC server, or a sandbox name")
    ap.add_argument("--name", help="session name (default: derived from target)")
    args = ap.parse_args(argv)

    from .control import Client, list_sessions, name_for_target
    name = args.name or name_for_target(args.target)
    live = {s["name"]: s for s in list_sessions()}
    if name in live and not live[name].get("headless"):
        print(f"{name} is already open in a window")
        return 0
    if ":" in args.target or "." in args.target or args.target == "localhost":
        target = args.target
    else:
        from . import sandbox
        target = sandbox.resolve(args.target)
    if name in live:
        Client(name).call(cmd="quit")
        _wait_for(lambda: name not in {s["name"] for s in list_sessions()}, 5)

    term, launch = _new_window_argv([sys.executable, "-m", "termdesk", target, "--name", name], f"termdesk: {name}")
    null = subprocess.DEVNULL
    subprocess.Popen(launch, stdin=null, stdout=null, stderr=null, start_new_session=True)
    if not _wait_for(lambda: name in {s["name"] for s in list_sessions()}, 20):
        raise RuntimeError(f"{term} opened, but no session named {name} came up within 20s")
    print(f"opened {name} in a new {term} window; drive it with `termdesk action --name {name} ...`")
    return 0


def _wait_for(ok, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if ok():
            return True
        time.sleep(0.25)
    return False


def _new_window_argv(cmd, title):
    """(terminal, argv) that opens a new OS window running cmd, preferring the terminal we run in."""
    mac = sys.platform == "darwin"
    found = {}
    for term, app in (("kitty", "kitty"), ("ghostty", "Ghostty"), ("wezterm", "WezTerm")):
        exe = shutil.which(term)
        bundle = f"/Applications/{app}.app"
        if not exe and mac and os.path.isdir(bundle):
            exe = f"{bundle}/Contents/MacOS/{term}"
        if exe:
            found[term] = (exe, bundle)
    env_term = os.environ.get("TERM_PROGRAM", "").lower()
    order = ["kitty", "ghostty", "wezterm"]
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TERM") == "xterm-kitty":
        order.remove("kitty")
        order.insert(0, "kitty")
    elif env_term in order:
        order.remove(env_term)
        order.insert(0, env_term)
    for term in order:
        if term not in found:
            continue
        exe, bundle = found[term]
        if term == "kitty":
            return term, [exe, "--title", title, "-o", "remember_window_size=no",
                          "-o", "initial_window_width=1280", "-o", "initial_window_height=860"] + cmd
        if term == "ghostty":
            if mac:  # `-e` through `open --args` can run the command twice
                return term, ["open", "-na", "Ghostty.app", "--args", f"--title={title}",
                              "--initial-command=" + shlex.join(cmd)]
            return term, [exe, f"--title={title}", "-e"] + cmd
        return term, [exe, "start", "--always-new-process", "--"] + cmd
    raise RuntimeError("no kitty, Ghostty or WezTerm found to open a window; "
                       "run `termdesk <target>` in one yourself, or use --headless")


def _daemonize(log):
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    err = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644) if log else null
    os.dup2(err, 2)


# ----------------------------------------------------------------- action
ACTION_HELP = """commands:
  state [--full]                    the screen as numbered elements (changes only, --full for all)
  click N | click X Y [--right|--middle] [--double]
  set-value N TEXT...               click element N, select all, type TEXT
  scroll N DY | scroll X Y DY [DX]  positive DY scrolls down
  move N|X Y | mousedown N|X Y | mouseup N|X Y
  drag X1 Y1 X2 Y2
  type TEXT...                      type literal text
  key COMBO                         e.g. ctrl+l, Return, alt+F4, ctrl+shift+t
  paste TEXT...                     send text to the remote clipboard
  screenshot [PATH] [--scale S]     save a PNG (or print base64 with --json and no PATH)
  wait MS
  wait-idle [--idle MS] [--timeout MS]   block until the screen stops changing
  wait-for TEXT [--timeout MS]      poll state until an element containing TEXT appears; prints it
  record start [PATH] [--fps N] | record stop
  info | done | quit                done clears the AGENT ACTING badge, quit closes the session
N is an element number from your latest `state`. X Y are remote desktop pixels; `info` prints the size."""


def cmd_action(argv):
    ap = argparse.ArgumentParser(prog="termdesk action", epilog=ACTION_HELP,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="session to drive (needed when several run)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    words = [w for w in args.cmd if w != "--"] if args.cmd[:1] == ["--"] else args.cmd
    if not words:
        ap.print_help()
        return 1
    from .control import Client
    client = Client(args.name)
    if words[0] in ("wait-for", "wait_for"):
        return _wait_for_element(client, words[1:], args.json)
    req = parse_action(words)
    resp = client.call(**req)
    if args.json:
        print(json.dumps(resp))
    else:
        resp.pop("ok", None)
        if "png_base64" in resp:
            print(resp["png_base64"])
        elif "state" in resp:
            print(resp["state"])
        elif resp:
            print("  ".join(f"{k}={v}" for k, v in resp.items()))
    return 0


def _wait_for_element(client, words, as_json):
    """Poll `state` until an element line contains TEXT, then print the matching lines."""
    words = list(words)
    timeout = float(_flag(words, "--timeout", 30000)) / 1000
    text = " ".join(words).lower()
    if not text:
        raise ValueError("wait-for needs the text to look for")
    deadline = time.time() + timeout
    while True:
        state = client.call("state", full=True)["state"]
        hits = [l for l in state.splitlines() if l.startswith("[") and text in l.lower()]
        if hits or time.time() >= deadline:
            break
        time.sleep(0.5)
    if as_json:
        print(json.dumps({"found": bool(hits), "lines": hits}))
    else:
        print("\n".join(hits) if hits else f"timed_out=True  no element matching {text!r}")
    return 0 if hits else 1


def _flag(words, name, default=None, takes_value=True):
    if name in words:
        i = words.index(name)
        if takes_value:
            val = words[i + 1]
            del words[i:i + 2]
            return val
        del words[i]
        return True
    return default


def parse_action(words):
    words = list(words)
    cmd = words.pop(0).replace("-", "_")
    if cmd == "screenshot":
        scale = _flag(words, "--scale")
        req = {"cmd": "screenshot"}
        if words:
            req["path"] = words[0]
        if scale:
            req["scale"] = float(scale)
        return req
    if cmd == "state":
        return {"cmd": "state", "full": bool(_flag(words, "--full", False, False))}
    if cmd in ("click", "move", "mousedown", "mouseup"):
        button = "right" if _flag(words, "--right", False, False) else "middle" if _flag(words, "--middle", False, False) else "left"
        count = 2 if _flag(words, "--double", False, False) else 1
        req = {"cmd": cmd, "button": button, "count": count}
        if len(words) == 1:
            req["index"] = int(words[0])
        else:
            req["x"], req["y"] = float(words[0]), float(words[1])
        return req
    if cmd == "set_value":
        return {"cmd": "set_value", "index": int(words[0]), "text": " ".join(words[1:])}
    if cmd == "drag":
        return {"cmd": "drag", "x": float(words[0]), "y": float(words[1]), "to_x": float(words[2]), "to_y": float(words[3])}
    if cmd == "scroll":
        if len(words) == 2:
            return {"cmd": "scroll", "index": int(words[0]), "dy": int(words[1]), "dx": 0}
        return {"cmd": "scroll", "x": float(words[0]), "y": float(words[1]), "dy": int(words[2]),
                "dx": int(words[3]) if len(words) > 3 else 0}
    if cmd in ("type", "paste"):
        return {"cmd": cmd, "text": " ".join(words)}
    if cmd == "key":
        return {"cmd": "key", "combo": words[0]}
    if cmd == "wait":
        return {"cmd": "wait", "ms": float(words[0]) if words else 500}
    if cmd == "wait_idle":
        return {"cmd": "wait_idle", "idle_ms": float(_flag(words, "--idle", 300)), "timeout_ms": float(_flag(words, "--timeout", 5000))}
    if cmd == "record":
        action = words.pop(0) if words else "start"
        req = {"cmd": "record", "action": action}
        fps = _flag(words, "--fps")
        if fps:
            req["fps"] = float(fps)
        if words:
            req["path"] = words[0]
        return req
    if cmd in ("info", "done", "quit"):
        return {"cmd": cmd}
    raise ValueError(f"unknown action {cmd!r}; try `termdesk action --help`")


def cmd_record(argv):
    return cmd_action(["record"] + argv)


# --------------------------------------------------------------------- ls
def cmd_ls(argv):
    from .control import list_sessions
    as_json = "--json" in argv
    live = list_sessions()
    if as_json:
        print(json.dumps(live))
        return 0
    if not live:
        print("no sessions")
    for s in live:
        flags = " headless" if s.get("headless") else ""
        flags += " recording" if s.get("recording") else ""
        print(f"{s['name']:24} {s.get('addr') or ''}  {s.get('target', '')}  {s.get('width')}x{s.get('height')}{flags}")
    return 0


# ---------------------------------------------------------------- sandbox
def cmd_sandbox(argv):
    from . import sandbox
    return sandbox.main(argv)


# -------------------------------------------------------------- uninstall
def cmd_uninstall(argv):
    from . import uninstall
    return uninstall.main(argv)


# ------------------------------------------------------------------ setup
def cmd_setup(argv):
    """Install the Claude Code skill so agents know how to drive termdesk."""
    src = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")
    dest_dir = os.path.expanduser("~/.claude/skills/termdesk")
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copy(src, os.path.join(dest_dir, "SKILL.md"))
    print(f"installed {dest_dir}/SKILL.md")
    if not shutil.which("docker"):
        print("docker not found: `termdesk sandbox` will not work until it is installed")
    return 0
