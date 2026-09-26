"""Control socket: big replies arrive whole, slow work stays off the loop, busy sessions survive `ls`."""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image

from termdesk import control
from termdesk.session import Session


class FakeRFB:
    name, w, h = "fake", 64, 48

    def __init__(self):
        self.sock, self.peer = socket.socketpair()
        self.fb = Image.new("RGB", (self.w, self.h))
        self.damage, self.resized = [], False
        self.continuous = self.continuous_on = False
        self.clip_ext, self.clipboard = True, None
        self.keys, self.cuts = [], []

    def request(self, incremental=True):
        pass

    def pending(self):
        return False

    def handle_message(self):
        return False

    def key(self, sym, down):
        self.keys.append((sym, down))

    def pointer(self, x, y, mask):
        pass

    def cut_text(self, text):
        self.cuts.append(text)
        return True

    def close(self):
        self.sock.close()
        self.peer.close()


def big_tree(n=600):
    return {"windows": [{"app": "soffice", "title": "fixture.ods", "active": True, "extents": None}],
            "elements": [{"role": "table cell", "name": f"B{i}", "value": f'row {i} "quoted" \\ ' + "x" * 60,
                          "x": i, "y": i, "w": 10, "h": 10, "app": "soffice", "window": "fixture.ods"}
                         for i in range(n)],
            "focused": None, "partial": False, "errors": [], "stats": {}}


class SocketDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")  # short: AF_UNIX paths max out near 104 bytes on macOS
        patcher = mock.patch("termdesk.control.socket_dir", return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def start_session(self, name="t"):
        rfb = FakeRFB()
        self.addCleanup(rfb.close)
        session = Session(rfb, name=name)
        session.sandbox_checked = True
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        self.addCleanup(self.stop_session, name, thread)
        return session

    def stop_session(self, name, thread):
        try:
            control.Client(name, timeout=5).call("quit")
        except (RuntimeError, OSError):
            pass
        thread.join(5)


class ReplySize(SocketDir):
    def test_50kb_state_arrives_whole(self):
        session = self.start_session()
        session.sandbox_name = "fake"
        with mock.patch.object(Session, "_dump_tree", return_value=big_tree()):
            resp = control.Client("t").call("state", full=True)
        self.assertGreater(len(json.dumps(resp)), 50000)
        self.assertEqual(resp["count"], 600)
        self.assertTrue(resp["state"].endswith("@599,599 10x10"))

    def test_screenshot_base64_without_path(self):
        session = self.start_session()
        session.rfb.fb = Image.frombytes("RGB", (200, 150), os.urandom(200 * 150 * 3))
        resp = control.Client("t").call("screenshot")
        self.assertGreater(len(resp["png_base64"]), 50000)
        self.assertEqual((resp["width"], resp["height"]), (200, 150))

    def test_slow_reader_does_not_stall_the_loop(self):
        session = self.start_session()
        session.rfb.fb = Image.frombytes("RGB", (300, 200), os.urandom(300 * 200 * 3))
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        slow.connect(os.path.join(self.dir, "t.sock"))
        slow.sendall(b'{"cmd":"screenshot"}\n')
        time.sleep(0.3)
        t0 = time.time()
        info = control.Client("t", timeout=5).call("info")
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual(info["target"], "fake")
        data = b""
        while not data.endswith(b"\n"):
            chunk = slow.recv(1 << 16)
            if not chunk:
                break
            data += chunk
        slow.close()
        self.assertGreater(len(data), 200000)
        self.assertTrue(json.loads(data)["ok"])

    def test_info_answers_while_a_dump_runs(self):
        session = self.start_session()
        session.sandbox_name = "fake"

        def slow_dump(*args):
            time.sleep(1.5)
            return big_tree(3)

        with mock.patch.object(Session, "_dump_tree", side_effect=slow_dump):
            got = {}
            t = threading.Thread(target=lambda: got.update(control.Client("t").call("state", full=True)))
            t.start()
            time.sleep(0.2)
            t0 = time.time()
            control.Client("t", timeout=5).call("info")
            self.assertLess(time.time() - t0, 0.5)
            self.assertEqual([s.get("busy") for s in control.list_sessions()], [None])
            t.join(5)
        self.assertEqual(got["count"], 3)

    def test_no_tree_error_is_permanent(self):
        self.start_session()
        with self.assertRaises(control.SessionError) as cm:
            control.Client("t").call("state")
        self.assertTrue(cm.exception.permanent)
        self.assertIn("not attached to a termdesk sandbox", str(cm.exception))


class ClientErrors(SocketDir):
    def serve_once(self, reply):
        path = os.path.join(self.dir, "fake.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)

        def run():
            conn, _ = srv.accept()
            conn.recv(1024)
            conn.sendall(reply)
            conn.close()
            srv.close()

        threading.Thread(target=run, daemon=True).start()
        return control.Client("fake", timeout=5)

    def test_cut_off_reply(self):
        with self.assertRaisesRegex(RuntimeError, "session reply was cut off after 8192 bytes"):
            self.serve_once(b'{"ok": true, "state": "' + b"x" * 8169).call("state")

    def test_invalid_json_reply(self):
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            self.serve_once(b"{nope}\n").call("info")


class ListSessions(SocketDir):
    def test_dead_socket_is_removed(self):
        path = os.path.join(self.dir, "dead.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        s.close()
        self.assertEqual(control.list_sessions(), [])
        self.assertFalse(os.path.exists(path))

    def test_busy_socket_is_kept_and_resolvable(self):
        path = os.path.join(self.dir, "busy.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        s.listen(8)  # listening but never answering, like a session stuck in a long call
        self.addCleanup(s.close)
        self.assertEqual(control.list_sessions(), [{"ok": True, "busy": True, "name": "busy"}])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(control.resolve("busy"), path)
        self.assertEqual(control.resolve(), path)

    def test_live_session_is_listed(self):
        self.start_session("live")
        [info] = control.list_sessions()
        self.assertEqual((info["name"], info["target"], info.get("busy")), ("live", "fake", None))


if __name__ == "__main__":
    unittest.main()
