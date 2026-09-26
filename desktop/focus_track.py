#!/usr/bin/env python3
"""Keep /tmp/termdesk-focus.json in step with the keyboard focus, for termdesk.

start.sh runs this for the life of the sandbox. On every focus change, and on every text change
of the focused object, the file is rewritten atomically with the focused object's role, name, app,
window, editable flag and text.
"""
import json
import os
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402
import pyatspi  # noqa: E402

PATH = "/tmp/termdesk-focus.json"
TEXT_CHARS = 4000
FLUSH_MS = 80
TOP_ROLES = {"application", "frame", "window", "dialog", "alert", "file chooser"}
LABELED_ROLES = {"text", "entry", "paragraph", "terminal", "spin button"}


def extents(acc):
    try:
        e = acc.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        return (int(e.x), int(e.y), int(e.width), int(e.height)) if e.width >= 0 and e.height >= 0 else None
    except Exception:
        return None


def locate(acc):
    """The window holding acc, and the parent's name and screen offset a11y_dump.py gives it."""
    chain, node = [acc], acc
    for _ in range(60):
        parent = node.parent
        if parent is None or parent.getRole() == pyatspi.ROLE_APPLICATION:
            break
        chain.append(parent)
        node = parent
    label = (chain[1].name or "").strip() if len(chain) > 2 else ""
    shift, sole = (0, 0), None
    for node in reversed(chain):
        ext = extents(node)
        if ext and sole and ext[2:] == sole[2:] and ext[:2] != sole[:2]:
            shift = (sole[0] + shift[0] - ext[0], sole[1] + shift[1] - ext[1])
        defunct = node.getState().contains(pyatspi.STATE_DEFUNCT)
        sole = ext if node is not chain[-1] and not defunct and node.childCount == 1 else None
    return chain[-1], label, shift


def text_near_caret(acc):
    try:
        t = acc.queryText()
    except Exception:
        return None, None
    n = t.characterCount
    caret = max(0, t.caretOffset)
    start = 0 if n <= TEXT_CHARS else max(0, min(caret - TEXT_CHARS // 2, n - TEXT_CHARS))
    text = t.getText(start, min(n, start + TEXT_CHARS)) if n > 0 else ""
    # U+FFFC stands for embedded children (links, paragraphs), not text
    return text.replace("\ufffc", ""), len(text[:caret - start].replace("\ufffc", ""))


class Tracker:
    def __init__(self):
        self.current = None
        self.window = None
        self.label = ""
        self.shift = (0, 0)
        self.moved = (0.0, None)
        self.pending = False

    def focus(self, acc):
        if acc is None:
            return
        self.current = acc
        self.window = None
        self.write()

    def later(self):
        if not self.pending:
            self.pending = True
            GLib.timeout_add(FLUSH_MS, self.flush)

    def flush(self):
        self.pending = False
        self.write()
        return False

    def write(self):
        acc = self.current
        try:
            st = acc.getState()
            role = acc.getRoleName()
            if self.window is None:
                self.window, self.label, self.shift = locate(acc)
            name = (acc.name or "").strip() or (self.label if role in LABELED_ROLES else "")
            data = {"role": role, "name": name, "app": acc.getApplication().name or "",
                    "window": self.window.name or "", "editable": st.contains(pyatspi.STATE_EDITABLE),
                    "text": None, "ts": time.time()}
            if role != "password text":
                text, caret = text_near_caret(acc)
                if text is not None:
                    if role == "terminal":
                        text = text.rstrip("\n")  # VTE pads the screen with empty rows
                    data["text"], data["caret"] = text, min(caret, len(text))
            ext = extents(acc)
            if ext:
                data.update(x=ext[0] + self.shift[0], y=ext[1] + self.shift[1], w=ext[2], h=ext[3])
        except Exception:
            return
        tmp = PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, PATH)
        except OSError:
            pass

    def echo(self, acc):
        """LibreOffice claims focus for its input line whenever the cell cursor moves, while the
        grid keeps the keyboard. Ignore claims from outside a container that still has focus and
        moved its active descendant a moment ago."""
        t, box = self.moved
        if box is None or time.monotonic() - t > 0.3 or not box.getState().contains(pyatspi.STATE_FOCUSED):
            return False
        node = acc
        for _ in range(40):
            if node is None:
                return True
            if node == box:
                return False
            node = node.parent
        return True

    def on_event(self, ev):
        try:
            kind = ev.type
            if kind.startswith("object:state-changed:focused") or kind.startswith("focus:"):
                if (ev.detail1 or kind.startswith("focus:")) and ev.source.getRole() not in (
                        pyatspi.ROLE_APPLICATION, pyatspi.ROLE_FRAME) and not self.echo(ev.source):
                    self.focus(ev.source)
            elif kind.startswith("object:active-descendant-changed"):
                if ev.source == self.current or ev.source.getState().contains(pyatspi.STATE_FOCUSED):
                    self.moved = (time.monotonic(), ev.source)
                    self.focus(ev.any_data)
            elif kind.startswith("object:text-changed"):
                if ev.source == self.current:
                    self.later()
            elif kind.startswith("object:property-change:accessible-name"):
                if ev.source == self.current or ev.source == self.window:
                    self.later()
        except Exception:
            pass

    def scan(self):
        """Pick up the focus that existed before this process started."""
        desktop = pyatspi.Registry.getDesktop(0)
        for i in range(desktop.childCount):
            app = desktop.getChildAtIndex(i)
            for j in range(max(0, app.childCount if app else 0)):
                win = app.getChildAtIndex(j)
                if win is not None and win.getState().contains(pyatspi.STATE_ACTIVE):
                    found = find_focused(win, 0)
                    if found is not None:
                        self.focus(found)
                        return


def find_focused(acc, depth):
    try:
        n = acc.childCount
        if depth > 30 or n > 500 or acc.getState().contains(pyatspi.STATE_MANAGES_DESCENDANTS):
            return None
        for i in range(n):
            child = acc.getChildAtIndex(i)
            if child is None:
                continue
            st = child.getState()
            if st.contains(pyatspi.STATE_SHOWING) or st.contains(pyatspi.STATE_FOCUSED):
                found = find_focused(child, depth + 1)
                if found is not None:
                    return found
            if st.contains(pyatspi.STATE_FOCUSED) and child.getRoleName() not in TOP_ROLES:
                return child
    except Exception:
        pass
    return None


def main():
    Atspi.set_timeout(1000, 0)
    t = Tracker()
    try:
        t.scan()
    except Exception as e:
        print("initial scan failed:", e, file=sys.stderr)
    pyatspi.Registry.registerEventListener(
        t.on_event, "object:state-changed:focused", "focus:", "object:active-descendant-changed",
        "object:text-changed", "object:property-change:accessible-name")
    pyatspi.Registry.start()


if __name__ == "__main__":
    main()
