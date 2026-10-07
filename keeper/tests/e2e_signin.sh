#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export HOME=/root
export KEEPER_HEALTH_TIMEOUT=90
export KEEPER_INSTALLER_TIMEOUT=900

KEEPER=/work/keeper/keeper.py
MAIN="$HOME/.brainstem/src/rapp_brainstem"
TWIN_ROOT=/tmp/keeper-signin-twins
TWIN_ONE="$TWIN_ROOT/twin-one"
TWIN_TWO="$TWIN_ROOT/twin-two"
TWIN_ONE_PID=
TWIN_TWO_PID=
OUTPUT_LOG=/tmp/keeper-signin-e2e-output.log

if [ "${KEEPER_SIGNIN_E2E_CAPTURED:-0}" != "1" ]; then
  export KEEPER_SIGNIN_E2E_CAPTURED=1
  set +e
  bash "$0" "$@" 2>&1 | tee "$OUTPUT_LOG"
  STATUS=${PIPESTATUS[0]}
  exit "$STATUS"
fi

cleanup() {
  local pid pid_file
  for pid in "$TWIN_ONE_PID" "$TWIN_TWO_PID"; do
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  for pid_file in \
    "$HOME/.brainstem/keeper/safe.pid" \
    "$HOME/.brainstem/keeper/installed.pid"; do
    if [ -f "$pid_file" ]; then
      pid=$(cat "$pid_file" 2>/dev/null || true)
      if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
      fi
    fi
  done
}
trap cleanup EXIT

wait_for_health() {
  local port=$1
  python3 - "$port" <<'PY'
import json
import sys
import time
import urllib.request

port = int(sys.argv[1])
deadline = time.monotonic() + 60
last_error = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:{}/health".format(port),
            timeout=2,
        ) as response:
            data = json.load(response)
        if response.status == 200 and data.get("status") in ("ok", "unauthenticated"):
            print(
                "HEALTH_READY port={} status={}".format(
                    port,
                    data.get("status"),
                )
            )
            raise SystemExit(0)
    except Exception as exc:
        last_error = str(exc)
    time.sleep(0.25)
raise SystemExit("health timeout on port {}: {}".format(port, last_error))
PY
}

start_twin() {
  local directory=$1
  local port=$2
  local log_path=$3
  (
    cd "$directory"
    PORT="$port" PYTHONUNBUFFERED=1 \
      exec "$HOME/.brainstem/venv/bin/python" brainstem.py
  ) >"$log_path" 2>&1 &
  echo "$!"
}

compare_auth_status() {
  python3 - "$@" <<'PY'
import json
import sys
import urllib.request

observed = {}
for raw_port in sys.argv[1:]:
    port = int(raw_port)
    with urllib.request.urlopen(
        "http://127.0.0.1:{}/health".format(port),
        timeout=10,
    ) as response:
        health = json.load(response)
    observed[port] = {
        "status": health.get("status"),
        "auth_error": health.get("auth_error"),
        "copilot": health.get("copilot"),
    }
reference = next(iter(observed.values()))
assert all(value == reference for value in observed.values()), observed
print("AUTH_STATUS " + json.dumps(reference, sort_keys=True))
PY
}

echo "SETUP installing Debian prerequisites"
apt-get update -qq
apt-get install -y -qq curl git sudo ca-certificates python3 python3-venv \
  >/tmp/keeper-signin-apt.log
python3 --version
git --version

printf '0.0.0.0 api.github.com\n' >> /etc/hosts
mkdir -p "$MAIN"
python3 - "$MAIN/.copilot_token" <<'PY'
import json
import os
import sys
import time

path = sys.argv[1]
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "access_token": "ghu_KEEPER_SIGNIN_E2E_ORIGINAL_NOT_A_CREDENTIAL",
            "saved_at": time.time(),
        },
        handle,
    )
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY

echo "STEP 1 official install brainstem-v0.6.16"
python3 - "$KEEPER" <<'PY'
import importlib.util
import sys
from pathlib import Path

path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("keeper_signin_e2e", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ok, reason = module.Keeper().run_installer("0.6.16")
print("INSTALL_0_6_16 ok={} reason={}".format(ok, reason))
if not ok:
    raise SystemExit(1)
PY
python3 "$KEEPER" start
wait_for_health 7071

echo "STEP 2 start two token-free twins"
mkdir -p "$TWIN_ONE" "$TWIN_TWO"
cp -a "$MAIN/." "$TWIN_ONE/"
cp -a "$MAIN/." "$TWIN_TWO/"
for twin in "$TWIN_ONE" "$TWIN_TWO"; do
  rm -f \
    "$twin/.copilot_token" \
    "$twin/.copilot_session" \
    "$twin/.copilot_pending" \
    "$twin/.brainstem_book.json"
done
TWIN_ONE_PID=$(start_twin "$TWIN_ONE" 7072 /tmp/keeper-twin-one.log)
TWIN_TWO_PID=$(start_twin "$TWIN_TWO" 7073 /tmp/keeper-twin-two.log)
wait_for_health 7072
wait_for_health 7073

echo "STEP 3 adopt main and register both twins"
python3 "$KEEPER" signin adopt
python3 "$KEEPER" signin register "$TWIN_ONE"
python3 "$KEEPER" signin register "$TWIN_TWO"
compare_auth_status 7071 7072 7073
python3 "$KEEPER" signin status

echo "STEP 4 simulate a newer successful sign-in in twin 2"
kill "$TWIN_TWO_PID"
wait "$TWIN_TWO_PID" 2>/dev/null || true
TWIN_TWO_PID=
python3 - "$TWIN_TWO" <<'PY'
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

brainstem = Path(sys.argv[1])
token_path = brainstem / ".copilot_token"
flight_path = brainstem / ".brainstem_book.json"

def atomic_json(path, value, mode):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(str(temporary), mode)
    os.replace(str(temporary), str(path))

atomic_json(
    token_path,
    {
        "access_token": "ghu_KEEPER_SIGNIN_E2E_NEWER_NOT_A_CREDENTIAL",
        "saved_at": time.time(),
    },
    0o600,
)
try:
    events = json.loads(flight_path.read_text(encoding="utf-8"))
except (FileNotFoundError, ValueError):
    events = []
if not isinstance(events, list):
    events = []
event_time = time.time() + 1
event_text = datetime.fromtimestamp(
    event_time,
    timezone.utc,
).isoformat()
events.extend(
    [
        {
            "ts": event_text,
            "type": "login.authorized",
            "level": "info",
        },
        {
            "ts": event_text,
            "type": "auth.token_saved",
            "level": "info",
        },
    ]
)
atomic_json(flight_path, events, 0o600)
shutil.copyfile(token_path, "/tmp/twin-two-new-token")
os.chmod("/tmp/twin-two-new-token", 0o600)
print("SIMULATED_SIGNIN twin=brainstem-2")
PY

python3 "$KEEPER" signin status
python3 "$KEEPER" signin sync
cmp -s /tmp/twin-two-new-token "$HOME/.brainstem/keeper/signin/.copilot_token"
cmp -s /tmp/twin-two-new-token "$MAIN/.copilot_token"
cmp -s /tmp/twin-two-new-token "$TWIN_ONE/.copilot_token"
cmp -s /tmp/twin-two-new-token "$TWIN_TWO/.copilot_token"
echo "ADOPT_FORWARD holder, main, twin 1, and twin 2 are byte-identical"

TWIN_TWO_PID=$(start_twin "$TWIN_TWO" 7073 /tmp/keeper-twin-two-restarted.log)
wait_for_health 7073
compare_auth_status 7071 7072 7073

python3 - "$OUTPUT_LOG" "$HOME/.brainstem/keeper/keeper.log" <<'PY'
import sys
from pathlib import Path

sentinels = (
    "ghu_KEEPER_SIGNIN_E2E_ORIGINAL_NOT_A_CREDENTIAL",
    "ghu_KEEPER_SIGNIN_E2E_NEWER_NOT_A_CREDENTIAL",
)
for raw_path in sys.argv[1:]:
    path = Path(raw_path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for sentinel in sentinels:
        assert sentinel not in text, (path, sentinel)
print("CREDENTIAL_LEAK_CHECK no credential text in keeper output or log")
PY

echo "STEP 5 uninstall leaves every Brainstem token in place"
python3 "$KEEPER" uninstall
test ! -e "$HOME/.brainstem/keeper"
cmp -s /tmp/twin-two-new-token "$MAIN/.copilot_token"
cmp -s /tmp/twin-two-new-token "$TWIN_ONE/.copilot_token"
cmp -s /tmp/twin-two-new-token "$TWIN_TWO/.copilot_token"
echo "UNINSTALL_PRESERVED main, twin 1, and twin 2 token files"
echo "SIGNIN E2E PASS"
