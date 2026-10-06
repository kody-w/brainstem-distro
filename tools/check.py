#!/usr/bin/env python3
"""Prove the distro stands on the kernel unchanged: the pin is a real kernel commit, the pinned chat page
matches its recorded hash, and nothing in this repo is a copy of a kernel file that could drift."""
import hashlib, json, os, sys, urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
k = json.load(open(os.path.join(ROOT, "plugin", "kernel.json")))
d = json.load(open(os.path.join(ROOT, "plugin", "distro.json")))


def raw(path):
    with urllib.request.urlopen(f"https://raw.githubusercontent.com/{k['kernel']}/{k['sha']}/{path}", timeout=60) as r:
        return r.read()


def blob(b):
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


checks = {}
try:
    checks["pin is a real kernel commit"] = len(raw(k["path"])) > 0
except Exception:
    checks["pin is a real kernel commit"] = False
try:
    checks["pinned chat page matches its hash"] = blob(raw(k["ui_path"])) == k["ui_blob"]
except Exception:
    checks["pinned chat page matches its hash"] = False
copies = [os.path.relpath(os.path.join(dp, f), ROOT) for dp, _, fs in os.walk(ROOT) if ".git" not in dp and "node_modules" not in dp
          for f in fs if f in ("brainstem.py", "index.html")]
checks["no copied kernel files in the repo"] = not copies
checks["distro has an id and a display name"] = bool(d.get("id")) and bool(d.get("display_name"))
checks["engine is a running kernel or a command"] = bool(d["engine"].get("url") or d["engine"].get("command"))
for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
if copies:
    print("  copies:", ", ".join(copies))
sys.exit(0 if all(checks.values()) else 1)
