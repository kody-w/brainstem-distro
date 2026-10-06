#!/usr/bin/env python3
"""The plugin over stdio against a stand-in engine: MCP handshake, the app card, the page's calls, chat.
No network for the engine; the kernel page is read from the cache or fetched once at the pinned commit."""
import json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin")


class Engine(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "version": "stand-in", "model": "m", "agents": [], "quarantined": []})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/chat":
            d = json.loads(raw)
            return self._send(200, {"response": "echo: " + d["user_input"], "session_id": "s-12345678", "agent_logs": ""})
        self._send(404, {"error": "not found"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
threading.Thread(target=srv.serve_forever, daemon=True).start()
agents = tempfile.mkdtemp()
cfg = json.load(open(os.path.join(ROOT, "distro.json")))
cfg.update(labels=[["RAPP Brainstem", "Test Distro"]], engine={"url": f"http://127.0.0.1:{srv.server_port}"}, agents_dir=agents)
work = tempfile.mkdtemp()
for name in os.listdir(ROOT):
    src = os.path.join(ROOT, name)
    os.symlink(src, os.path.join(work, name)) if name != "distro.json" else None
json.dump(cfg, open(os.path.join(work, "distro.json"), "w"))
p = subprocess.Popen([sys.executable, os.path.join(work, "server.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                     env={**os.environ, "DISTRO_CACHE": tempfile.mkdtemp()})
n = 0


def rpc(method, params=None):
    global n
    n += 1
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}}) + "\n"); p.stdin.flush()
    return json.loads(p.stdout.readline())


def tool(name, args):
    return rpc("tools/call", {"name": name, "arguments": args})["result"]


init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})["result"]
tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
page = rpc("resources/read", {"uri": tools["open"]["_meta"]["ui"]["resourceUri"]})["result"]["contents"][0]
http_health = tool("http", {"path": "/health"})["structuredContent"]
imported = tool("http", {"method": "POST", "path": "/agents/import", "file": {"name": "hello_agent.py", "text": "class HelloAgent(BasicAgent):\n    pass\n"}})["structuredContent"]
landed = os.path.exists(os.path.join(agents, "hello_agent.py"))
bad_name = tool("http", {"method": "POST", "path": "/agents/import", "file": {"name": "../x.py", "text": ""}})["structuredContent"]
listed = json.loads(tool("http", {"path": "/agents"})["structuredContent"]["body"])
deleted = tool("http", {"method": "DELETE", "path": "/agents/hello_agent.py"})["structuredContent"]
escape = tool("http", {"path": "/../etc/passwd"})["structuredContent"]
chat = tool("chat", {"message": "hi"})
good_agent = "from agents.basic_agent import BasicAgent\n\nclass ShoutAgent(BasicAgent):\n    pass\n"
added = tool("add_agent", {"filename": "shout_agent.py", "code": good_agent})
bad_add = tool("add_agent", {"filename": "Shout.py", "code": good_agent})
p.stdin.close(); p.wait(timeout=10)

checks = {
    "initialize names the distro": init["serverInfo"]["name"] == cfg["id"],
    "open carries the app card": tools["open"]["_meta"]["ui"]["resourceUri"].startswith("ui://"),
    "http is for the app only": tools["http"]["_meta"]["ui"]["visibility"] == ["app"],
    "page is an MCP App": page["mimeType"] == "text/html;profile=mcp-app",
    "page is the kernel's, bridge in front": "window.__distroReady" in page["text"] and 'id="input"' in page["text"],
    "labels ride along with the page": '"Test Distro"' in page["text"] and "window.__distro = " in page["text"],
    "page calls reach the engine": http_health["status"] == 200 and json.loads(http_health["body"])["status"] == "ok",
    "agent import lands in the agents folder": imported["status"] == 200 and landed,
    "agent delete removes it": deleted["status"] == 200 and not os.path.exists(os.path.join(agents, "hello_agent.py")),
    "only plain .py agent names are accepted": bad_name["status"] == 400,
    "agent list shows the agent's class": listed["files"] == [{"filename": "hello_agent.py", "agents": ["HelloAgent"]}],
    "paths outside the engine are refused": escape["status"] == 400,
    "add_agent installs a new agent": not added.get("isError") and os.path.exists(os.path.join(agents, "shout_agent.py")),
    "add_agent refuses a bad filename": bad_add.get("isError") is True,
    "add_agent is visible to the model": "add_agent" in tools and "ui" not in tools["add_agent"].get("_meta", {}),
    "chat answers from the engine": chat["structuredContent"]["response"] == "echo: [Assistant] hi",
}
for k, v in checks.items():
    print(("PASS " if v else "FAIL ") + k)
sys.exit(0 if all(checks.values()) else 1)
