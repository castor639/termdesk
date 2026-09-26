"""termdesk command line: connect, window, action, ls, sandbox, mcp, guide, setup, uninstall."""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import __version__

SUBCOMMANDS = ("connect", "window", "action", "ls", "sandbox", "setup", "record", "uninstall", "mcp", "guide")

USAGE = """termdesk <host[:port]>                connect this terminal pane to a VNC server
termdesk window <host[:port]|sandbox> open it in a new terminal window, so people can watch
termdesk action [--name N] <cmd...>   drive the open session (see `termdesk action --help`)
termdesk ls                           list running sessions
termdesk sandbox up|ls|down|cp|...    disposable desktop containers
termdesk mcp                          MCP server on stdio, for any MCP client
termdesk guide                        print the agent guide, for agents without the skill
termdesk setup                        install the Claude Code skill
termdesk uninstall                    remove termdesk and everything it created
"""
MCP_CONFIG = '{"mcpServers": {"termdesk": {"command": "termdesk", "args": ["mcp"]}}}'


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
                "uninstall": cmd_uninstall, "mcp": cmd_mcp, "guide": cmd_guide}[cmd](rest) or 0
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
    print(open_window(args.target, args.name))
    return 0


def open_window(target, name=None):
    """Run a session on target in a new terminal window, replacing a background one of the same name."""
    from .control import Client, list_sessions, name_for_target
    name = name or name_for_target(target)
    live = {s["name"]: s for s in list_sessions()}
    if name in live and live[name].get("busy"):
        raise RuntimeError(f"session {name} is busy; try again in a few seconds")
    if name in live and not live[name].get("headless"):
        return f"{name} is already open in a window"
    if not (":" in target or "." in target or target == "localhost"):
        from . import sandbox
        target = sandbox.resolve(target)
    if name in live:
        Client(name).call(cmd="quit")
        _wait_for(lambda: name not in {s["name"] for s in list_sessions()}, 5)

    term, launch = _new_window_argv([sys.executable, "-m", "termdesk", target, "--name", name], f"termdesk: {name}")
    null = subprocess.DEVNULL
    subprocess.Popen(launch, stdin=null, stdout=null, stderr=null, start_new_session=True)
    if not _wait_for(lambda: name in {s["name"] for s in list_sessions()}, 20):
        raise RuntimeError(f"{term} opened, but no session named {name} came up within 20s")
    return f"opened {name} in a new {term} window; drive it with `termdesk action --name {name} ...`"


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
  state [--full] [--window TEXT] [--focused] [--cells N]
                                    the screen as numbered elements (changes only, --full for all);
                                    --window: only windows whose title contains TEXT, --focused: only
                                    the focused element, --cells: at most N visible table cells (300)
  click N | click X Y [--right|--middle] [--double]
  set-value N TEXT... [--paste|--keys]   click element N, select all, type TEXT (checked like type)
  scroll N DY | scroll X Y DY [DX]  positive DY scrolls down
  move N|X Y | mousedown N|X Y | mouseup N|X Y
  drag X1 Y1 X2 Y2
  type TEXT... [--paste|--keys] [--delay-ms N]
                                    type literal text. Text off the US keymap (accents, CJK, emoji,
                                    curly quotes) goes through the clipboard and ctrl+v (ctrl+shift+v in
                                    a terminal); --paste forces that, --keys forces keysyms, --delay-ms
                                    paces keysyms for slow VNC servers. In a sandbox the reply names the
                                    focused element and says verified=True/False (None: cannot tell),
                                    with want, got and first_bad_offset on a mismatch, and a warning
                                    when focus is on a button or menu, where letters get lost
  key COMBO                         e.g. ctrl+l, Return, alt+F4, ctrl+shift+t
  open FILE|URL                     open a URL, or a file in its default app; a host file is copied
                                    into /home/guest first (sandboxes only)
  paste TEXT...                     put UTF-8 text on the remote clipboard (does not press ctrl+v)
  clipboard                         print the remote clipboard text
  screenshot [PATH] [--scale S]     save a PNG (or print base64 with --json and no PATH)
  wait MS
  wait-idle [--idle MS] [--timeout MS]   block until the screen stops changing
  wait-for TEXT [--timeout MS]      poll state until an element or a window title contains TEXT
                                    (ignoring case and curly quotes); prints the lines, exit 1 on timeout
  record start [PATH] [--fps N] | record stop
  info | done | quit                done clears the AGENT ACTING badge, quit closes the session
N is an element number from your latest `state`. X Y are remote desktop pixels; `info` prints the size.
Flags go before a `--`; everything after it is text, e.g. type -- --not-a-flag"""


def cmd_action(argv):
    ap = argparse.ArgumentParser(prog="termdesk action", epilog=ACTION_HELP,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="session to drive (needed when several run)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    words = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not words:
        ap.print_help()
        return 1
    from .control import Client
    client = Client(args.name)
    if words[0] in ("wait-for", "wait_for"):
        return _wait_for_element(client, words[1:], args.json)
    req = parse_action(words)
    if req["cmd"] in ("wait", "wait_idle"):
        client.timeout = max(client.timeout, float(req.get("ms") or req.get("timeout_ms") or 0) / 1000 + 30)
    elif req.get("delay_ms"):
        client.timeout += len(req.get("text", "")) * req["delay_ms"] / 1000
    resp = client.call(**req)
    if args.json:
        print(json.dumps(resp))
    elif "png_base64" in resp:
        print(resp["png_base64"])
    else:
        text = format_reply(req["cmd"], resp)
        if text:
            print(text)
    return 0


def format_reply(cmd, resp):
    """A control reply as text: state and clipboard as they are, anything else as key=value pairs."""
    resp = {k: v for k, v in resp.items() if k != "ok"}
    if "state" in resp:
        return resp["state"]
    if cmd == "clipboard":
        return resp.get("text", "")
    return "  ".join(f"{k}={_show(k, v)}" for k, v in resp.items())


def _show(key, value):
    if isinstance(value, (dict, list)) or key in ("want", "got"):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_JSON_STR = re.compile(r'"(?:[^"\\]|\\.)*"')


def _loose(text, decode=False):
    """Lowercase with curly quotes straightened; decode=True also unescapes the JSON strings of a state line."""
    if decode:
        def raw(m):
            try:
                return '"' + json.loads(m.group()) + '"'
            except ValueError:
                return m.group()
        text = _JSON_STR.sub(raw, text)
    return text.translate(_QUOTES).lower()


def _state_matches(state, text):
    """Element lines and `== window` headers containing text, else the windows: line if a title does."""
    want = _loose(text)
    lines = state.split("\n")
    hits = [l for l in lines if l.startswith(("[", "== ")) and want in _loose(l, decode=True)]
    if not hits and lines[0].startswith("windows: ") and want in _loose(lines[0]):
        hits = [lines[0]]
    return hits


def _wait_for_element(client, words, as_json):
    """Poll `state` until an element or window title contains TEXT, then print the matching lines."""
    words = list(words)
    timeout = float(_flag(words, "--timeout", 30000)) / 1000
    text = " ".join(words)
    hits, error = wait_for_text(client, text, timeout)
    if as_json:
        print(json.dumps({"found": bool(hits), "lines": hits, "error": str(error) if error else None}))
    elif hits:
        print("\n".join(hits))
    else:
        print(f"timed_out=True  no element or window matching {text!r}" + (f"  last error: {error}" if error else ""))
    return 0 if hits else 1


def wait_for_text(client, text, timeout):
    """(matching state lines, last error) once an element or window title contains text, or at timeout."""
    from .control import SessionError
    if not text:
        raise ValueError("wait-for needs the text to look for")
    deadline = time.time() + timeout
    hits, error = [], None
    while True:
        client.timeout = max(2.0, deadline - time.time() + 1)
        try:
            hits = _state_matches(client.call("state", full=True)["state"], text)
            error = None
        except SessionError as e:
            if e.permanent:
                raise
            error = e
        except (RuntimeError, OSError) as e:
            error = e
        if hits or time.time() >= deadline:
            return hits, error
        time.sleep(0.5)


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


def _text_words(words, flags):
    """Pull flags (name -> takes a value) out of the words before any `--`; returns (flags, other words)."""
    head, tail = list(words), []
    if "--" in words:
        i = words.index("--")
        head, tail = list(words[:i]), list(words[i + 1:])
    return {name: _flag(head, name, None, takes) for name, takes in flags.items()}, head + tail


def _type_req(cmd, words):
    flags, words = _text_words(words, {"--paste": False, "--keys": False, "--delay-ms": True})
    req = {"cmd": cmd}
    if cmd == "set_value":
        req["index"] = int(words.pop(0))
    req["text"] = " ".join(words)
    if flags["--paste"] and flags["--keys"]:
        raise ValueError("pick one of --paste and --keys")
    if flags["--paste"] or flags["--keys"]:
        req["mode"] = "paste" if flags["--paste"] else "keys"
    if flags["--delay-ms"]:
        req["delay_ms"] = float(flags["--delay-ms"])
    return req


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
        req = {"cmd": "state", "full": bool(_flag(words, "--full", False, False))}
        if _flag(words, "--focused", False, False):
            req["focused"] = True
        window, cells = _flag(words, "--window"), _flag(words, "--cells")
        if window:
            req["window"] = window
        if cells:
            req["cells"] = int(cells)
        if words:
            raise ValueError(f"state does not take {' '.join(words)!r}; try `termdesk action --help`")
        return req
    if cmd in ("click", "move", "mousedown", "mouseup"):
        button = "right" if _flag(words, "--right", False, False) else "middle" if _flag(words, "--middle", False, False) else "left"
        count = 2 if _flag(words, "--double", False, False) else 1
        req = {"cmd": cmd, "button": button, "count": count}
        if len(words) == 1:
            req["index"] = int(words[0])
        else:
            req["x"], req["y"] = float(words[0]), float(words[1])
        return req
    if cmd in ("type", "set_value"):
        return _type_req(cmd, words)
    if cmd == "drag":
        return {"cmd": "drag", "x": float(words[0]), "y": float(words[1]), "to_x": float(words[2]), "to_y": float(words[3])}
    if cmd == "scroll":
        if len(words) == 2:
            return {"cmd": "scroll", "index": int(words[0]), "dy": int(words[1]), "dx": 0}
        return {"cmd": "scroll", "x": float(words[0]), "y": float(words[1]), "dy": int(words[2]),
                "dx": int(words[3]) if len(words) > 3 else 0}
    if cmd == "paste":
        return {"cmd": cmd, "text": " ".join(_text_words(words, {})[1])}
    if cmd == "open":
        target = " ".join(words)
        if not target:
            raise ValueError("open needs a file or a URL")
        return {"cmd": "open", "target": os.path.abspath(target) if os.path.exists(target) else target}
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
    if cmd in ("info", "done", "quit", "clipboard"):
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
        if s.get("busy"):
            print(f"{s['name']:24} busy (did not answer within 1s; still running)")
            continue
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
SKILL = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")


def cmd_setup(argv):
    """Install the Claude Code skill so agents know how to drive termdesk."""
    dest_dir = os.path.expanduser("~/.claude/skills/termdesk")
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copy(SKILL, os.path.join(dest_dir, "SKILL.md"))
    print(f"installed {dest_dir}/SKILL.md")
    print("other agents: add the MCP server " + MCP_CONFIG)
    print("              or give them the guide: termdesk guide >> AGENTS.md")
    if not shutil.which("docker"):
        print("docker not found: `termdesk sandbox` will not work until it is installed")
    return 0


# ------------------------------------------------------------------ guide
def guide_text():
    """The agent guide: SKILL.md without its front matter."""
    with open(SKILL, encoding="utf-8") as f:
        text = f.read()
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1]
    return text.lstrip("\n")


def cmd_guide(argv):
    sys.stdout.write(guide_text())
    return 0


# -------------------------------------------------------------------- mcp
def cmd_mcp(argv):
    from . import mcp
    return mcp.main(argv)
