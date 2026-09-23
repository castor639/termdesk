#!/usr/bin/env python3
"""Dump the visible accessibility tree of the desktop as JSON, for the termdesk agent.

Runs inside the sandbox: python3 a11y_dump.py [--max N]
"""
import json
import os
import sys

import pyatspi

_geo = os.environ.get("GEOMETRY", "1280x800").split("x")
SCREEN_W, SCREEN_H = int(_geo[0]), int(_geo[1])

INTERACTIVE = {
    "push button", "toggle button", "menu", "menu item", "check menu item", "radio menu item",
    "text", "entry", "password text", "link", "check box", "radio button", "list item", "tab",
    "page tab", "combo box", "icon", "tree item", "table cell", "slider", "spin button",
    "document web", "document frame", "terminal", "scroll bar", "label", "heading", "image",
}
SKIP_STATES = set()
MAX_DEPTH = 40


def state_names(acc):
    try:
        st = acc.getState()
    except Exception:
        return []
    names = []
    for flag, name in ((pyatspi.STATE_FOCUSED, "focused"), (pyatspi.STATE_EDITABLE, "editable"),
                       (pyatspi.STATE_SELECTED, "selected"), (pyatspi.STATE_CHECKED, "checked"),
                       (pyatspi.STATE_EXPANDED, "expanded"), (pyatspi.STATE_ACTIVE, "active")):
        if st.contains(flag):
            names.append(name)
    if not st.contains(pyatspi.STATE_ENABLED) or not st.contains(pyatspi.STATE_SENSITIVE):
        names.append("disabled")
    return names


def visible(acc):
    try:
        st = acc.getState()
        return st.contains(pyatspi.STATE_SHOWING) and st.contains(pyatspi.STATE_VISIBLE)
    except Exception:
        return False


def extents(acc):
    try:
        comp = acc.queryComponent()
        e = comp.getExtents(pyatspi.DESKTOP_COORDS)
        return int(e.x), int(e.y), int(e.width), int(e.height)
    except Exception:
        return None


def walk(acc, app, window, depth, out, seen):
    if depth > MAX_DEPTH or len(out) > 2000:
        return
    try:
        children = list(acc)
    except Exception:
        children = []
    for child in children:
        if child is None or id(child) in seen:
            continue
        seen.add(id(child))
        try:
            role = child.getRoleName()
            name = (child.name or "").strip()
        except Exception:
            continue
        if not visible(child):
            continue
        ext = extents(child)
        if ext and (ext[0] >= SCREEN_W or ext[1] >= SCREEN_H or ext[0] + ext[2] <= 0 or ext[1] + ext[3] <= 0):
            continue  # hidden panel or off-screen
        if ext and ext[2] > 0 and ext[3] > 0 and (role in INTERACTIVE or name):
            desc = ""
            try:
                desc = (child.description or "").strip()
            except Exception:
                pass
            value = None
            try:
                if role in ("text", "entry", "password text"):
                    t = child.queryText()
                    value = t.getText(0, min(t.characterCount, 200))
            except Exception:
                pass
            item = {"role": role, "name": name[:120], "x": ext[0], "y": ext[1], "w": ext[2], "h": ext[3],
                    "app": app, "window": window}
            if desc and desc != name:
                item["desc"] = desc[:120]
            if value:
                item["value"] = value
            states = state_names(child)
            if states:
                item["states"] = states
            out.append(item)
        walk(child, app, window, depth + 1, out, seen)


def dump():
    desktop = pyatspi.Registry.getDesktop(0)
    elements, windows = [], []
    for app in desktop:
        if app is None:
            continue
        try:
            app_name = app.name or ""
        except Exception:
            continue
        for win in app:
            if win is None or not visible(win):
                continue
            try:
                title = win.name or ""
                st = win.getState()
                active = st.contains(pyatspi.STATE_ACTIVE)
            except Exception:
                continue
            ext = extents(win)
            windows.append({"app": app_name, "title": title, "active": active, "extents": ext})
            walk(win, app_name, title, 0, elements, set())
    return windows, elements


def main():
    limit = 120
    if "--max" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--max") + 1])
    windows, elements = dump()
    active = [w["title"] for w in windows if w["active"]]

    def priority(e):
        p = 0
        if e["window"] in active:
            p -= 100
        if e["role"] in ("push button", "menu item", "link", "entry", "text", "tab", "page tab", "list item", "icon", "check box"):
            p -= 10
        if "focused" in e.get("states", []):
            p -= 50
        return p

    elements.sort(key=priority)
    elements = elements[:limit]
    for i, e in enumerate(elements, 1):
        e["id"] = f"e{i}"
    print(json.dumps({"windows": windows, "elements": elements}))


if __name__ == "__main__":
    main()
