#!/usr/bin/env python3
"""Prove the distro stands on the exact kernel commit recorded in kernel.json."""

import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
k = json.load(open(os.path.join(ROOT, "plugin", "kernel.json")))
d = json.load(open(os.path.join(ROOT, "plugin", "distro.json")))
REQUIRED = {"kernel", "sha", "version", "path", "kernel_blob"}
OPTIONAL = {
    "pinned", "ui_path", "ui_blob", "soul_path", "soul_blob",
    "contract", "rule", "vendored",
}
HEX40 = re.compile(r"[0-9a-f]{40}")


def raw(path):
    path = urllib.parse.quote(path, safe="/")
    request = urllib.request.Request(
        f"https://raw.githubusercontent.com/{k['kernel']}/{k['sha']}/{path}",
        headers={"User-Agent": "brainstem-distro-check"},
    )
    with urllib.request.urlopen(request, timeout=60) as r:
        return r.read()


def blob(b):
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


checks = {}
errors = {}


def check(name, action):
    try:
        checks[name] = bool(action())
    except (OSError, KeyError, UnicodeDecodeError, ValueError) as exc:
        checks[name] = False
        errors[name] = str(exc)


check("pin has only canonical keys", lambda: REQUIRED <= set(k) and not (set(k) - REQUIRED - OPTIONAL))
check("sha is a full commit", lambda: isinstance(k["sha"], str) and HEX40.fullmatch(k["sha"]))
check("pin is a real kernel commit", lambda: len(raw(k["path"])) > 0)
check("kernel file matches its git blob", lambda: blob(raw(k["path"])) == k["kernel_blob"])
version_path = str(pathlib.PurePosixPath(k["path"]).parent / "VERSION")
check("kernel version matches the commit", lambda: raw(version_path).decode().strip() == k["version"])
check("pinned chat page matches its hash", lambda: blob(raw(k["ui_path"])) == k["ui_blob"])
copies = [os.path.relpath(os.path.join(dp, f), ROOT) for dp, _, fs in os.walk(ROOT) if ".git" not in dp and "node_modules" not in dp
          for f in fs if f in ("brainstem.py", "index.html")]
checks["no copied kernel files in the repo"] = not copies
checks["distro has an id and a display name"] = bool(d.get("id")) and bool(d.get("display_name"))
checks["engine is a running kernel or a command"] = bool(d["engine"].get("url") or d["engine"].get("command"))
for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
    if name in errors:
        print("  " + errors[name])
if copies:
    print("  copies:", ", ".join(copies))
sys.exit(0 if all(checks.values()) else 1)
