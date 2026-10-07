#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export HOME=/root
export KEEPER_HEALTH_TIMEOUT=90
export KEEPER_INSTALLER_TIMEOUT=900

KEEPER=/work/keeper/keeper.py
MIRROR=/tmp/keeper-grail-mirror.git
MIRROR_WORK=/tmp/keeper-grail-work

cleanup() {
  local pid_file pid
  for pid_file in "$HOME/.brainstem/keeper/safe.pid" "$HOME/.brainstem/keeper/installed.pid"; do
    if [ -f "$pid_file" ]; then
      pid=$(cat "$pid_file" 2>/dev/null || true)
      if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
      fi
    fi
  done
}
trap cleanup EXIT

snapshot_tree() {
  local root=$1
  local output=$2
  (
    cd "$root"
    find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
    find . -type l -print0 | LC_ALL=C sort -z |
      while IFS= read -r -d '' link; do
        printf 'LINK %s -> %s\n' "$link" "$(readlink "$link")"
      done
  ) > "$output"
}

snapshot_tracked_source() {
  local output=$1
  (
    cd "$HOME/.brainstem/src"
    git ls-files -z |
      while IFS= read -r -d '' file; do
        if [ -f "$file" ]; then
          sha256sum "$file"
        elif [ -L "$file" ]; then
          printf 'LINK %s -> %s\n' "$file" "$(readlink "$file")"
        fi
      done
  ) > "$output"
}

snapshot_without_keeper() {
  local output=$1
  (
    cd "$HOME/.brainstem"
    find . -path './keeper' -prune -o -type f -print0 |
      LC_ALL=C sort -z | xargs -0 -r sha256sum
    find . -path './keeper' -prune -o -type l -print0 |
      LC_ALL=C sort -z |
      while IFS= read -r -d '' link; do
        printf 'LINK %s -> %s\n' "$link" "$(readlink "$link")"
      done
  ) > "$output"
}

observe_contract() {
  local expected=$1
  python3 - "$expected" <<'PY'
import json
import sys
import urllib.error
import urllib.request

expected = sys.argv[1]
with urllib.request.urlopen("http://127.0.0.1:7071/health", timeout=10) as response:
    health = json.load(response)
    assert response.status == 200, response.status
assert health.get("version") == expected, health
assert health.get("status") in ("ok", "unauthenticated"), health
print("HEALTH " + json.dumps(health, sort_keys=True))

request = urllib.request.Request(
    "http://127.0.0.1:7071/chat",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(request, timeout=10)
except urllib.error.HTTPError as exc:
    try:
        body = json.loads(exc.read().decode("utf-8"))
        status = exc.code
    finally:
        exc.close()
else:
    raise AssertionError("POST /chat {} unexpectedly succeeded")
assert status == 400, (status, body)
assert isinstance(body.get("error"), str) and body["error"], body
print("CHAT status={} body={}".format(status, json.dumps(body, sort_keys=True)))
PY
}

show_state() {
  python3 - <<'PY'
import json
from pathlib import Path

state = json.loads(Path("/root/.brainstem/keeper/state.json").read_text())
excerpt = {
    "last_good": state.get("last_good"),
    "safe_version": state.get("safe_version"),
    "failed": state.get("failed"),
    "serving": state.get("serving"),
    "history_tail": state.get("history", [])[-4:],
}
print("STATE " + json.dumps(excerpt, sort_keys=True))
PY
}

echo "SETUP installing Debian prerequisites"
apt-get update -qq
apt-get install -y -qq curl git sudo ca-certificates python3 python3-venv >/tmp/keeper-apt.log
python3 --version
git --version

printf '0.0.0.0 api.github.com\n' >> /etc/hosts
TOKEN_VALUE="ghu_$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
mkdir -p "$HOME/.brainstem/src/rapp_brainstem"
printf '%s\n' "$TOKEN_VALUE" > "$HOME/.brainstem/src/rapp_brainstem/.copilot_token"

echo "STEP 1 official install brainstem-v0.6.15 without a persistent launch"
python3 - "$KEEPER" <<'PY'
import importlib.util
import sys
from pathlib import Path

path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("keeper_e2e", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ok, reason = module.Keeper().run_installer("0.6.15")
print("INSTALL_0_6_15 ok={} reason={}".format(ok, reason))
if not ok:
    raise SystemExit(1)
PY
printf '%s\n' "$TOKEN_VALUE" > "$HOME/.brainstem/src/rapp_brainstem/.copilot_token"
snapshot_tracked_source /tmp/source-before-first-start.sha256
python3 "$KEEPER" start
observe_contract "0.6.15"
show_state
test -d "$HOME/.brainstem/keeper/safe/0.6.15"
test -x "$HOME/.brainstem/keeper/safe-venv/bin/python"
snapshot_tracked_source /tmp/source-after-first-start.sha256
diff -u /tmp/source-before-first-start.sha256 /tmp/source-after-first-start.sha256
echo "SOURCE_COMPARISON first keeper start left all tracked grail files unchanged"

echo "STEP 2 upgrade to brainstem-v0.6.16"
python3 "$KEEPER" upgrade --to 0.6.16
observe_contract "0.6.16"
show_state

echo "STEP 3 create local bad release brainstem-v0.6.99"
git clone --mirror "$HOME/.brainstem/src" "$MIRROR" >/dev/null 2>&1
git clone "$MIRROR" "$MIRROR_WORK" >/dev/null 2>&1
git -C "$MIRROR_WORK" config user.name "Keeper E2E"
git -C "$MIRROR_WORK" config user.email "keeper-e2e@example.invalid"
git -C "$MIRROR_WORK" checkout -q brainstem-v0.6.16
printf '\ndef keeper_broken(:\n    pass\n' >> "$MIRROR_WORK/rapp_brainstem/brainstem.py"
printf '0.6.99\n' > "$MIRROR_WORK/rapp_brainstem/VERSION"
git -C "$MIRROR_WORK" add rapp_brainstem/brainstem.py rapp_brainstem/VERSION
git -C "$MIRROR_WORK" commit -q -m "keeper e2e bad release"
git -C "$MIRROR_WORK" tag brainstem-v0.6.99
git -C "$MIRROR_WORK" push -q origin HEAD:refs/heads/keeper-e2e-bad brainstem-v0.6.99
git config --global protocol.file.allow always
git config --global url."file://$MIRROR".insteadOf "https://github.com/kody-w/rapp-installer.git"
git config --global --add url."file://$MIRROR".insteadOf "https://github.com/kody-w/rapp-installer"

python3 "$KEEPER" upgrade --to 0.6.99
observe_contract "0.6.16"
show_state
python3 - <<'PY'
import json
from pathlib import Path

state = json.loads(Path("/root/.brainstem/keeper/state.json").read_text())
assert "0.6.99" in state["failed"], state
assert state["last_good"] == "0.6.16", state
PY

echo "STEP 4 failed release is skipped, then higher healthy release is accepted"
python3 "$KEEPER" upgrade --to 0.6.99
observe_contract "0.6.16"
git -C "$MIRROR_WORK" checkout -q -B keeper-e2e-good brainstem-v0.6.16
printf '0.6.100\n' > "$MIRROR_WORK/rapp_brainstem/VERSION"
git -C "$MIRROR_WORK" add rapp_brainstem/VERSION
git -C "$MIRROR_WORK" commit -q -m "keeper e2e healthy release"
git -C "$MIRROR_WORK" tag brainstem-v0.6.100
git -C "$MIRROR_WORK" push -q origin HEAD:refs/heads/keeper-e2e-good brainstem-v0.6.100
python3 "$KEEPER" upgrade --to 0.6.100
observe_contract "0.6.100"
show_state

echo "STEP 5 catastrophe falls back to safe copy"
if [ -f "$HOME/.brainstem/keeper/installed.pid" ]; then
  INSTALLED_PID=$(cat "$HOME/.brainstem/keeper/installed.pid")
  kill "$INSTALLED_PID"
  for _ in $(seq 1 50); do
    if ! kill -0 "$INSTALLED_PID" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
fi
printf '\ndef keeper_catastrophe(:\n    pass\n' >> "$HOME/.brainstem/src/rapp_brainstem/brainstem.py"
rm -rf "$HOME/.brainstem/venv"
rm -f "$HOME/.gitconfig"
printf '0.0.0.0 github.com\n' >> /etc/hosts
python3 "$KEEPER" start
observe_contract "0.6.100"
python3 "$KEEPER" status
show_state
python3 - <<'PY'
import json
import subprocess
import urllib.request

status = json.loads(subprocess.check_output(
    ["python3", "/work/keeper/keeper.py", "status"], text=True
))
assert status["serving"] == "safe", status
with urllib.request.urlopen("http://127.0.0.1:7071/health", timeout=10) as response:
    health = json.load(response)
assert "/keeper/safe/0.6.100" in health["brainstem_dir"], health
PY

echo "STEP 6 uninstall removes keeper and changes nothing else"
snapshot_tree "$HOME/.brainstem/src" /tmp/source-before-uninstall.sha256
snapshot_without_keeper /tmp/brainstem-before-uninstall.sha256
show_state
python3 "$KEEPER" uninstall
test ! -e "$HOME/.brainstem/keeper"
test -d "$HOME/.brainstem/src/rapp_brainstem"
snapshot_tree "$HOME/.brainstem/src" /tmp/source-after-uninstall.sha256
snapshot_without_keeper /tmp/brainstem-after-uninstall.sha256
diff -u /tmp/source-before-uninstall.sha256 /tmp/source-after-uninstall.sha256
diff -u /tmp/brainstem-before-uninstall.sha256 /tmp/brainstem-after-uninstall.sha256
echo "REMOVAL_COMPARISON source listing+hashes and all ~/.brainstem files outside keeper are byte-identical"
echo "E2E PASS"
