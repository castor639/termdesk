#!/usr/bin/env python3
"""Dump the visible accessibility tree of the desktop as JSON, for the termdesk agent.

Runs inside the sandbox as guest:
  python3 a11y_dump.py [--max N] [--cells N] [--window SUBSTR] [--focused-only] [--deadline SECONDS]

Always prints exactly one JSON object, also when an app hangs or the deadline passes;
then "partial" is true and "errors" says what was skipped.
"""
import argparse
import json
import os
import re
import threading
import time

FOCUS_FILE = "/tmp/termdesk-focus.json"
CALL_TIMEOUT_MS = 1500
STALL = CALL_TIMEOUT_MS / 1000 * 0.9
MAX_DEPTH = 40
CHILD_CAP = 500
MAX_NODES = 8000
POINT_PROBES = 400
VALUE_CHARS = 200
CELL_CHARS = 1000
FOCUS_CHARS = 1000

INTERACTIVE = {
    "push button", "toggle button", "menu", "menu item", "check menu item", "radio menu item",
    "text", "entry", "password text", "link", "check box", "radio button", "list item", "tab",
    "page tab", "combo box", "icon", "tree item", "table cell", "slider", "spin button",
    "document web", "document frame", "terminal", "scroll bar", "label", "heading", "image",
}
TEXT_ROLES = {"text", "entry", "paragraph", "terminal", "spin button", "table cell"}
LABELED_ROLES = {"text", "entry", "paragraph", "terminal", "spin button"}
CELL_ROLES = {"table cell", "column header", "row header", "table column header", "table row header"}
TOP_ROLES = {"application", "frame", "window", "dialog", "desktop frame"}
PRIORITY_ROLES = {"push button", "menu item", "link", "entry", "text", "tab", "page tab", "list item", "icon",
                  "check box"}
A1 = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})$")

_geo = os.environ.get("GEOMETRY", "1280x800").split("x")
SCREEN = [0, 0, int(_geo[0]), int(_geo[1])]

pyatspi = None


class Stop(Exception):
    pass


class Deadline(Stop):
    pass


class Hung(Stop):
    pass


def overlap(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


def extents(acc):
    try:
        e = acc.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        return int(e.x), int(e.y), int(e.width), int(e.height)
    except Exception:
        return None


def child_count(acc):
    try:
        return acc.childCount
    except Exception:
        return -1


def text_of(acc, limit, tail=False):
    # LibreOffice returns "" when the end offset passes characterCount, so ask for the exact length
    t = acc.queryText()
    n = t.characterCount
    if n <= 0:
        return ""
    s = t.getText(max(0, n - limit), n) if tail else t.getText(0, min(n, limit))
    s = s.replace("\ufffc", "")
    return s.rstrip() if tail else s


def state_names(st):
    names = [n for f, n in ((pyatspi.STATE_FOCUSED, "focused"), (pyatspi.STATE_EDITABLE, "editable"),
                            (pyatspi.STATE_SELECTED, "selected"), (pyatspi.STATE_CHECKED, "checked"),
                            (pyatspi.STATE_EXPANDED, "expanded"), (pyatspi.STATE_ACTIVE, "active"))
             if st.contains(f)]
    # LibreOffice cells are ENABLED without SENSITIVE; only a missing ENABLED means disabled
    if not st.contains(pyatspi.STATE_ENABLED):
        names.append("disabled")
    return names


def read_focus_file():
    try:
        with open(FOCUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("app") is not None else None
    except Exception:
        return None


class Dump:
    def __init__(self, args):
        self.args = args
        self.t0 = time.monotonic()
        self.soft = self.t0 + max(args.deadline - 2, args.deadline * 0.6)
        self.tick = self.t0
        self.all_windows = []
        self.windows = []
        self.ui, self.cells, self.focus_hits = [], [], []
        self.keys = set()
        self.errors = []
        self.partial = False
        self.nodes = 0
        self.cell_budget = args.cells
        self.in_cell = 0
        self.hung_apps = set()
        self.focus_file = read_focus_file()

    def check(self):
        now = time.monotonic()
        if now > self.soft:
            raise Deadline("deadline %gs reached" % self.args.deadline)
        # a call that ran into its timeout since the last check means the app is busy or hung
        stalled, self.tick = now - self.tick > STALL, now
        if stalled:
            raise Hung()

    def error(self, msg):
        self.partial = True
        if len(self.errors) < 20:
            self.errors.append(msg[:300])

    # ------------------------------------------------------------ elements
    def item(self, acc, role, name, ext, st, ctx, chars=VALUE_CHARS):
        e = {"role": role, "name": name[:120], "x": ext[0], "y": ext[1], "w": ext[2], "h": ext[3],
             "app": ctx[0], "window": ctx[1]}
        try:
            desc = (acc.description or "").strip()
            if desc and desc != name:
                e["desc"] = desc[:120]
        except Exception:
            pass
        if role in TEXT_ROLES:
            try:
                value = text_of(acc, chars, tail=role == "terminal")
                if value and value != name:
                    e["value"] = value
            except Exception:
                pass
        states = state_names(st)
        if states:
            e["states"] = states
        return e

    def emit(self, e, bucket, depth):
        key = (e["app"], e["role"], e["name"], e["x"], e["y"], e["w"], e["h"])
        if key in self.keys:
            return
        self.keys.add(key)
        bucket.append(e)
        if "focused" in e.get("states", ()):
            self.focus_hits.append((depth, e))

    # ---------------------------------------------------------------- walk
    # Extents are read in the app's own coordinates. `shift` maps them to the screen: a sole child
    # the size of its parent is drawn over it, so where it claims to be elsewhere the app is wrong.
    # LibreOffice reports its whole VCL tree 25 px too high after launch, and the GTK widgets
    # embedded in it correctly; this rule fixes both without knowing about either.
    def walk(self, acc, ctx, depth, label, shift=(0, 0), ext=None):
        if depth > MAX_DEPTH:
            return
        n = child_count(acc)
        sole = ext if n == 1 else None
        for i in range(min(n, CHILD_CAP)):
            self.check()
            try:
                child = acc.getChildAtIndex(i)
            except Exception:
                continue
            if child is not None:
                self.visit(child, ctx, depth + 1, label, shift, sole)

    def visit(self, acc, ctx, depth, label, shift=(0, 0), sole=None):
        if self.nodes >= MAX_NODES:
            raise Deadline("stopped after %d nodes" % MAX_NODES)
        try:
            role = acc.getRoleName()
            name = (acc.name or "").strip()
            st = acc.getState()
        except Exception:
            return
        self.nodes += 1
        if st.contains(pyatspi.STATE_DEFUNCT):
            # LibreOffice marks its input line panel defunct while the paragraph inside stays live
            self.walk(acc, ctx, depth, name, shift)
            return
        if not (st.contains(pyatspi.STATE_SHOWING) and st.contains(pyatspi.STATE_VISIBLE)):
            return
        ext = extents(acc)
        if ext and (ext[2] < 0 or ext[3] < 0):
            ext = None  # GTK notebook pages report -1,-1,-1,-1; keep walking their children
        if ext and role == "menu bar" and not (ext[2] and ext[3]):
            return  # a collapsed duplicate, like the VCL menu bar under LibreOffice's GTK one
        if ext and sole and ext[2:] == sole[2:] and ext[:2] != sole[:2]:
            shift = (sole[0] + shift[0] - ext[0], sole[1] + shift[1] - ext[1])
        real = (ext[0] + shift[0], ext[1] + shift[1], ext[2], ext[3]) if ext else None
        if real and real[2] and real[3] and not overlap(real, SCREEN):
            return
        n = child_count(acc)
        huge = st.contains(pyatspi.STATE_MANAGES_DESCENDANTS) or n > CHILD_CAP
        focused = st.contains(pyatspi.STATE_FOCUSED)
        wanted = role in INTERACTIVE or name or (role == "paragraph" and st.contains(pyatspi.STATE_FOCUSABLE))
        if real and real[2] > 0 and real[3] > 0 and (wanted or focused):
            # unnamed text takes its parent's name, as LibreOffice's "Input line" and "Cell B16" editors need
            e = self.item(acc, role, name or (label if role in LABELED_ROLES else ""), real, st, ctx)
            if self.in_cell and role in CELL_ROLES:
                if name or e.get("value") or e.get("states"):
                    self.cell_budget -= 1
                    self.emit(e, self.cells, depth)
            elif wanted:
                self.emit(e, self.ui, depth)
            else:
                self.focus_hits.append((depth, e))
        if huge:
            if real:
                self.visible_children(acc, ctx, depth, real, name, shift)
        elif n > 0:
            self.walk(acc, ctx, depth, name, shift, ext)

    # --------------------------------------------------- big containers
    def visible_children(self, acc, ctx, depth, real, label, shift):
        """Read only what is on screen: never enumerate a container with thousands of children."""
        clip = overlap(real, SCREEN)
        if clip and ctx[2]:
            clip = overlap(clip, ctx[2])
        if not clip or clip[2] < 3 or clip[3] < 3:
            return
        clip = (clip[0] - shift[0], clip[1] - shift[1], clip[2], clip[3])  # back to the app's coordinates
        try:
            ifaces = acc.get_interfaces() or []
        except Exception:
            ifaces = []
        done = False
        if "Table" in ifaces:
            try:
                done = self.table_cells(acc, ctx, depth, clip, shift)
            except Stop:
                raise
            except Exception as e:
                self.error("%s: table: %s" % (ctx[1], e))
        if not done:
            done = self.ordered_children(acc, ctx, depth, clip, label, shift)
        if not done and "Component" in ifaces:
            self.sample_points(acc, ctx, depth, clip, label, shift)
        if "Selection" in ifaces:
            self.selected(acc, ctx, depth, label, shift)

    def cell_pos(self, hit, table, T, sheet):
        for _ in range(5):
            if hit is None:
                return None
            if sheet:
                m = A1.match(hit.name or "")
                if m:
                    col = 0
                    for ch in m.group(1):
                        col = col * 26 + ord(ch) - 64
                    return int(m.group(2)) - 1, col - 1
            try:
                if "TableCell" in (hit.get_interfaces() or []):
                    _, r, c = hit.queryTableCell().get_position()
                    return r, c
                parent = hit.parent
            except Exception:
                return None
            if parent == table:
                i = hit.getIndexInParent()
                return T.getRowAtIndex(i), T.getColumnAtIndex(i)
            hit = parent
        return None

    def corner(self, table, T, sheet, clip, top):
        comp = table.queryComponent()
        x0, y0, x1, y1 = clip[0], clip[1], clip[0] + clip[2], clip[1] + clip[3]
        for k in range(0, min(clip[3] - 2, 160), 6):  # step past column headers drawn inside the table
            for inset in (2, 9):  # and past border spacing
                self.check()
                x, y = (x0 + inset, y0 + 1 + k) if top else (x1 - 1 - inset, y1 - 2 - k)
                pos = self.cell_pos(comp.getAccessibleAtPoint(x, y, pyatspi.DESKTOP_COORDS), table, T, sheet)
                if pos and pos[0] >= 0 and pos[1] >= 0:
                    return pos
        return None

    def table_cells(self, table, ctx, depth, clip, shift):
        T = table.queryTable()
        rows, cols = T.nRows, T.nColumns
        # Calc sheets have 2^20 x 2^14 cells, more than an int index can address; their cells are named A1, B16...
        sheet = rows * cols >= 2 ** 31 - 1
        start = self.corner(table, T, sheet, clip, True)
        if start is None:
            return False
        end = self.corner(table, T, sheet, clip, False)
        if end and (end[0] < start[0] or end[1] < start[1]):
            end = None

        def lit(r, c):
            if not (0 <= r < rows and 0 <= c < cols):
                return False
            cell = T.getAccessibleAt(r, c)
            e = extents(cell) if cell is not None else None
            return bool(e and e[2] > 0 and e[3] > 0 and overlap(e, clip))

        # Hit testing can land a row off (GTK counts the header row in TableCell positions)
        r0, c0 = start
        for _ in range(20):
            if lit(r0 - 1, c0):
                r0 -= 1
            elif lit(r0, c0 - 1):
                c0 -= 1
            else:
                break
        start = r0, c0
        if end:
            r1, c1 = end
            for _ in range(20):
                if lit(r1 + 1, c1):
                    r1 += 1
                elif lit(r1, c1 + 1):
                    c1 += 1
                else:
                    break
            end = r1, c1
        last_r, last_c = end if end else (rows - 1, cols - 1)
        # Off-screen cells are zero-size (LibreOffice) or outside the view (GTK); without a known
        # bottom-right cell, a few invisible rows or columns in a row mark the edge
        probes, limit, dark_rows = 0, max(200, self.args.cells * 3), 0
        for r in range(start[0], last_r + 1):
            lit, dark = False, 0
            for c in range(start[1], last_c + 1):
                self.check()
                probes += 1
                cell = T.getAccessibleAt(r, c)
                e = extents(cell) if cell is not None else None
                if e is None or e[0] >= clip[0] + clip[2]:
                    break
                if e[2] > 0 and e[3] > 0 and overlap(e, clip):
                    lit, dark = True, 0
                    self.cell(cell, ctx, depth + 1, (e[0] + shift[0], e[1] + shift[1], e[2], e[3]), shift, e)
                    if self.cell_budget <= 0:
                        self.error("%s: stopped after %d visible cells (--cells)" % (ctx[1], self.args.cells))
                        return True
                else:
                    dark += 1
                    if c == start[1] or (end is None and dark >= 4):
                        break
            dark_rows = 0 if lit else dark_rows + 1
            if probes >= limit or (end is None and dark_rows >= 4):
                break
        return True

    def cell(self, cell, ctx, depth, real, shift, ext):
        try:
            st = cell.getState()
            role = cell.getRoleName()
            name = (cell.name or "").strip()
        except Exception:
            return
        try:
            value = text_of(cell, CELL_CHARS)
        except Exception:
            value = ""  # GTK cells that hold an icon and a label have neither text nor name
        if value or st.contains(pyatspi.STATE_FOCUSED) or st.contains(pyatspi.STATE_SELECTED):
            e = {"role": role if role in CELL_ROLES else "table cell", "name": name[:120], "x": real[0], "y": real[1], "w": real[2], "h": real[3],
                 "app": ctx[0], "window": ctx[1]}
            if value and value != name:
                e["value"] = value
            try:
                for a in cell.getAttributes() or []:
                    if a.startswith("Formula:") and len(a) > 8:
                        # LibreOffice escapes , : ; with a backslash and cuts the value at the first
                        # colon, so SUM(C2:C5) arrives as SUM(C2\ ; the Input line has the whole formula
                        raw = a[8:]
                        cut = (len(raw) - len(raw.rstrip("\\"))) % 2 == 1
                        e["formula"] = "=" + re.sub(r"\\(.)", r"\1", raw[:-1] if cut else raw) + ("…" if cut else "")
            except Exception:
                pass
            states = state_names(st)
            if states:
                e["states"] = states
            self.cell_budget -= 1
            self.emit(e, self.cells, depth)
        if child_count(cell) > 0:
            self.in_cell += 1
            try:
                self.walk(cell, ctx, depth, name, shift, ext)
            finally:
                self.in_cell -= 1

    def ordered_children(self, acc, ctx, depth, clip, label, shift):
        """Documents and long lists lay children out top to bottom: binary search for the first
        one on screen, then read on until the children run below the view."""
        n = child_count(acc)
        top, bottom = clip[1], clip[1] + clip[3]

        def placed(i):
            for j in range(i, min(i + 8, n)):
                self.check()
                e = extents(acc.getChildAtIndex(j))
                if e and e[2] > 0 and e[3] > 0:
                    return j, e
            return i, None

        lo, hi = 0, n - 1
        while lo < hi:
            j, e = placed((lo + hi) // 2)
            if e is not None and e[1] + e[3] <= top:
                lo = j + 1
            else:
                hi = (lo + hi) // 2
        before, i, below = len(self.ui) + len(self.cells), max(0, lo - 2), 0
        while i < n and below < 10 and i < lo + CHILD_CAP:
            self.check()
            child = acc.getChildAtIndex(i)
            e = extents(child) if child is not None else None
            below = below + 1 if e and e[3] > 0 and e[1] >= bottom else 0
            if child is not None and not below:
                self.visit(child, ctx, depth + 1, label, shift)
            i += 1
        return len(self.ui) + len(self.cells) > before

    def sample_points(self, acc, ctx, depth, clip, label, shift):
        comp = acc.queryComponent()
        x0, y0, x1, y1 = clip[0], clip[1], clip[0] + clip[2], clip[1] + clip[3]
        seen, probes, y = set(), 0, y0 + 1
        while y < y1 and probes < POINT_PROBES:
            x, bottom = x0 + 1, None
            while x < x1 and probes < POINT_PROBES:
                self.check()
                probes += 1
                try:
                    hit = comp.getAccessibleAtPoint(x, y, pyatspi.DESKTOP_COORDS)
                except Exception:
                    hit = None
                e = extents(hit) if hit is not None and hit != acc else None
                if not e or e[2] <= 0 or e[3] <= 0 or not (e[0] <= x < e[0] + e[2]):
                    x += 24
                    continue
                if e not in seen:
                    seen.add(e)
                    self.visit(hit, ctx, depth + 1, label, shift)
                x = e[0] + e[2]
                bottom = e[1] + e[3] if bottom is None else min(bottom, e[1] + e[3])
            y = bottom if bottom and bottom > y else y + 16

    def selected(self, acc, ctx, depth, label, shift):
        try:
            sel = acc.querySelection()
            for i in range(min(sel.nSelectedChildren, 5)):
                self.check()
                child = sel.getSelectedChild(i)
                if child is not None:
                    self.visit(child, ctx, depth + 1, label, shift)
        except Stop:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------- windows
    def find_windows(self):
        found = []
        desktop = pyatspi.Registry.getDesktop(0)
        for i in range(child_count(desktop)):
            try:
                app = desktop.getChildAtIndex(i)
            except Exception:
                continue
            if app is None:
                continue
            self.tick = time.monotonic()
            try:
                found += self.app_windows(app)
            except Hung:
                self.hung(self.app_label(app), "skipped")
        return found

    def app_windows(self, app):
        count = child_count(app)
        self.check()  # libatspi gives -1 instead of raising when an app does not answer in time
        try:
            app_name = app.name or ""
        except Exception:
            app_name = ""
        out = []
        for j in range(min(count, 50)):
            self.check()
            try:
                win = app.getChildAtIndex(j)
                st = win.getState()
                if not (st.contains(pyatspi.STATE_SHOWING) and st.contains(pyatspi.STATE_VISIBLE)):
                    continue
                title = win.name or ""
                role = win.getRoleName()
            except Exception:
                continue
            active = st.contains(pyatspi.STATE_ACTIVE)
            w = {"app": app_name, "title": title, "active": active, "extents": extents(win)}
            if app_name == "xfdesktop" and w["extents"]:
                SCREEN[2] = max(SCREEN[2], w["extents"][2])
                SCREEN[3] = max(SCREEN[3], w["extents"][3])
            desk = app_name in ("xfce4-panel", "xfdesktop", "xfwm4")
            rank = 3 if desk else 0 if active else 1 if role in ("dialog", "alert", "file chooser") else 2
            out.append((rank, w, win))
        return out

    @staticmethod
    def app_label(app):
        try:
            with open("/proc/%d/comm" % app.get_process_id()) as f:
                return f.read().strip()
        except Exception:
            return "an app"

    def hung(self, app, what):
        self.hung_apps.add(app)
        self.error("%s: no reply within %d ms, busy or hung; %s" % (app, CALL_TIMEOUT_MS, what))

    def run(self):
        try:
            found = self.find_windows()
        except Deadline as e:
            self.error("%s while listing windows" % e)
            return
        self.all_windows = [w for _, w, _ in found]
        want = (self.args.window or "").lower()
        todo = [t for t in found if want in t[1]["title"].lower()]
        self.windows = [w for _, w, _ in todo]
        if self.args.focused_only:
            if self.fresh_focus():
                return
            todo = [t for t in todo if t[1]["active"]]
        # the active window and dialogs first, the panels last, so a deadline cuts what matters least
        for _, w, win in sorted(todo, key=lambda t: t[0]):
            if w["app"] in self.hung_apps:
                continue
            self.tick = time.monotonic()
            try:
                self.walk(win, (w["app"], w["title"], w["extents"]), 0, "")
            except Hung:
                self.hung(w["app"], "rest of its tree skipped")
            except Deadline as e:
                self.error("%s: %s, rest of the tree skipped" % (w["title"] or w["app"], e))
                break
            except Exception as e:
                self.error("%s: %s: %s" % (w["title"] or w["app"], type(e).__name__, e))

    # ------------------------------------------------------------- output
    def fresh_focus(self):
        f = self.focus_file
        return bool(f) and f.get("role") not in TOP_ROLES and any(
            w["active"] and w["app"] == f.get("app") and w["title"] == f.get("window") for w in self.all_windows)

    def focused(self, elements):
        f = self.focus_file
        if self.fresh_focus():
            same = [e for e in elements + [h for _, h in self.focus_hits]
                    if (e["app"], e["window"], e["role"], e["name"]) == (f["app"], f["window"], f.get("role"), f.get("name"))]
            same.sort(key=lambda e: "focused" not in e.get("states", ()))
            src = same[0] if same else f
            out = {"role": f.get("role", ""), "name": f.get("name", ""), "app": f["app"], "window": f["window"]}
            for k in ("x", "y", "w", "h"):
                out[k] = int(src.get(k) or 0)
            text = f.get("text")
            if text:
                caret = f.get("caret")
                if len(text) > FOCUS_CHARS and isinstance(caret, int):
                    start = max(0, min(caret - FOCUS_CHARS // 2, len(text) - FOCUS_CHARS))
                    text = text[start:start + FOCUS_CHARS]
                out["value"] = text[:FOCUS_CHARS]
            return out
        active = {w["title"] for w in self.all_windows if w["active"]}
        hits = [h for d, h in sorted(self.focus_hits, key=lambda t: (t[1]["window"] not in active, -t[0]))
                if h["role"] not in TOP_ROLES]
        if not hits:
            return None
        h = hits[0]
        out = {k: h[k] for k in ("role", "name", "app", "window", "x", "y", "w", "h")}
        if h.get("value"):
            out["value"] = h["value"]
        return out

    def result(self):
        active = {w["title"] for w in self.all_windows if w["active"]}

        def priority(e):
            p = 0
            if e["window"] in active:
                p -= 100
            if e["role"] in PRIORITY_ROLES:
                p -= 10
            if "focused" in e.get("states", ()):
                p -= 50
            return p

        elements = [] if self.args.focused_only else (
            sorted(list(self.ui), key=priority)[:self.args.max] + list(self.cells)[:self.args.cells])
        return {
            "windows": list(self.windows),
            "elements": elements,
            "focused": self.focused(elements),
            "partial": self.partial,
            "errors": list(self.errors),
            "stats": {"nodes": self.nodes, "cells": len(self.cells),
                      "elapsed_ms": int((time.monotonic() - self.t0) * 1000)},
        }


_printed = threading.Lock()


def emit(obj):
    if not _printed.acquire(blocking=False):
        time.sleep(30)  # the other thread is printing, and the process ends when it is done
        return
    data = (json.dumps(obj) + "\n").encode()
    while data:
        data = data[os.write(1, data):]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=120, help="UI elements to keep, by priority")
    ap.add_argument("--cells", type=int, default=300, help="visible table cells to read")
    ap.add_argument("--window", help="only windows whose title contains SUBSTR")
    ap.add_argument("--focused-only", action="store_true", help="only the window list and the focused element")
    ap.add_argument("--deadline", type=float, default=12.0, help="seconds before printing whatever was read")
    args = ap.parse_args()
    d = Dump(args)

    def hard_stop():
        d.error("deadline %gs: an accessibility call did not return" % args.deadline)
        try:
            out = d.result()
        except Exception as e:
            out = {"windows": [], "elements": [], "focused": None, "partial": True,
                   "errors": d.errors + ["result: %s" % e], "stats": {"nodes": d.nodes, "cells": 0, "elapsed_ms": 0}}
        emit(out)
        os._exit(0)

    timer = threading.Timer(args.deadline, hard_stop)
    timer.daemon = True
    timer.start()
    global pyatspi
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        import pyatspi
        Atspi.set_timeout(CALL_TIMEOUT_MS, 0)
        d.run()
    except BaseException as e:
        d.error("dump: %s: %s" % (type(e).__name__, e))
    timer.cancel()
    try:
        emit(d.result())
    except Exception as e:
        emit({"windows": [], "elements": [], "focused": None, "partial": True, "errors": d.errors + ["result: %s" % e],
              "stats": {"nodes": d.nodes, "cells": 0, "elapsed_ms": int((time.monotonic() - d.t0) * 1000)}})


if __name__ == "__main__":
    main()
