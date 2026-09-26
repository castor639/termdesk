"""termdesk mcp: an MCP server on stdio, so any MCP client can drive termdesk desktops.

JSON-RPC 2.0, one message per line. Each tool maps onto a control-socket request or a sandbox call.
"""
import json
import os
import sys
import time

from . import __version__

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TEXT_LIMIT = 20000

INSTRUCTIONS = """termdesk gives you a real Linux desktop (XFCE, Firefox, LibreOffice, a terminal) in a disposable
Docker sandbox that the human can watch live.
Start: sandbox_up, then window so the human sees what you do.
Loop: state -> act on an element by its [N] index -> read the reply -> state again to check the result.
- state lists the UI as numbered elements, only what changed since the last call (full=true for everything),
  plus a focused: line. Big tables and lists show the visible cells only; scroll to see more.
- type and set_value reply verified=True/False and a warning when focus is on a button or menu, where letters
  get lost. Act on that before typing more. In a new app, check the first thing you type before doing more.
- wait_for after opening an app, a page or a dialog. screenshot when the tree is not enough.
- For bulk data, copy files in with sandbox_cp and change them with scripts through sandbox_exec, then use the
  GUI to look at and check the result.
Call done when finished. The full guide is `termdesk guide`."""

SESSION = {"type": "string", "description": "session name; defaults to the one sandbox_up started, or the only one running"}
MODE = {"type": "string", "enum": ["paste", "keys"],
        "description": "paste: always through the clipboard; keys: always as keystrokes. Default picks per text."}


def _tool(name, description, props=None, required=()):
    schema = {"type": "object", "properties": dict(props or {}), "required": list(required)}
    if name not in ("sandbox_up", "sandbox_down", "sandbox_cp", "sandbox_exec", "sessions"):
        schema["properties"]["session"] = SESSION
    return {"name": name, "description": description, "inputSchema": schema}


TOOLS = [
    _tool("sandbox_up", "Start a disposable desktop sandbox (XFCE, Firefox, LibreOffice) and a background session "
          "for it. Call window next so the human can watch.",
          {"name": {"type": "string", "description": "sandbox and session name, e.g. work"},
           "image": {"type": "string", "description": "desktop image; defaults to the termdesk image"},
           "share": {"type": "string", "description": "host folder to mount at /home/guest/shared, read-only; "
                                                      "add :rw for writable"}}),
    _tool("window", "Open the session in a new terminal window on the human's screen, so they can watch and "
          "take over. Keyboard focus moves to that window, so say so first."),
    _tool("sessions", "List running sessions."),
    _tool("state", "The screen as numbered elements: role, name, value, position. By default only what changed "
          "since the last state; the first call and full=true list everything. Also shows the focused element.",
          {"full": {"type": "boolean"},
           "window": {"type": "string", "description": "only windows whose title contains this"},
           "focused": {"type": "boolean", "description": "only the window list and the focused element"},
           "cells": {"type": "integer", "description": "at most this many visible table cells (default 300)"}}),
    _tool("click", "Click element [index] from the latest state, or desktop pixel x,y.",
          {"index": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"},
           "button": {"type": "string", "enum": ["left", "right", "middle"]}, "double": {"type": "boolean"}}),
    _tool("set_value", "Click element [index], select all, and type text. The reply says whether the text landed.",
          {"index": {"type": "integer"}, "text": {"type": "string"}, "mode": MODE}, ["index", "text"]),
    _tool("type", "Type text where the keyboard focus is. \\t and \\n press Tab and Return. The reply names the "
          "focused element and says verified=True/False; heed any warning.",
          {"text": {"type": "string"}, "mode": MODE,
           "delay_ms": {"type": "number", "description": "pause between keystrokes, for slow VNC servers"}},
          ["text"]),
    _tool("key", "Press a key or combination: Return, Tab, Escape, ctrl+s, alt+F4, ctrl+shift+t.",
          {"combo": {"type": "string"}}, ["combo"]),
    _tool("scroll", "Scroll at element [index] or pixel x,y; positive dy scrolls down, in wheel clicks.",
          {"index": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"},
           "dy": {"type": "integer"}, "dx": {"type": "integer"}}, ["dy"]),
    _tool("drag", "Drag with the left button from pixel x,y to to_x,to_y.",
          {"x": {"type": "number"}, "y": {"type": "number"}, "to_x": {"type": "number"}, "to_y": {"type": "number"}},
          ["x", "y", "to_x", "to_y"]),
    _tool("wait_for", "Wait until an element or window title contains text (ignoring case and curly quotes). "
          "Use after opening an app, a page or a dialog.",
          {"text": {"type": "string"}, "timeout_ms": {"type": "integer", "description": "default 30000"}}, ["text"]),
    _tool("wait_idle", "Wait until the screen stops changing.",
          {"idle_ms": {"type": "integer", "description": "default 300"},
           "timeout_ms": {"type": "integer", "description": "default 5000"}}),
    _tool("screenshot", "A PNG of the desktop.",
          {"scale": {"type": "number", "description": "e.g. 0.5 for half size; default 1"}}),
    _tool("clipboard", "The desktop's clipboard text."),
    _tool("set_clipboard", "Put text on the desktop's clipboard (does not paste it).",
          {"text": {"type": "string"}}, ["text"]),
    _tool("open", "Open a URL, or a file in its default app. A host file is copied into /home/guest first.",
          {"target": {"type": "string", "description": "URL, host path, or path inside the sandbox"}}, ["target"]),
    _tool("sandbox_cp", "Copy files between the host and a sandbox. NAME:PATH is the sandbox side (relative "
          "paths start at /home/guest); the other side is a host path.",
          {"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"]),
    _tool("sandbox_exec", "Run a shell command inside a sandbox, as guest (or root). Good for scripts that edit "
          "files, installing packages as root, and checking results.",
          {"command": {"type": "string"}, "root": {"type": "boolean"},
           "sandbox": {"type": "string", "description": "defaults to the sandbox of the current session"}},
          ["command"]),
    _tool("sandbox_down", "Remove a sandbox and everything in it.", {"name": {"type": "string"}}, ["name"]),
    _tool("done", "Clear the AGENT ACTING badge the human sees. Call when finished."),
]
TOOL_NAMES = {t["name"] for t in TOOLS}


class ToolError(Exception):
    pass


class Server:
    def __init__(self, out):
        self.out = out
        self.session = None

    def send(self, msg):
        self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.out.flush()

    # ---------------------------------------------------------------- tools
    def client(self, args, timeout=60.0):
        from .control import Client
        return Client(args.get("session") or self.session, timeout=timeout)

    def call(self, args, cmd, **req):
        from .cli import format_reply
        resp = self.client(args).call(cmd, **{k: v for k, v in req.items() if v is not None})
        return format_reply(cmd, resp) or "ok"

    def sandbox_name(self, args):
        from . import sandbox
        if args.get("sandbox"):
            return args["sandbox"]
        name = args.get("session") or self.session
        if name and any(e["name"] == name for e in sandbox.ls()):
            return name
        return sandbox.resolve_name(None)

    def tool(self, name, args):
        from . import sandbox
        if name == "sandbox_up":
            from .control import start_headless
            info = sandbox.up(name=args.get("name"), image=args.get("image") or sandbox.DEFAULT_IMAGE,
                              share=args.get("share"))
            start_headless(info["target"], info["name"])
            self.session = info["name"]
            text = f"sandbox {info['name']} is up at {info['target']} with a background session of the same name."
            if args.get("share"):
                text += f" {args['share']} is mounted at {sandbox.SHARE_DIR}."
            return text + " Call window so the human can watch, then state."
        if name == "window":
            from .cli import open_window
            s = self.client(args)
            info = s.call("info")
            session = os.path.basename(s.path)[:-5]
            return open_window(info.get("addr") or info["target"], session)
        if name == "sessions":
            from .control import list_sessions
            rows = [f"{s['name']}  busy" if s.get("busy") else
                    f"{s['name']}  {s.get('addr') or ''}  {s.get('width')}x{s.get('height')}"
                    + ("  headless" if s.get("headless") else "") for s in list_sessions()]
            return "\n".join(rows) or "no sessions"
        if name == "state":
            return self.call(args, "state", full=bool(args.get("full")), window=args.get("window"),
                             focused=bool(args.get("focused")) or None, cells=args.get("cells"))
        if name == "click":
            req = self.point(args)
            return self.call(args, "click", button=args.get("button") or "left",
                             count=2 if args.get("double") else 1, **req)
        if name in ("set_value", "type"):
            req = {"text": args["text"], "mode": args.get("mode"), "delay_ms": args.get("delay_ms")}
            if name == "set_value":
                req["index"] = int(args["index"])
            return self.call(args, name, **req)
        if name == "key":
            return self.call(args, "key", combo=args["combo"])
        if name == "scroll":
            return self.call(args, "scroll", dy=int(args["dy"]), dx=int(args.get("dx") or 0), **self.point(args))
        if name == "drag":
            return self.call(args, "drag", **{k: float(args[k]) for k in ("x", "y", "to_x", "to_y")})
        if name == "wait_for":
            from .cli import wait_for_text
            hits, error = wait_for_text(self.client(args), args["text"], float(args.get("timeout_ms") or 30000) / 1000)
            if hits:
                return "\n".join(hits)
            raise ToolError(f"nothing matched {args['text']!r} before the timeout" + (f"; last error: {error}" if error else ""))
        if name == "wait_idle":
            timeout = float(args.get("timeout_ms") or 5000)
            resp = self.client(args, timeout=timeout / 1000 + 30).call(
                "wait_idle", idle_ms=float(args.get("idle_ms") or 300), timeout_ms=timeout)
            return "timed out; the screen is still changing" if resp.get("timed_out") else "idle"
        if name == "screenshot":
            resp = self.client(args).call("screenshot", scale=float(args.get("scale") or 1))
            return [{"type": "image", "data": resp["png_base64"], "mimeType": "image/png"},
                    {"type": "text", "text": f"{resp['width']}x{resp['height']} at scale {resp['scale']:g}; "
                                             "divide pixel positions by the scale before clicking"}]
        if name == "clipboard":
            return self.client(args).call("clipboard")["text"]
        if name == "set_clipboard":
            return self.call(args, "paste", text=args["text"])
        if name == "open":
            target = args["target"]
            if os.path.exists(target):
                target = os.path.abspath(target)
            return self.call(args, "open", target=target)
        if name == "sandbox_cp":
            return "copied to " + sandbox.cp(args["src"], args["dst"])
        if name == "sandbox_exec":
            r = sandbox.exec(self.sandbox_name(args), ["sh", "-c", args["command"]],
                             "root" if args.get("root") else "guest")
            text = f"exit {r.returncode}"
            if r.stdout:
                text += "\n" + r.stdout[-TEXT_LIMIT:]
            if r.stderr:
                text += "\nstderr:\n" + r.stderr[-TEXT_LIMIT // 4:]
            if r.returncode:
                raise ToolError(text)
            return text
        if name == "sandbox_down":
            removed = sandbox.down(name=args["name"])
            if self.session in removed:
                self.session = None
            return "removed " + ", ".join(removed)
        if name == "done":
            return self.call(args, "done")
        raise ToolError(f"unknown tool {name}")

    @staticmethod
    def point(args):
        if args.get("index") is not None:
            return {"index": int(args["index"])}
        if args.get("x") is None or args.get("y") is None:
            raise ToolError("give an element index from state, or x and y")
        return {"x": float(args["x"]), "y": float(args["y"])}

    # ------------------------------------------------------------- protocol
    def handle(self, msg):
        """The response to one message, or None for notifications and responses."""
        if not isinstance(msg, dict) or "method" not in msg:
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        method, mid = msg["method"], msg.get("id")
        params = msg.get("params") or {}
        if mid is None:
            return None
        try:
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            result = self.dispatch(method, params)
        except LookupError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": str(e)}}
        except (TypeError, ValueError) as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def dispatch(self, method, params):
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "termdesk", "version": __version__},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in TOOL_NAMES:
                raise ValueError(f"unknown tool {name!r}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            try:
                out = self.tool(name, args)
            except KeyError as e:
                return _result(f"missing argument {e}", True)
            except Exception as e:
                return _result(str(e) or type(e).__name__, True)
            return {"content": out} if isinstance(out, list) else _result(out)
        raise LookupError(f"method not found: {method}")


def _result(text, error=False):
    res = {"content": [{"type": "text", "text": text}]}
    if error:
        res["isError"] = True
    return res


def serve(inp, out):
    server = Server(out)
    for raw in inp:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            server.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(msg, list):  # a batch, from clients on older protocol versions
            replies = [r for r in (server.handle(m) for m in msg) if r is not None]
            if replies:
                server.send(replies)
            continue
        reply = server.handle(msg)
        if reply is not None:
            server.send(reply)


def main(argv=None):
    if argv:
        sys.stderr.write("usage: termdesk mcp   (an MCP server on stdin and stdout; add it to your client's config)\n")
        return 1
    # the protocol owns stdout; anything else that prints, here or in a child process, goes to stderr
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    started = time.time()
    try:
        serve(sys.stdin.buffer, out)
    except KeyboardInterrupt:
        pass
    sys.stderr.write(f"termdesk mcp: stdin closed after {time.time() - started:.0f}s\n")
    return 0
