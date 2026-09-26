"""MCP server: framing, version negotiation and tool dispatch, without a sandbox."""
import io
import json
import unittest
from unittest import mock

from termdesk import mcp


def run(*messages):
    raw = b"".join((m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n" for m in messages)
    out = io.StringIO()
    mcp.serve(io.BytesIO(raw), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def request(mid, method, params=None):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class Protocol(unittest.TestCase):
    def test_initialize_negotiates_the_version(self):
        known, unknown = run(request(1, "initialize", {"protocolVersion": "2025-03-26"}),
                             request(2, "initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(known["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(unknown["result"]["protocolVersion"], mcp.PROTOCOLS[0])
        self.assertIn("tools", known["result"]["capabilities"])
        self.assertIn("state", known["result"]["instructions"])

    def test_notifications_get_no_reply(self):
        self.assertEqual(run({"jsonrpc": "2.0", "method": "notifications/initialized"}), [])

    def test_errors(self):
        replies = run(b"{not json", request(3, "nope"), request(4, "tools/call", {"name": "nope"}),
                      request(5, "tools/list", [1]))
        self.assertEqual([r["error"]["code"] for r in replies], [-32700, -32601, -32602, -32602])
        self.assertEqual([r["id"] for r in replies], [None, 3, 4, 5])

    def test_batch(self):
        [batch] = run([request(1, "ping"), {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(batch, [{"jsonrpc": "2.0", "id": 1, "result": {}}])

    def test_tool_schemas(self):
        [r] = run(request(1, "tools/list"))
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertLessEqual({"sandbox_up", "window", "state", "click", "type", "screenshot", "done"}, set(tools))
        for t in tools.values():
            schema = t["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertLessEqual(set(schema["required"]), set(schema["properties"]))


class Tools(unittest.TestCase):
    def call(self, name, args, reply=None):
        client = mock.Mock()
        client.call.return_value = reply or {"ok": True}
        with mock.patch("termdesk.control.Client", return_value=client):
            [r] = run(request(1, "tools/call", {"name": name, "arguments": args}))
        return r["result"], client

    def test_errors_are_tool_results(self):
        res, client = self.call("click", {})
        self.assertTrue(res["isError"])
        self.assertIn("index", res["content"][0]["text"])
        client.call.assert_not_called()

    def test_state_maps_onto_a_control_request(self):
        res, client = self.call("state", {"full": True, "window": "Calc"}, {"ok": True, "state": "windows: none"})
        client.call.assert_called_once_with("state", full=True, window="Calc")
        self.assertEqual(res["content"], [{"type": "text", "text": "windows: none"}])

    def test_type_reply_reads_like_the_cli(self):
        res, client = self.call("type", {"text": "héllo"}, {"ok": True, "via": "paste", "verified": False,
                                                            "want": "héllo", "got": "hllo"})
        client.call.assert_called_once_with("type", text="héllo")
        self.assertEqual(res["content"][0]["text"], 'via=paste  verified=False  want="héllo"  got="hllo"')

    def test_window_reopens_the_session_by_its_address(self):
        client = mock.Mock(path="/state/work.sock")
        client.call.return_value = {"ok": True, "addr": "localhost:5901", "target": "termdesk"}
        with mock.patch("termdesk.control.Client", return_value=client), \
                mock.patch("termdesk.cli.open_window", return_value="opened work") as opened:
            [r] = run(request(1, "tools/call", {"name": "window", "arguments": {"session": "work"}}))
        opened.assert_called_once_with("localhost:5901", "work")
        self.assertEqual(r["result"]["content"][0]["text"], "opened work")

    def test_screenshot_is_image_content(self):
        res, _ = self.call("screenshot", {"scale": 0.5},
                           {"ok": True, "png_base64": "iVBOR", "width": 640, "height": 400, "scale": 0.5})
        self.assertEqual(res["content"][0], {"type": "image", "data": "iVBOR", "mimeType": "image/png"})


if __name__ == "__main__":
    unittest.main()
