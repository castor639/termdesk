"""Action parsing, keymap routing, state rendering, typed-text checks, wait-for matching, RFB clipboard."""
import struct
import unittest
import zlib
from unittest import mock

from termdesk import rfb as rfbmod
from termdesk.cli import _state_matches, parse_action
from termdesk.keys import KEY_NAMES, off_keymap, parse_combo
from termdesk.session import Session, _check_typed

from test_control import FakeRFB


class ParseAction(unittest.TestCase):
    def test_state_flags(self):
        self.assertEqual(parse_action(["state", "--window", "Save As", "--focused", "--cells", "50", "--full"]),
                         {"cmd": "state", "full": True, "focused": True, "window": "Save As", "cells": 50})
        self.assertEqual(parse_action(["state"]), {"cmd": "state", "full": False})
        with self.assertRaises(ValueError):
            parse_action(["state", "--bogus"])

    def test_type_flags(self):
        self.assertEqual(parse_action(["type", "--paste", "Zoë", "李"]), {"cmd": "type", "text": "Zoë 李", "mode": "paste"})
        self.assertEqual(parse_action(["type", "hi", "--keys"]), {"cmd": "type", "text": "hi", "mode": "keys"})
        self.assertEqual(parse_action(["type", "--delay-ms", "20", "abc"]), {"cmd": "type", "text": "abc", "delay_ms": 20.0})
        self.assertEqual(parse_action(["type", "--", "--keys", "x"]), {"cmd": "type", "text": "--keys x"})
        with self.assertRaises(ValueError):
            parse_action(["type", "--paste", "--keys", "x"])

    def test_set_value_paste_clipboard(self):
        self.assertEqual(parse_action(["set-value", "--paste", "7", "a", "b"]),
                         {"cmd": "set_value", "index": 7, "text": "a b", "mode": "paste"})
        self.assertEqual(parse_action(["paste", "C:\\x", "--", "--y"]), {"cmd": "paste", "text": "C:\\x --y"})
        self.assertEqual(parse_action(["clipboard"]), {"cmd": "clipboard"})


class Keymap(unittest.TestCase):
    def test_off_keymap(self):
        self.assertFalse(off_keymap('Plain ASCII ~!@#$%^&*()_+{}|:"<>? 0-9\tC:\\x\\y\n'))
        for text in ("Zoë", "李", "“Q”", "don’t", "😀", "é", "\u00a0"):
            self.assertTrue(off_keymap(text), text)

    def test_shift_combos_ask_for_the_shifted_keysym(self):
        shift, ctrl = parse_combo("shift")[1], parse_combo("ctrl")[1]
        self.assertEqual(parse_combo("ctrl+shift+t"), ([ctrl, shift], ord("T")))
        self.assertEqual(parse_combo("shift+4"), ([shift], ord("$")))
        self.assertEqual(parse_combo("shift+/"), ([shift], ord("?")))
        self.assertEqual(parse_combo("ctrl+t"), ([ctrl], ord("t")))
        self.assertEqual(parse_combo("shift+Tab"), ([shift], 0xFF09))


class WaitForMatching(unittest.TestCase):
    STATE = "\n".join([
        'windows: soffice: "fixture.ods - LibreOffice Calc"; soffice: "Save Document?" (active)',
        "focused: [327] push button \"Don\u2019t Save\"",
        "== Save Document? (active)",
        "[327] push button \"Don\u2019t Save\" @500,400 90x30",
        "[328] label \"Your changes will be lost if you don\u2019t save them.\" @400,300 300x20",
        "== fixture.ods - LibreOffice Calc",
        '[40] table cell "B6" value="Dwayne \\"The Rock\\" Johnson" @66,274 194x17',
        '[41] table cell "G5" value="\\\\\\\\server\\\\share" @66,274 194x17',
    ])

    def test_typographic_apostrophe_and_case(self):
        self.assertEqual(_state_matches(self.STATE, "don't save"), [
            "[327] push button \"Don\u2019t Save\" @500,400 90x30",
            "[328] label \"Your changes will be lost if you don\u2019t save them.\" @400,300 300x20"])
        self.assertEqual(_state_matches(self.STATE, "DON\u2019T SAVE")[0], "[327] push button \"Don\u2019t Save\" @500,400 90x30")

    def test_window_titles(self):
        self.assertEqual(_state_matches(self.STATE, "save document?"), ["== Save Document? (active)"])
        self.assertEqual(_state_matches(self.STATE, "libreoffice calc"), ["== fixture.ods - LibreOffice Calc"])
        state = 'windows: xfce4-terminal: "Terminal - guest@box" (active)\nfocused: none'
        self.assertEqual(_state_matches(state, "guest@box"), [state.split("\n")[0]])

    def test_json_escapes_in_values(self):
        self.assertEqual(len(_state_matches(self.STATE, 'Dwayne "The Rock"')), 1)
        self.assertEqual(len(_state_matches(self.STATE, "\\\\server\\share")), 1)
        self.assertEqual(_state_matches(self.STATE, "nothing like this"), [])


def fake_session(test):
    rfb = FakeRFB()
    test.addCleanup(rfb.close)
    s = Session(rfb)
    s.sandbox_checked = True
    return s


class RenderState(unittest.TestCase):
    def session(self):
        s = fake_session(self)
        s.sandbox_name = "fake"
        return s

    @staticmethod
    def tree(elements, focused=None, partial=False, errors=()):
        return {"windows": [{"app": "mousepad", "title": "Untitled 1", "active": True, "extents": [0, 0, 800, 600]}],
                "elements": elements, "focused": focused, "partial": partial, "errors": list(errors)}

    @staticmethod
    def elem(name, value=None, app="mousepad", x=10, **kw):
        e = {"role": "text", "name": name, "x": x, "y": 20, "w": 100, "h": 20, "app": app, "window": "Untitled 1"}
        if value is not None:
            e["value"] = value
        e.update(kw)
        return e

    def test_json_rendering_and_focus_line(self):
        s = self.session()
        body = self.elem("say \"hi\"", 'a "b" C:\\x\nnext\u2028line', formula="=D2*(1+E2)", states=["focused", "editable"])
        out = s._render_state(self.tree([body], focused=body), full=True, scoped=False)["state"].split("\n")
        self.assertEqual(out[0], 'windows: mousepad: "Untitled 1" (active)')
        self.assertEqual(out[1], 'focused: [1] text "say \\"hi\\"" value="a \\"b\\" C:\\\\x\\nnext\\u2028line"')
        self.assertEqual(out[2], "== Untitled 1 (active)")
        self.assertEqual(out[3], '[1] text "say \\"hi\\"" value="a \\"b\\" C:\\\\x\\nnext\\u2028line" '
                                 'formula="=D2*(1+E2)" @10,20 100x20 [focused, editable]')
        self.assertEqual(len(out), 4)
        s2 = self.session()
        out = s2._render_state(self.tree([]), full=True, scoped=False)["state"].split("\n")
        self.assertEqual(out[1], "focused: none")

    def test_partial_diff_keeps_skipped_apps(self):
        s = self.session()
        a, b = self.elem("a"), self.elem("b", app="soffice", x=50)
        s._render_state(self.tree([a, b]), full=False, scoped=False)
        resp = s._render_state(self.tree([a], partial=True, errors=["soffice: TimeoutError: skipped"]),
                               full=False, scoped=False)
        self.assertEqual(resp["state"].split("\n")[2:], ["note: partial tree: soffice: TimeoutError: skipped",
                                                         "no change since last state"])
        self.assertTrue(resp["partial"])
        resp = s._render_state(self.tree([a]), full=False, scoped=False)
        self.assertIn('- [2] text "b" @50,20 100x20', resp["state"])

    def test_scoped_state_keeps_baseline_and_indices(self):
        s = self.session()
        a, b = self.elem("a"), self.elem("b", x=50)
        s._render_state(self.tree([a, b]), full=False, scoped=False)
        out = s._render_state(self.tree([b]), full=False, scoped=True)
        self.assertTrue(out["state"].endswith('[2] text "b" @50,20 100x20'))
        self.assertEqual(sorted(s.elements), [1, 2])
        self.assertIn("no change", s._render_state(self.tree([a, b]), full=False, scoped=False)["state"])


class TypedText(unittest.TestCase):
    def test_check_typed(self):
        self.assertEqual(_check_typed("Zoë 李", "Name: Zoë 李 here"), (True, "Zoë 李", None, None))
        self.assertEqual(_check_typed("Zoë 李", "Zo 李")[:3], (False, "Zoë 李", 2))
        verified, want, bad, note = _check_typed('Zoë 李 "Q", Ltd\tC:\\x\\y', "C:\\x\\y")
        self.assertEqual((verified, want, bad), (True, "C:\\x\\y", None))
        self.assertIn("last tab or newline", note)
        self.assertEqual(_check_typed("query\n", "")[0], None)
        self.assertIsNone(_check_typed("abc", "x" * 4000)[0])

    def test_type_routes_off_keymap_text_through_clipboard(self):
        s = fake_session(self)
        with mock.patch("termdesk.session.PASTE_SETTLE", 0):
            resp = s._type({"text": "Zoë 李\tabc"})
        self.assertEqual((resp["via"], resp["verified"]), ("paste", None))
        self.assertEqual(s.rfb.cuts, ["Zoë 李", "abc"])
        downs = [sym for sym, down in s.rfb.keys if down]
        ctrl, v, tab = KEY_NAMES["ctrl"], ord("v"), KEY_NAMES["tab"]
        self.assertEqual(downs, [ctrl, v, tab, ctrl, v])

    def test_type_keeps_keysyms_on_the_keymap(self):
        s = fake_session(self)
        resp = s._type({"text": 'C:\\x "Q"'})
        self.assertEqual(resp["via"], "keys")
        self.assertEqual(s.rfb.cuts, [])
        self.assertEqual("".join(chr(sym) for sym, down in s.rfb.keys if down), 'C:\\x "Q"')
        s._type({"text": "é", "mode": "keys"})
        self.assertEqual(s.rfb.cuts, [])


class ExtendedClipboard(unittest.TestCase):
    def fake(self):
        r = rfbmod.RFB.__new__(rfbmod.RFB)
        r.sent = []
        r.send = r.sent.append
        r.clip_ext, r.clipboard, r.own_clip = False, None, None
        return r

    def test_server_caps_then_utf8_paste(self):
        r = self.fake()
        self.assertFalse(r.cut_text("李"))  # legacy Latin-1 before the server says it has the extension
        self.assertEqual(r.clipboard, "?")
        r._server_clip(struct.pack(">II", rfbmod.CLIP_CAPS | rfbmod.CLIP_UTF8, 1 << 20))
        self.assertTrue(r.clip_ext)
        r.sent.clear()
        self.assertTrue(r.cut_text("Zoë 李\n"))
        self.assertEqual(r.clipboard, "Zoë 李\n")
        notify, provide = r.sent
        self.assertEqual(notify, struct.pack(">BxxxiI", 6, -4, rfbmod.CLIP_NOTIFY | rfbmod.CLIP_UTF8))
        _, n, flags = struct.unpack(">BxxxiI", provide[:12])
        self.assertEqual((n, flags), (-(len(provide) - 8), rfbmod.CLIP_PROVIDE | rfbmod.CLIP_UTF8))
        data = zlib.decompress(provide[12:])
        self.assertEqual(data[4:], "Zoë 李\r\n".encode() + b"\0")
        r._server_clip(struct.pack(">I", rfbmod.CLIP_NOTIFY))  # Xvnc sends this after taking the selection
        self.assertEqual(r.clipboard, "Zoë 李\n")

    def test_server_provide_is_read(self):
        r = self.fake()
        r._server_clip(struct.pack(">I", rfbmod.CLIP_NOTIFY | rfbmod.CLIP_UTF8))
        self.assertEqual(r.sent, [struct.pack(">BxxxiI", 6, -4, rfbmod.CLIP_REQUEST | rfbmod.CLIP_UTF8)])
        text = "“Q” 李\r\nline 2".encode() + b"\0"
        r._server_clip(struct.pack(">I", rfbmod.CLIP_PROVIDE | rfbmod.CLIP_UTF8)
                       + zlib.compress(struct.pack(">I", len(text)) + text))
        self.assertEqual(r.clipboard, "“Q” 李\nline 2")


if __name__ == "__main__":
    unittest.main()
