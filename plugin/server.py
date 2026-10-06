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
        if _engine["url"] and (_engine["proc"] is None or _engine["proc"].poll() is None):
            return _engine["url"]
        eng = DISTRO["engine"]
        url = os.environ.get("DISTRO_ENGINE_URL") or eng.get("url")
        if url and _healthy(url.rstrip("/")):
            _engine["url"] = url.rstrip("/")
            return _engine["url"]
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
    inject = (f"<script>{bridge}</script>\n"
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
        {"name": "chat", "title": f"Ask {name}",
         "description": f"Send a message to {name} and get its answer. {name} runs the user's own agents on their machine.",
         "inputSchema": {"type": "object", "required": ["message"], "properties": {
             "message": {"type": "string"}, "session_id": {"type": "string"}}}},
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
        body = {"user_input": args.get("message", ""), "conversation_history": []}
        if args.get("session_id"):
            body["session_id"] = args["session_id"]
        status, text = call_engine("POST", "/chat", json.dumps(body))
        try:
            d = json.loads(text)
        except ValueError:
            d = {"error": text[:500]}
        if status != 200:
            return {"content": [{"type": "text", "text": d.get("error") or f"The engine answered {status}."}], "isError": True}
        return {"content": [{"type": "text", "text": d.get("response", "")}],
                "structuredContent": {"response": d.get("response", ""), "session_id": d.get("session_id")}}
    if name == "http":
        status, text = http_tool(args)
        return {"content": [{"type": "text", "text": f"{status}"}], "structuredContent": {"status": status, "body": text}}
    raise KeyError(name)


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
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
