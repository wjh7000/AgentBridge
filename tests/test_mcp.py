import io
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agentbridge.mcp import Server, serve
from agentbridge.store import Store


def request(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def initialized(self, root=None, agent="codex"):
        server = Server(root or self.root, agent)
        result = server.handle(request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}))
        self.assertEqual(result["result"]["protocolVersion"], "2025-11-25")
        return server

    def test_peers_share_but_other_projects_do_not(self):
        writer = self.initialized(agent="claude")
        result = writer.handle(request("tools/call", {"name": "report_progress", "arguments": {"summary": "Implemented login. Tests passed.", "files": ["auth.py"]}}))
        self.assertFalse(result["result"]["isError"])
        reader = self.initialized()
        response = reader.handle(request("tools/call", {"name": "get_context"}))
        self.assertIn("Implemented login", json.dumps(response))
        other = self.root / "separate-project"
        other.mkdir()
        response = self.initialized(other).handle(request("tools/call", {"name": "get_context"}))
        self.assertNotIn("Implemented login", json.dumps(response))

    def test_bound_identity_cannot_be_overridden(self):
        server = self.initialized()
        for injected in ({"project": "/tmp/other"}, {"agent": "claude"}, {"limit": True}):
            result = server.handle(request("tools/call", {"name": "get_context", "arguments": injected}))
            self.assertTrue(result["result"]["isError"])

    def test_protocol_errors_notifications_and_recovery(self):
        server = Server(self.root, "codex")
        self.assertIn("error", server.handle(request("tools/list")))
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "report_progress", "arguments": {"summary": "must not be saved"}}}))
        self.assertEqual(Store(self.root).list_events(), [])
        input_lines = ["not JSON", json.dumps(request("initialize", {"protocolVersion": "2099-01-01"})), json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), json.dumps(request("tools/list", request_id=2)), json.dumps(request("unknown", request_id=3)), json.dumps(request("tools/call", {"name": "get_context", "arguments": []}, 4))]
        output = io.StringIO()
        serve(self.root, "codex", io.StringIO("\n".join(input_lines) + "\n"), output)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(messages), 5)
        self.assertEqual(messages[0]["error"]["code"], -32700)
        self.assertEqual(messages[1]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(len(messages[2]["result"]["tools"]), 3)
        self.assertEqual(messages[3]["error"]["code"], -32601)
        self.assertTrue(messages[4]["result"]["isError"])

    def test_real_stdio_process(self):
        runner = Path(__file__).resolve().parents[1] / "agentbridge.py"
        subprocess.run([sys.executable, str(runner), "init", "--project", str(self.root), "--clients", "claude"], capture_output=True, text=True, check=True)
        payload = "\n".join(json.dumps(x) for x in [request("initialize", {"protocolVersion": "2024-11-05"}), {"jsonrpc": "2.0", "method": "notifications/initialized"}, request("tools/list", request_id=2)]) + "\n"
        process = subprocess.run([sys.executable, str(runner), "serve", "--project", str(self.root), "--agent", "claude"], cwd=self.root, input=payload, capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        frames = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(len(frames[1]["result"]["tools"]), 3)

    def test_copied_config_cannot_read_original_project(self):
        runner = Path(__file__).resolve().parents[1] / "agentbridge.py"
        original = self.root / "project1"
        original.mkdir()
        subprocess.run([sys.executable, str(runner), "init", "--project", str(original), "--clients", "claude", "--with-mcp"], capture_output=True, check=True)
        Store(original).publish("claude", "session1", "Private project one progress")
        clone = self.root / "project2"
        shutil.copytree(original, clone)
        config = json.loads((clone / ".mcp.json").read_text())["mcpServers"]["agentbridge"]
        process = subprocess.run([config["command"]] + config["args"], cwd=clone, input=json.dumps(request("initialize", {"protocolVersion": "2025-11-25"})) + "\n", capture_output=True, text=True, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, "")
        self.assertNotIn("Private project one progress", process.stderr)


if __name__ == "__main__":
    unittest.main()
