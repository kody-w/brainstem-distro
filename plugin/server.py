#!/usr/bin/env python3
"""Run a Brainstem distro wherever an AI tool runs, with the kernel's own chat page as the app.

Speaks MCP over stdio (standard library only). On first use it starts the distro's engine on a free
local port (or uses one already running), and serves the kernel's unchanged index.html, fetched at the
commit pinned in kernel.json and checked against its git blob hash, as an MCP App. A small script in
front of the page reroutes the page's same-origin calls (/chat, /health, /agents ...) through the
`http` tool, which only the app can call, to the engine.

    python3 server.py           MCP over stdio (what the AI tool runs)
    python3 server.py --check   start the engine, fetch and verify the page, print a JSON report
"""
import hashlib, json, os, re, socket, subprocess, sys, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DISTRO = json.load(open(os.path.join(HERE, "distro.json")))
KERNEL = json.load(open(os.path.join(HERE, "kernel.json")))
CACHE = os.path.expanduser(os.environ.get("DISTRO_CACHE") or f"~/.cache/brainstem-distro/{DISTRO['id']}")
UI_URI = f"ui://{DISTRO['id']}/chat.html"
UI_MIME = "text/html;profile=mcp-app"
VERSION = DISTRO.get("version", "0.1.0")

_engine = {"url": None, "proc": None}
_lock = threading.Lock()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------- the engine
def _healthy(url):
    try:
        with urllib.request.urlopen(url + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def engine():
    """The engine's base URL, starting it the first time it is needed."""
    with _lock:
        if _engine["url"] and (_engine["proc"].poll() is None if _engine["proc"] else _healthy(_engine["url"])):
            return _engine["url"]
        _engine.update(url=None, proc=None)
        eng = DISTRO["engine"]
        url = os.environ.get("DISTRO_ENGINE_URL") or eng.get("url")
        if url and _healthy(url.rstrip("/")):
            _engine["url"] = url.rstrip("/")
            return _engine["url"]
        shared = os.path.join(CACHE, "engine.json")  # an engine another AI tool already started
        try:
            url = json.load(open(shared))["url"]
            if _healthy(url):
                _engine["url"] = url
                return url
        except Exception:
            pass
        if not eng.get("command"):
            raise RuntimeError(f"No engine answering at {url}. {eng.get('install_hint', '')}".strip())
        port = _free_port()
        cmd = [sys.executable if c == "{python}" else c.replace("{port}", str(port)).replace("{root}", HERE) for c in eng["command"]]
        env = {**os.environ, **{k: v.replace("{port}", str(port)) for k, v in eng.get("env", {}).items()}}
        os.makedirs(CACHE, exist_ok=True)
        out = open(os.path.join(CACHE, "engine.log"), "ab")
        proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=out, stderr=out, stdin=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError(f"The engine stopped while starting; see {CACHE}/engine.log")
            if _healthy(url):
                break
            time.sleep(0.1)
        else:
            proc.terminate()
            raise RuntimeError(f"The engine did not answer in 10 seconds; see {CACHE}/engine.log")
        _engine.update(url=url, proc=proc)
        json.dump({"url": url, "pid": proc.pid}, open(shared, "w"))
        return url


def call_engine(method, path, body=None, timeout=600, file=None):
    data = body.encode() if isinstance(body, str) else body
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if file:  # the page uploads agent files as a form; send them on to the engine the same way
        boundary = "distro" + os.urandom(12).hex()
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file.get("name", "")}"\r\n'
                f"Content-Type: text/x-python\r\n\r\n{file.get('text', '')}\r\n--{boundary}--\r\n").encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(engine() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------- one conversation shared by everyone: the person in the window, and the AI tool through `chat`
THREAD_MAX = 60
_thread_lock = threading.Lock()


class _locked:
    """Held across processes too: every AI tool on this machine runs its own copy of this server, sharing one conversation."""
    def __enter__(self):
        _thread_lock.acquire()
        os.makedirs(CACHE, exist_ok=True)
        self.f = open(os.path.join(CACHE, "thread.lock"), "a")
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except ImportError:  # Windows: msvcrt locks a byte range
            import msvcrt
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *a):
        self.f.close()  # closing releases the lock
        _thread_lock.release()
_host = {"name": "Assistant", "seen": 0}


def _thread_path():
    return os.path.join(CACHE, "thread.json")


def load_thread():
    try:
        return json.load(open(_thread_path()))
    except Exception:
        return {"session_id": "thread-" + os.urandom(8).hex(), "turns": [], "next": 1}


def save_thread(t):
    os.makedirs(CACHE, exist_ok=True)
    t["turns"] = t["turns"][-THREAD_MAX:]
    tmp = _thread_path() + ".tmp"
    json.dump(t, open(tmp, "w"))
    os.replace(tmp, _thread_path())


def say(who, text):
    """One turn in the shared conversation: send it to the engine's /chat with the whole shared history."""
    with _locked():
        t = load_thread()
    history = []
    for turn in t["turns"]:
        history.append({"role": "user", "content": turn["text"] if turn["who"] == "window" else f"[{turn['who']}] {turn['text']}"})
        history.append({"role": "assistant", "content": turn["reply"]})
    user_input = text if who == "window" else f"[{who}] {text}"
    status, body = call_engine("POST", "/chat", json.dumps({"user_input": user_input, "conversation_history": history, "session_id": t["session_id"]}))
    if status == 200:
        d = json.loads(body)
        with _locked():
            t = load_thread()
            t["turns"].append({"n": t["next"], "who": who, "text": text, "reply": d.get("response", ""), "agent_logs": d.get("agent_logs") or ""})
            t["next"] += 1
            save_thread(t)
    return status, body


def turns_after(n):
    with _locked():
        return [x for x in load_thread()["turns"] if x["n"] > n]


# ---------- agents, for engines that keep only /chat and /health
def agents_dir():
    d = DISTRO.get("agents_dir")
    return os.path.expanduser(d) if d else None


def local_agents(method, path, body, file):
    d = agents_dir()
    if method == "GET" and path == "/agents":
        files = []
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if name.endswith(".py"):
                src = open(os.path.join(d, name), encoding="utf-8", errors="replace").read()
                files.append({"filename": name, "agents": re.findall(r"^class\s+(\w+)\(", src, re.M)})
        return 200, {"files": files}
    if method == "POST" and path == "/agents/import":
        if not file or not re.fullmatch(r"[\w.-]+\.py", file.get("name", "")):
            return 400, {"status": "error", "error": "Choose a single .py agent file."}
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, file["name"]), "w", encoding="utf-8").write(file.get("text", ""))
        return 200, {"status": "ok", "filename": file["name"]}
    m = re.fullmatch(r"/agents/([\w.-]+\.py)", path)
    if method == "DELETE" and m:
        p = os.path.join(d, m.group(1))
        if os.path.isfile(p):
            os.remove(p)
            return 200, {"status": "ok"}
        return 404, {"status": "error", "error": "No such agent."}
    return None


def http_tool(args):
    method, path = (args.get("method") or "GET").upper(), args.get("path") or "/"
    if not path.startswith("/") or ".." in path:
        return 400, json.dumps({"error": "bad path"})
    if method == "POST" and path == "/chat":
        try:
            text = json.loads(args.get("body") or "{}").get("user_input", "")
        except ValueError:
            text = ""
        if not isinstance(text, str) or not text.strip():
            return 400, json.dumps({"error": "user_input is required"})
        return say("window", text)
    if method == "GET" and path.startswith("/distro/thread"):
        m = re.search(r"after=(\d+)", path)
        return 200, json.dumps({"turns": turns_after(int(m.group(1)) if m else 0)})
    file = args.get("file")
    if file and not re.fullmatch(r"[\w.-]+\.py", str(file.get("name", ""))):
        return 400, json.dumps({"status": "error", "error": "Choose a single .py agent file."})
    status, text = call_engine(method, path, args.get("body"), file=file)
    if status in (404, 405, 501) and agents_dir() and path.split("?")[0].startswith("/agents"):
        local = local_agents(method, path.split("?")[0], args.get("body"), args.get("file"))
        if local:
            return local[0], json.dumps(local[1])
    return status, text


# ---------- the page: the kernel's index.html at the pinned commit, unchanged, plus the bridge in front
def kernel_page():
    sha, path, blob = KERNEL["sha"], KERNEL["ui_path"], KERNEL["ui_blob"]
    cached = os.path.join(CACHE, f"index-{blob}.html")
    if os.path.exists(cached):
        raw = open(cached, "rb").read()
    else:
        url = f"https://raw.githubusercontent.com/{KERNEL['kernel']}/{sha}/{path}"
        with urllib.request.urlopen(url, timeout=60) as r:
            raw = r.read()
    got = hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()
    if got != blob:
        raise RuntimeError(f"The kernel page at {sha[:7]} does not match its pinned hash; refusing to serve it.")
    if not os.path.exists(cached):
        os.makedirs(CACHE, exist_ok=True)
        open(cached, "wb").write(raw)
    return raw.decode("utf-8")


def app_html():
    page = kernel_page()
    bridge = open(os.path.join(HERE, "bridge.js"), encoding="utf-8").read()
    client = open(os.path.join(HERE, "vendor", "mcp-apps.js"), encoding="utf-8").read()
    demo = []
    if DISTRO.get("demo") and os.path.exists(os.path.join(HERE, DISTRO["demo"])):
        demo = json.load(open(os.path.join(HERE, DISTRO["demo"]))).get("steps", [])
    page_cfg = json.dumps({"name": DISTRO["display_name"], "labels": DISTRO.get("labels", []), "demo": demo}).replace("</", "<\\/")
    inject = (f"<script>window.__distro = {page_cfg};\n{bridge}</script>\n"
              f"<script type=\"module\">{client}\n"
              f"const app = new window.__McpApps.App({{name: {json.dumps(DISTRO['id'])}, version: {json.dumps(VERSION)}}}, {{}}, {{autoResize: false}});\n"
              f"app.connect().then(() => {{ app.sendSizeChanged({{height: {DISTRO.get('height', 680)}}}); window.__distroReady(app); }}, e => window.__distroFailed(e));</script>\n")
    head = re.search(r"<head[^>]*>", page, re.I)
    return page[:head.end()] + "\n" + inject + page[head.end():] if head else inject + page


# ---------- MCP
def tools():
    ui = {"resourceUri": UI_URI}
    name = DISTRO["display_name"]
    return [
        {"name": "open", "title": f"Open {name}",
         "description": f"Open the {name} chat window, where the user can talk to their agents directly.",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": True},
         "_meta": {"ui": ui, "ui/resourceUri": UI_URI, "openai/outputTemplate": UI_URI}},
        {"name": "chat", "title": f"Talk to {name}",
         "description": (f"Say something to {name}, the user's own AI, in the one conversation the user, you and {name} share. "
                         f"The user talks to {name} in its window; you talk to it here; everyone sees the whole conversation. "
                         f"The answer also includes anything the user said in the window since you last spoke."),
         "inputSchema": {"type": "object", "required": ["message"], "properties": {"message": {"type": "string"}}}},
        {"name": "conversation", "title": f"Read the conversation with {name}",
         "description": f"Read the latest turns of the shared conversation between the user, you and {name}.",
         "inputSchema": {"type": "object", "properties": {"last": {"type": "integer", "description": "how many turns (default 10)"}}},
         "annotations": {"readOnlyHint": True}},
        {"name": "add_agent", "title": f"Teach {name} a new skill",
         "description": (f"Install a new agent into {name}; it is usable right away, in the chat window and through the chat tool. "
                         "Write a complete single-file Python agent: `from agents.basic_agent import BasicAgent`, one class that subclasses "
                         "BasicAgent, whose __init__ calls super().__init__(name=<Name>, metadata={'name': <Name>, 'description': <when to use it>, "
                         "'parameters': <JSON schema>}), and whose perform(self, **kwargs) returns a string. Standard library only. "
                         "If it fails to load, the error comes back: fix the code and call again with the same filename."),
         "inputSchema": {"type": "object", "required": ["filename", "code"], "properties": {
             "filename": {"type": "string", "description": "snake_case name ending in _agent.py, e.g. expense_report_agent.py"},
             "code": {"type": "string", "description": "the whole agent file"}}}},
        {"name": "http", "title": "Chat window connection",
         "description": "Used only by the chat window to reach its engine.",
         "inputSchema": {"type": "object", "required": ["path"], "properties": {
             "method": {"type": "string"}, "path": {"type": "string"}, "body": {"type": "string"},
             "file": {"type": "object", "properties": {"name": {"type": "string"}, "text": {"type": "string"}}}}},
         "_meta": {"ui": {"visibility": ["app"]}}},
    ]


def call_tool(name, args):
    if name == "open":
        engine()
        return {"content": [{"type": "text", "text": f"{DISTRO['display_name']} is open."}]}
    if name == "chat":
        message = args.get("message", "")
        if not message.strip():
            return {"content": [{"type": "text", "text": "message is required"}], "isError": True}
        missed = [x for x in turns_after(_host["seen"]) if x["who"] == "window"]
        status, text = say(_host["name"], message)
        try:
            d = json.loads(text)
        except ValueError:
            d = {"error": text[:500]}
        if status != 200:
            return {"content": [{"type": "text", "text": d.get("error") or f"The engine answered {status}."}], "isError": True}
        latest = turns_after(0)
        _host["seen"] = latest[-1]["n"] if latest else 0
        out = d.get("response", "")
        if missed:
            out = ("Since you last spoke, the user said in the window:\n" +
                   "\n".join(f"- user: {x['text'][:400]}\n  {DISTRO['display_name']}: {x['reply'][:600]}" for x in missed) +
                   f"\n\n{DISTRO['display_name']} now answers you:\n" + out)
        return {"content": [{"type": "text", "text": out}],
                "structuredContent": {"response": d.get("response", ""), "user_in_window": [{"text": x["text"], "reply": x["reply"]} for x in missed]}}
    if name == "conversation":
        turns = turns_after(0)[-int(args.get("last") or 10):]
        return {"content": [{"type": "text", "text": "\n".join(f"- {('user' if x['who'] == 'window' else x['who'])}: {x['text'][:400]}\n  {DISTRO['display_name']}: {x['reply'][:600]}" for x in turns) or "No conversation yet."}]}
    if name == "add_agent":
        fname = args.get("filename", "")
        if not re.fullmatch(r"[a-z0-9_]+_agent\.py", fname):
            return {"content": [{"type": "text", "text": "The filename must be snake_case and end in _agent.py."}], "isError": True}
        status, text = http_tool({"method": "POST", "path": "/agents/import", "file": {"name": fname, "text": args.get("code", "")}})
        if status != 200:
            return {"content": [{"type": "text", "text": f"Install failed ({status}): {text[:500]}"}], "isError": True}
        health = json.loads(call_engine("GET", "/health")[1])
        problems = [q for q in health.get("quarantined", []) if fname in q]
        if problems:  # take a file that does not load back out, so a failed attempt never lingers
            http_tool({"method": "DELETE", "path": "/agents/" + fname})
            return {"content": [{"type": "text", "text": "It did not load, so it was not installed: " + "; ".join(problems)[:800]}], "isError": True}
        return {"content": [{"type": "text", "text": f"Installed {fname}. {DISTRO['display_name']} now has: {', '.join(health.get('agents', []))}."}],
                "structuredContent": {"installed": fname, "agents": health.get("agents", [])}}
    if name == "http":
        status, text = http_tool(args)
        return {"content": [{"type": "text", "text": f"{status}"}], "structuredContent": {"status": status, "body": text}}
    raise KeyError(name)


def host_name(client):
    c = client.lower()
    for key, name in (("claude", "Claude"), ("chatgpt", "ChatGPT"), ("openai", "ChatGPT"), ("codex", "Codex"),
                      ("cursor", "Cursor"), ("copilot", "Copilot"), ("visual studio code", "Copilot"), ("vscode", "Copilot")):
        if key in c:
            return name
    return "Assistant"


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        _host["name"] = host_name((params.get("clientInfo") or {}).get("name", ""))
        _host["seen"] = (turns_after(0) or [{"n": 0}])[-1]["n"]
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}, "resources": {},
                                 "extensions": {"io.modelcontextprotocol/ui": {}}},
                "serverInfo": {"name": DISTRO["id"], "title": DISTRO["display_name"], "version": VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tools()}
    if method == "tools/call":
        try:
            return call_tool(params.get("name"), params.get("arguments") or {})
        except KeyError:
            raise ValueError(f"Unknown tool: {params.get('name')}")
        except Exception as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    if method == "resources/list":
        return {"resources": [{"uri": UI_URI, "name": f"{DISTRO['display_name']} chat", "mimeType": UI_MIME}]}
    if method == "resources/read":
        if params.get("uri") != UI_URI:
            raise ValueError("Unknown resource")
        csp = {"connectDomains": DISTRO.get("connect_domains", []), "resourceDomains": []}
        return {"contents": [{"uri": UI_URI, "mimeType": UI_MIME, "text": app_html(),
                              "_meta": {"ui": {"csp": csp, "prefersBorder": False},
                                        "openai/widgetCSP": {"connect_domains": csp["connectDomains"], "resource_domains": []},
                                        "openai/widgetDescription": f"The {DISTRO['display_name']} chat window."}}]}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    raise LookupError(method)


def serve():
    out_lock = threading.Lock()

    def send(obj):
        with out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def work(msg):
        try:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)})
        except LookupError:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"Method not found: {msg.get('method')}"}})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": str(e)}})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" in msg and "method" in msg:
            threading.Thread(target=work, args=(msg,), daemon=True).start()  # a long chat never blocks the page's other calls
    if _engine["proc"]:
        _engine["proc"].terminate()


if __name__ == "__main__":
    if "--check" in sys.argv:
        report = {"engine": engine(), "health": json.loads(call_engine("GET", "/health")[1]), "page_bytes": len(app_html().encode())}
        print(json.dumps(report, indent=2))
        if _engine["proc"]:
            _engine["proc"].terminate()
    else:
        serve()
