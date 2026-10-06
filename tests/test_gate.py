#!/usr/bin/env python3
"""The privacy gate on made-up data: everything private is swapped out before a gated voice sees it, swapped back
in the reply, and a gate that cannot read its private list refuses (fails closed)."""
import importlib.util, os, sys, tempfile

deny = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
deny.write("# made-up private terms\nContoso Bakery\nre:project\\s+falcon\n")
deny.close()
os.environ["DISTRO_DENYLIST"] = deny.name
spec = importlib.util.spec_from_file_location("server", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin", "server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

private = ["jane.doe@example.com", "sk-test0000AAAA1111BBBB2222", "+1 (555) 010-0199", "/Users/janedoe",
           "10.0.0.12", "4111 1111 1111 1111", "Contoso Bakery", "Project Falcon"]
text = ("User: email jane.doe@example.com about Contoso Bakery and Project Falcon. Key sk-test0000AAAA1111BBBB2222, "
        "call +1 (555) 010-0199, files in /Users/janedoe/notes, server 10.0.0.12, card 4111 1111 1111 1111. "
        "Then email jane.doe@example.com again.")
safe, found, n = server.gate(text)
reply = "Done: I wrote to [EMAIL_1] about [PRIVATE_1]."
checks = {
    "nothing private reaches the voice": not any(p.lower() in safe.lower() for p in private),
    "ordinary words survive": "email" in safe and "Then" in safe,
    "the same value gets the same placeholder": safe.count("[EMAIL_1]") == 2 and "[EMAIL_2]" not in safe,
    "the reply is put back together locally": server.ungate(reply, found) == "Done: I wrote to jane.doe@example.com about Contoso Bakery.",
    "every swap is counted": n == len(found) >= 8,
}
# the optional on-device screen: a stand-in local model that names a person
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Screen(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        b = json.dumps({"choices": [{"message": {"content": '{"items": ["Maria Lopez"]}'}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
srv = ThreadingHTTPServer(("127.0.0.1", 0), Screen); threading.Thread(target=srv.serve_forever, daemon=True).start()
server.DISTRO["screen"] = {"url": f"http://127.0.0.1:{srv.server_port}/v1", "model": "stand-in"}
safe2, found2, _ = server.gate("Maria Lopez asked about the order.")
checks["the on-device screen catches what patterns miss"] = "Maria Lopez" not in safe2 and "[PRIVATE_1]" in safe2
server.DISTRO["screen"] = {"url": "https://example.com/v1", "model": "x"}
try:
    server.gate("hello"); checks["the screen must be on this machine"] = False
except Exception:
    checks["the screen must be on this machine"] = True
server.DISTRO["screen"] = {"url": "http://127.0.0.1:9/v1", "model": "x"}
try:
    server.gate("hello"); checks["an unreachable screen fails closed"] = False
except Exception:
    checks["an unreachable screen fails closed"] = True
server.DISTRO.pop("screen")
os.environ["DISTRO_DENYLIST"] = "/nonexistent/denylist.txt"
try:
    server.gate("anything")
    checks["an unreadable private list fails closed"] = False
except Exception:
    checks["an unreadable private list fails closed"] = True
os.unlink(deny.name)
for k, v in checks.items():
    print(("PASS " if v else "FAIL ") + k)
if not checks["nothing private reaches the voice"]:
    print(safe)
sys.exit(0 if all(checks.values()) else 1)
