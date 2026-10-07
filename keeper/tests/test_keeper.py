import contextlib
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


KEEPER_PATH = Path(__file__).resolve().parents[1] / "keeper.py"
SPEC = importlib.util.spec_from_file_location("keeper_module", KEEPER_PATH)
keeper_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(keeper_module)


def write_token(brainstem_dir, content):
    brainstem_dir.mkdir(parents=True, exist_ok=True)
    token_path = brainstem_dir / ".copilot_token"
    token_path.write_bytes(content)
    token_path.chmod(0o600)
    return token_path


def write_flight_log(brainstem_dir, events):
    path = brainstem_dir / keeper_module.SIGNIN_FLIGHT_LOG
    path.write_text(json.dumps(events), encoding="utf-8")
    return path


class ContractHandler(BaseHTTPRequestHandler):
    version = "0.6.16"
    health_status = "ok"
    chat_status = 400

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(
            {"status": self.health_status, "version": self.version}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/chat":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({"error": "user_input is required"}).encode("utf-8")
        self.send_response(self.chat_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class FakeKeeper(keeper_module.Keeper):
    def __init__(self, root):
        super().__init__(brainstem_home=Path(root) / ".brainstem")
        self.current_version = None
        self.serving_version = None
        self.serving_kind = None
        self.install_calls = []
        self.capture_calls = []
        self.installer_failures = set()
        self.launch_failures = {}
        self.safe_failures = set()

    def _write_log(self, message, display=True):
        super()._write_log(message, display=False)

    def installed_version(self):
        return self.current_version

    def probe_health(self, expected_version=None, timeout=2):
        healthy = self.serving_version is not None
        if healthy and expected_version is not None:
            healthy = (
                keeper_module.normalize_version(expected_version)
                == self.serving_version
            )
        return {
            "healthy": healthy,
            "status": "ok" if healthy else None,
            "version": self.serving_version,
            "chat_status": 400 if healthy else None,
            "chat_error": "user_input is required" if healthy else None,
            "reason": None if healthy else "not serving",
        }

    def _health_is_safe(self, _state, health):
        return health["healthy"] and self.serving_kind == "safe"

    def launch_installed(self, expected_version):
        version = keeper_module.normalize_version(expected_version)
        remaining = self.launch_failures.get(version, 0)
        if remaining:
            self.launch_failures[version] = remaining - 1
            self.serving_version = None
            self.serving_kind = None
            return {"healthy": False, "reason": "fake unhealthy release"}
        self.serving_version = version
        self.serving_kind = "installed"
        return self.probe_health(version)

    def launch_safe(self, version):
        version = keeper_module.normalize_version(version)
        if version in self.safe_failures:
            return {"healthy": False, "reason": "fake safe failure"}
        self.serving_version = version
        self.serving_kind = "safe"
        return self.probe_health(version)

    def run_installer(self, version):
        version = keeper_module.normalize_version(version)
        self.install_calls.append(version)
        self.serving_version = None
        self.serving_kind = None
        if version in self.installer_failures:
            return False, "fake installer failure"
        self.current_version = version
        return True, "fake installer success"

    def capture_safe_copy(self, version):
        version = keeper_module.normalize_version(version)
        self.capture_calls.append(version)
        return self.safe_root / version, self.safe_venv / "bin" / "python"

    def _stop_managed_processes(self):
        return

    def _stop_port_listeners(self):
        return


class CopyKeeper(keeper_module.Keeper):
    def __init__(self, root, tracked):
        super().__init__(brainstem_home=Path(root) / ".brainstem")
        self.tracked = tracked

    def _tracked_kernel_files(self):
        return list(self.tracked)

    def _write_log(self, message, display=True):
        super()._write_log(message, display=False)

    def _ensure_safe_venv(self, _tree):
        return self.grail_venv / "bin" / "python"


class UninstallKeeper(keeper_module.Keeper):
    def _stop_managed_processes(self):
        return

    def uninstall_service(self):
        return


class KeeperTests(unittest.TestCase):
    def test_version_normalization_and_comparison(self):
        self.assertEqual(
            keeper_module.normalize_version("brainstem-v0.6.016"), "0.6.16"
        )
        self.assertGreater(
            keeper_module.compare_versions("0.6.100", "0.6.99"), 0
        )
        self.assertEqual(
            keeper_module.compare_versions("1.0", "1.0.0"), 0
        )
        with self.assertRaises(keeper_module.KeeperError):
            keeper_module.normalize_version("latest")

    def test_http_probe_requires_health_and_chat_contract(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ContractHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                keeper = keeper_module.Keeper(
                    brainstem_home=Path(temporary) / ".brainstem"
                )
                keeper.base_url = "http://127.0.0.1:{}".format(
                    server.server_port
                )
                result = keeper.probe_health("0.6.16")
                self.assertTrue(result["healthy"], result)
                wrong = keeper.probe_health("0.6.15")
                self.assertFalse(wrong["healthy"])
                self.assertIn("expected version", wrong["reason"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_probe_rejects_non_400_chat_response(self):
        class BadChatHandler(ContractHandler):
            chat_status = 200

        server = ThreadingHTTPServer(("127.0.0.1", 0), BadChatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                keeper = keeper_module.Keeper(
                    brainstem_home=Path(temporary) / ".brainstem"
                )
                keeper.base_url = "http://127.0.0.1:{}".format(
                    server.server_port
                )
                result = keeper.probe_health("0.6.16")
                self.assertFalse(result["healthy"])
                self.assertIn("POST /chat", result["reason"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_state_history_is_capped_at_twenty(self):
        with tempfile.TemporaryDirectory() as temporary:
            keeper = keeper_module.Keeper(
                brainstem_home=Path(temporary) / ".brainstem"
            )
            state = keeper.load_state()
            for index in range(25):
                keeper.record_event(state, "event", index=index)
            keeper.save_state(state)
            loaded = keeper.load_state()
            self.assertEqual(len(loaded["history"]), 20)
            self.assertEqual(loaded["history"][0]["details"]["index"], 5)

    def test_start_is_idempotent_and_records_safe_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            keeper = FakeKeeper(temporary)
            keeper.current_version = "0.6.15"
            keeper.serving_version = "0.6.15"
            keeper.serving_kind = "installed"
            result = keeper.start()
            state = keeper.load_state()
            self.assertTrue(result["healthy"])
            self.assertEqual(state["last_good"], "0.6.15")
            self.assertEqual(state["safe_version"], "0.6.15")
            self.assertEqual(keeper.capture_calls, ["0.6.15"])
            self.assertEqual(keeper.install_calls, [])

    def test_start_reinstalls_last_good_after_installed_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            keeper = FakeKeeper(temporary)
            keeper.current_version = "0.6.15"
            keeper.launch_failures["0.6.15"] = 1
            state = keeper.load_state()
            state["last_good"] = "0.6.15"
            state["safe_version"] = "0.6.14"
            keeper.save_state(state)
            result = keeper.start()
            self.assertTrue(result["healthy"])
            self.assertEqual(keeper.install_calls, ["0.6.15"])
            self.assertEqual(keeper.serving_kind, "installed")

    def test_start_falls_back_to_safe_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            keeper = FakeKeeper(temporary)
            keeper.current_version = "0.6.15"
            keeper.launch_failures["0.6.15"] = 10
            keeper.installer_failures.add("0.6.15")
            state = keeper.load_state()
            state["last_good"] = "0.6.15"
            state["safe_version"] = "0.6.14"
            keeper.save_state(state)
            result = keeper.start()
            self.assertTrue(result["healthy"])
            self.assertEqual(keeper.serving_kind, "safe")
            self.assertEqual(keeper.serving_version, "0.6.14")

    def test_upgrade_success_updates_last_good_and_safe_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            keeper = FakeKeeper(temporary)
            keeper.current_version = "0.6.15"
            keeper.serving_version = "0.6.15"
            keeper.serving_kind = "installed"
            result = keeper.upgrade("0.6.16")
            state = keeper.load_state()
            self.assertTrue(result["upgraded"])
            self.assertEqual(state["last_good"], "0.6.16")
            self.assertEqual(state["safe_version"], "0.6.16")
            self.assertEqual(keeper.install_calls, ["0.6.16"])
            self.assertIn("0.6.16", keeper.capture_calls)

    def test_bad_upgrade_is_marked_failed_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            keeper = FakeKeeper(temporary)
            keeper.current_version = "0.6.16"
            keeper.serving_version = "0.6.16"
            keeper.serving_kind = "installed"
            keeper.launch_failures["0.6.99"] = 10
            result = keeper.upgrade("0.6.99")
            state = keeper.load_state()
            self.assertFalse(result["upgraded"])
            self.assertEqual(result["recovered"], "rollback")
            self.assertIn("0.6.99", state["failed"])
            self.assertEqual(state["last_good"], "0.6.16")
            self.assertEqual(keeper.install_calls, ["0.6.99", "0.6.16"])
            self.assertEqual(keeper.serving_version, "0.6.16")

    def test_failed_target_skips_but_higher_target_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            keeper = FakeKeeper(temporary)
            keeper.current_version = "0.6.16"
            keeper.serving_version = "0.6.16"
            keeper.serving_kind = "installed"
            state = keeper.load_state()
            state["failed"]["0.6.99"] = {
                "time": "2026-01-01T00:00:00Z",
                "reason": "bad",
            }
            keeper.save_state(state)
            skipped = keeper.upgrade("0.6.99")
            self.assertEqual(skipped["skipped"], "failed")
            self.assertEqual(keeper.install_calls, [])
            upgraded = keeper.upgrade("0.6.100")
            self.assertTrue(upgraded["upgraded"])
            self.assertEqual(keeper.install_calls, ["0.6.100"])

    def test_force_retries_failed_target_and_clears_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            keeper = FakeKeeper(temporary)
            keeper.current_version = "0.6.16"
            keeper.serving_version = "0.6.16"
            keeper.serving_kind = "installed"
            state = keeper.load_state()
            state["failed"]["0.6.99"] = {
                "time": "2026-01-01T00:00:00Z",
                "reason": "bad",
            }
            keeper.save_state(state)
            result = keeper.upgrade("0.6.99", force=True)
            self.assertTrue(result["upgraded"])
            self.assertEqual(keeper.install_calls, ["0.6.99"])
            self.assertNotIn("0.6.99", keeper.load_state()["failed"])

    def test_safe_copy_contains_only_tracked_non_user_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracked = [
                Path("VERSION"),
                Path("brainstem.py"),
                Path("requirements.txt"),
                Path("agents/basic_agent.py"),
                Path(".env"),
                Path(".brainstem_data/history.json"),
            ]
            keeper = CopyKeeper(root, tracked)
            for relative, content in (
                ("VERSION", "0.6.16\n"),
                ("brainstem.py", "print('ok')\n"),
                ("requirements.txt", "flask\n"),
                ("agents/basic_agent.py", "class BasicAgent: pass\n"),
                ("agents/custom_agent.py", "SECRET = True\n"),
                (".env", "SECRET=value\n"),
                (".brainstem_data/history.json", "{}\n"),
                (".copilot_token", "ghu_secret\n"),
            ):
                path = keeper.kernel_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            tree, _python = keeper.capture_safe_copy("0.6.16")
            self.assertTrue((tree / "brainstem.py").is_file())
            self.assertTrue((tree / "agents" / "basic_agent.py").is_file())
            self.assertFalse((tree / "agents" / "custom_agent.py").exists())
            self.assertFalse((tree / ".env").exists())
            self.assertFalse((tree / ".copilot_token").exists())
            self.assertFalse((tree / ".brainstem_data").exists())

    def test_installer_carries_signin_files_without_overwriting_newer_files(self):
        script = b"""#!/bin/bash
set -e
target=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) target="${2#brainstem-v}"; shift 2 ;;
    *) shift ;;
  esac
done
rm -rf "$HOME/.brainstem/src"
mkdir -p "$HOME/.brainstem/src/rapp_brainstem"
printf '%s\n' "$target" > "$HOME/.brainstem/src/rapp_brainstem/VERSION"
printf 'new-token\n' > "$HOME/.brainstem/src/rapp_brainstem/.copilot_token"
chmod 600 "$HOME/.brainstem/src/rapp_brainstem/.copilot_token"
"""

        class InstallerKeeper(keeper_module.Keeper):
            def _origin_reachable(self):
                return True, None

            def _download_installer(self):
                return script

            def probe_health(self, expected_version=None, timeout=2):
                return {"healthy": False, "reason": "not running"}

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            kernel = home / ".brainstem" / "src" / "rapp_brainstem"
            kernel.mkdir(parents=True)
            original = {
                ".brainstem_secret": b"lan-secret\n",
                ".copilot_pending": b'{"device_code":"pending"}\n',
                ".copilot_session": b'{"token":"session"}\n',
                ".copilot_token": b"old-token\n",
            }
            for name, content in original.items():
                path = kernel / name
                path.write_bytes(content)
                path.chmod(0o600)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                keeper = InstallerKeeper(
                    brainstem_home=home / ".brainstem"
                )
                keeper.installer_timeout = 10
                keeper._carry_signin_files()
                self.assertEqual(
                    keeper.carry_dir.stat().st_mode & 0o777, 0o700
                )
                for name, content in original.items():
                    carried = keeper.carry_dir / name
                    self.assertEqual(carried.read_bytes(), content)
                    self.assertEqual(carried.stat().st_mode & 0o777, 0o600)
                succeeded, reason = keeper.run_installer("0.6.15")
            self.assertTrue(succeeded, reason)
            self.assertIn("without leaving a server", reason)
            self.assertEqual(
                (kernel / ".copilot_token").read_bytes(), b"new-token\n"
            )
            self.assertEqual(
                (kernel / ".copilot_token").stat().st_mode & 0o777, 0o600
            )
            for name in (
                ".brainstem_secret",
                ".copilot_pending",
                ".copilot_session",
            ):
                path = kernel / name
                self.assertEqual(path.read_bytes(), original[name])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(keeper.carry_dir.exists())

    def test_bannerless_installer_server_is_adopted_by_contract(self):
        script = b"""#!/bin/bash
set -e
target=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) target="${2#brainstem-v}"; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$HOME/.brainstem/src/rapp_brainstem"
printf '%s\n' "$target" > "$HOME/.brainstem/src/rapp_brainstem/VERSION"
exec python3 "$HOME/test-server/brainstem.py"
"""

        class InstallerKeeper(keeper_module.Keeper):
            def _origin_reachable(self):
                return True, None

            def _download_installer(self):
                return script

            def _port_pids(self):
                pid_path = Path.home() / "test-server.pid"
                if not pid_path.is_file():
                    return set()
                return {int(pid_path.read_text(encoding="ascii"))}

            def _pid_is_brainstem(self, _pid):
                return True

        server_source = """\
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "version": os.environ["KEEPER_TEST_VERSION"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({"error": "user_input is required"}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

with open(os.path.expanduser("~/test-server.pid"), "w", encoding="ascii") as handle:
    handle.write(str(os.getpid()))
ThreadingHTTPServer(("127.0.0.1", int(os.environ["KEEPER_TEST_PORT"])), Handler).serve_forever()
"""

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            server_dir = home / "test-server"
            server_dir.mkdir(parents=True)
            (server_dir / "brainstem.py").write_text(
                server_source, encoding="utf-8"
            )
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            environment = {
                "HOME": str(home),
                "KEEPER_TEST_PORT": str(port),
                "KEEPER_TEST_VERSION": "0.6.15",
            }
            with mock.patch.dict(os.environ, environment):
                keeper = InstallerKeeper(
                    brainstem_home=home / ".brainstem"
                )
                keeper.port = port
                keeper.base_url = "http://127.0.0.1:{}".format(port)
                keeper.installer_timeout = 10
                succeeded, reason = keeper.run_installer("0.6.15")
                try:
                    self.assertTrue(succeeded, reason)
                    self.assertIn("adopted PID", reason)
                    adopted = keeper._read_pid(keeper.installed_pid_path)
                    self.assertEqual(
                        adopted,
                        int(
                            (home / "test-server.pid").read_text(
                                encoding="ascii"
                            )
                        ),
                    )
                    self.assertTrue(keeper.probe_health("0.6.15")["healthy"])
                finally:
                    keeper._stop_pidfile(keeper.installed_pid_path)

    def test_default_target_reads_version_pin_and_reports_missing_pin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            kernel_json = plugin / "kernel.json"
            kernel_json.write_text(
                json.dumps({"version": "brainstem-v0.6.16"}),
                encoding="utf-8",
            )
            keeper = keeper_module.Keeper(
                brainstem_home=root / ".brainstem", repo_root=root
            )
            self.assertEqual(keeper.default_target(), "0.6.16")
            kernel_json.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(keeper_module.KeeperError, "no 'version'"):
                keeper.default_target()

    def test_install_service_fallback_copies_self_and_prints_watch_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            (repo / "plugin").mkdir(parents=True)
            (repo / "plugin" / "kernel.json").write_text(
                json.dumps({"version": "0.6.16"}), encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                keeper = keeper_module.Keeper(
                    brainstem_home=home / ".brainstem", repo_root=repo
                )
                output = io.StringIO()
                with mock.patch.object(
                    keeper_module.sys, "platform", "linux"
                ):
                    with mock.patch.object(
                        keeper, "_systemd_user_available", return_value=False
                    ), contextlib.redirect_stdout(output):
                        keeper.install_service()
            self.assertTrue(keeper.installed_script_path.is_file())
            self.assertTrue(keeper.installed_kernel_path.is_file())
            self.assertIn("watch --interval 30", output.getvalue())

    def test_signin_adopt_register_status_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brainstem_home = root / ".brainstem"
            main = brainstem_home / "src" / "rapp_brainstem"
            twin = root / "twin"
            token_record = (
                b'{"access_token":"credential-sentinel","saved_at":1}\n'
            )
            write_token(main, token_record)
            twin.mkdir()
            keeper = keeper_module.Keeper(
                brainstem_home=brainstem_home,
                now_func=lambda: 1000.0,
            )

            adopted = keeper.signin_adopt()
            registered = keeper.signin_register(twin)
            status = keeper.signin_status()

            self.assertEqual(adopted["adopted_from"], "main")
            self.assertEqual(adopted["generation"], 1)
            self.assertEqual(registered["alias"], "brainstem-1")
            self.assertEqual(
                keeper.signin_holder_path.read_bytes(),
                token_record,
            )
            self.assertEqual((twin / ".copilot_token").read_bytes(), token_record)
            self.assertEqual(
                keeper.signin_dir.stat().st_mode & 0o777,
                0o700,
            )
            for path in (
                keeper.signin_holder_path,
                keeper.signin_generation_path,
                keeper.signin_manifest_path,
                main / ".copilot_token",
                twin / ".copilot_token",
            ):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                [item["alias"] for item in status["registered"]],
                ["main", "brainstem-1"],
            )
            self.assertTrue(
                all(item["matches_holder"] for item in status["registered"])
            )
            rendered = json.dumps(status, sort_keys=True)
            self.assertNotIn("credential-sentinel", rendered)
            self.assertNotIn(str(main), rendered)
            self.assertNotIn(str(twin), rendered)

    def test_signin_status_uses_generation_size_and_mtime_not_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brainstem_home = root / ".brainstem"
            main = brainstem_home / "src" / "rapp_brainstem"
            twin = root / "twin"
            original = b"same-length-record-a\n"
            replacement = b"same-length-record-b\n"
            self.assertEqual(len(original), len(replacement))
            write_token(main, original)
            twin.mkdir()
            keeper = keeper_module.Keeper(
                brainstem_home=brainstem_home,
                now_func=lambda: 1000.0,
            )
            keeper.signin_adopt()
            keeper.signin_register(twin)
            generation = keeper._load_signin_generation(required=True)

            twin_token = twin / ".copilot_token"
            twin_token.write_bytes(replacement)
            twin_token.chmod(0o600)
            os.utime(
                str(twin_token),
                ns=(
                    generation["token_mtime_ns"],
                    generation["token_mtime_ns"],
                ),
            )

            status = keeper.signin_status()
            twin_status = next(
                item
                for item in status["registered"]
                if item["alias"] == "brainstem-1"
            )
            self.assertTrue(twin_status["matches_holder"])
            self.assertEqual(twin_status["generation"], 1)

    def test_signin_sync_projects_holder_to_registered_brainstems(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brainstem_home = root / ".brainstem"
            main = brainstem_home / "src" / "rapp_brainstem"
            twin = root / "twin"
            holder_record = b"holder-record\n"
            write_token(main, holder_record)
            twin.mkdir()
            keeper = keeper_module.Keeper(
                brainstem_home=brainstem_home,
                now_func=lambda: 1000.0,
            )
            keeper.signin_adopt()
            keeper.signin_register(twin)
            write_token(twin, b"drifted-data\n")

            result = keeper.signin_sync()

            self.assertIsNone(result["adopted_from"])
            self.assertEqual(result["generation"], 1)
            self.assertEqual((twin / ".copilot_token").read_bytes(), holder_record)
            self.assertEqual(result["projected"], ["main", "brainstem-1"])

    def test_signin_sync_adopts_newer_successful_login_and_projects_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brainstem_home = root / ".brainstem"
            main = brainstem_home / "src" / "rapp_brainstem"
            twin_one = root / "twin-one"
            twin_two = root / "twin-two"
            original = b"original-record\n"
            newer = b"newer-record\n"
            write_token(main, original)
            twin_one.mkdir()
            twin_two.mkdir()
            clock = [1000.0]
            keeper = keeper_module.Keeper(
                brainstem_home=brainstem_home,
                now_func=lambda: clock[0],
            )
            keeper.signin_adopt()
            keeper.signin_register(twin_one)
            keeper.signin_register(twin_two)

            write_token(twin_two, newer)
            write_flight_log(
                twin_two,
                [
                    {
                        "ts": keeper._format_timestamp(1001.0),
                        "type": "login.authorized",
                        "level": "info",
                        "data": {"token_prefix": "not-reported"},
                    },
                    {
                        "ts": keeper._format_timestamp(1001.1),
                        "type": "auth.token_saved",
                        "level": "info",
                    },
                    {
                        "ts": keeper._format_timestamp(1001.2),
                        "type": "auth.copilot_ready",
                        "level": "info",
                    },
                ],
            )
            status_before = keeper.signin_status()
            twin_two_status = next(
                item
                for item in status_before["registered"]
                if item["alias"] == "brainstem-2"
            )
            self.assertTrue(twin_two_status["drift"])
            self.assertEqual(twin_two_status["exchange_state"], "ok")

            clock[0] = 1002.0
            result = keeper.signin_sync()

            self.assertEqual(result["adopted_from"], "brainstem-2")
            self.assertEqual(result["generation"], 2)
            for brainstem_dir in (main, twin_one, twin_two):
                self.assertEqual(
                    (brainstem_dir / ".copilot_token").read_bytes(),
                    newer,
                )
            status_after = keeper.signin_status()
            self.assertFalse(
                any(item["drift"] for item in status_after["registered"])
            )

    def test_signin_sync_rejects_newer_login_when_exchange_is_failing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brainstem_home = root / ".brainstem"
            main = brainstem_home / "src" / "rapp_brainstem"
            twin = root / "twin"
            original = b"working-record\n"
            write_token(main, original)
            twin.mkdir()
            keeper = keeper_module.Keeper(
                brainstem_home=brainstem_home,
                now_func=lambda: 1000.0,
            )
            keeper.signin_adopt()
            keeper.signin_register(twin)
            write_token(twin, b"rejected-record\n")
            write_flight_log(
                twin,
                [
                    {
                        "ts": keeper._format_timestamp(1001.0),
                        "type": "login.authorized",
                        "level": "info",
                    },
                    {
                        "ts": keeper._format_timestamp(1001.1),
                        "type": "auth.copilot_exchange_error",
                        "level": "error",
                        "data": {"status": 401},
                    },
                ],
            )

            result = keeper.signin_sync()

            self.assertIsNone(result["adopted_from"])
            self.assertEqual(result["generation"], 1)
            self.assertEqual((twin / ".copilot_token").read_bytes(), original)
            self.assertEqual(
                keeper.signin_holder_path.read_bytes(),
                original,
            )

    def test_signin_unregister_leaves_token_file_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brainstem_home = root / ".brainstem"
            main = brainstem_home / "src" / "rapp_brainstem"
            twin = root / "twin"
            token_record = b"retained-record\n"
            write_token(main, token_record)
            twin.mkdir()
            keeper = keeper_module.Keeper(
                brainstem_home=brainstem_home,
                now_func=lambda: 1000.0,
            )
            keeper.signin_adopt()
            keeper.signin_register(twin)

            result = keeper.signin_unregister(twin)

            self.assertFalse(result["registered"])
            self.assertFalse(result["token_removed"])
            self.assertIn("left in place", result["message"])
            self.assertEqual((twin / ".copilot_token").read_bytes(), token_record)
            self.assertEqual(
                [item["alias"] for item in keeper.signin_status()["registered"]],
                ["main"],
            )

    def test_signin_cli_never_prints_or_logs_credential_text_or_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brainstem_home = root / ".brainstem"
            main = brainstem_home / "src" / "rapp_brainstem"
            twin = root / "twin"
            sentinel = "KEEPER_CREDENTIAL_SENTINEL_DO_NOT_REPORT"
            write_token(
                main,
                json.dumps(
                    {
                        "access_token": sentinel,
                        "saved_at": 1,
                    }
                ).encode("utf-8"),
            )
            twin.mkdir()
            write_flight_log(
                main,
                [
                    {
                        "ts": "2020-01-01T00:00:00Z",
                        "type": "login.authorized",
                        "data": {"token_prefix": sentinel},
                    }
                ],
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            commands = (
                ["signin", "adopt"],
                ["signin", "register", str(twin)],
                ["signin", "status"],
                ["signin", "sync"],
                ["signin", "unregister", str(twin)],
            )
            with mock.patch.dict(
                os.environ,
                {"BRAINSTEM_HOME": str(brainstem_home)},
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                for command in commands:
                    self.assertEqual(keeper_module.main(command), 0)

            keeper = keeper_module.Keeper(brainstem_home=brainstem_home)
            log_text = (
                keeper.log_path.read_text(encoding="utf-8")
                if keeper.log_path.exists()
                else ""
            )
            all_reports = stdout.getvalue() + stderr.getvalue() + log_text
            self.assertNotIn(sentinel, all_reports)
            self.assertNotIn(str(main), all_reports)
            self.assertNotIn(str(twin), all_reports)

    def test_watch_syncs_signin_before_start_each_cycle(self):
        class WatchKeeper(keeper_module.Keeper):
            def __init__(self, root):
                super().__init__(
                    brainstem_home=Path(root) / ".brainstem",
                    now_func=lambda: 10.0,
                    sleep_func=self.stop_after_cycle,
                )
                self.calls = []

            def stop_after_cycle(self, _interval):
                raise StopIteration()

            def _write_log(self, _message, display=True):
                return

            def _signin_holder_present(self):
                return True

            def signin_sync(self):
                self.calls.append("sync")
                return {
                    "generation": 1,
                    "adopted_from": None,
                    "warnings": [],
                }

            def start(self):
                self.calls.append("start")

            def load_state(self):
                state = self._default_state()
                state["last_upgrade_check"] = 10.0
                return state

        with tempfile.TemporaryDirectory() as temporary:
            keeper = WatchKeeper(temporary)
            with self.assertRaises(StopIteration):
                keeper.watch(1)
            self.assertEqual(keeper.calls, ["sync", "start"])

    def test_uninstall_removes_only_keeper_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keeper = UninstallKeeper(brainstem_home=root / ".brainstem")
            grail_file = keeper.kernel_dir / "brainstem.py"
            grail_file.parent.mkdir(parents=True)
            grail_file.write_text("unchanged\n", encoding="utf-8")
            write_token(keeper.kernel_dir, b"main-record\n")
            twin = root / "twin"
            twin.mkdir()
            keeper.signin_adopt()
            keeper.signin_register(twin)
            twin_token = twin / ".copilot_token"
            with contextlib.redirect_stdout(io.StringIO()):
                keeper.uninstall()
            self.assertFalse(keeper.keeper_home.exists())
            self.assertEqual(
                grail_file.read_text(encoding="utf-8"), "unchanged\n"
            )
            self.assertEqual(
                (keeper.kernel_dir / ".copilot_token").read_bytes(),
                b"main-record\n",
            )
            self.assertEqual(twin_token.read_bytes(), b"main-record\n")


if __name__ == "__main__":
    unittest.main()
