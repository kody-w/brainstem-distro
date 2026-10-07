#!/usr/bin/env python3
"""Keep an unchanged RAPP Brainstem install healthy and recoverable."""

import argparse
import contextlib
import datetime
import errno
import hashlib
import json
import math
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


INSTALLER_URL = "https://kody-w.github.io/rapp-installer/install.sh"
GRAIL_URL = "https://github.com/kody-w/rapp-installer"
DEFAULT_PORT = 7071
DEFAULT_INTERVAL = 30
UPGRADE_INTERVAL = 24 * 60 * 60
HISTORY_LIMIT = 20
SERVICE_LABEL = "io.rapp.keeper"
SYSTEMD_SERVICE = "rapp-keeper.service"
SIGNIN_MANIFEST_VERSION = 1
SIGNIN_FLIGHT_LOG = ".brainstem_book.json"
SIGNIN_SUCCESS_EVENTS = (
    "auth.copilot_ready",
    "auth.copilot_restored",
    "auth.retry_ok",
)
SIGNIN_FAILURE_EVENTS = (
    "auth.copilot_exchange_failed",
    "auth.copilot_exchange_error",
    "auth.copilot_no_token",
    "auth.no_copilot_access",
)
SIGNIN_GENERATION_EVENTS = (
    "login.authorized",
    "auth.token_saved",
)
SIGNIN_CARRY_NAMES = (
    ".brainstem_secret",
    ".copilot_pending",
    ".copilot_session",
    ".copilot_token",
)
SENSITIVE_KERNEL_NAMES = {
    ".brainstem_data",
    ".brainstem_model",
    ".env",
    *SIGNIN_CARRY_NAMES,
}


class KeeperError(RuntimeError):
    """An expected keeper failure that should be shown to the user."""


def normalize_version(value):
    if value is None:
        raise KeeperError("A Brainstem version is required.")
    version = str(value).strip()
    for prefix in ("brainstem-v", "brainstem-", "v"):
        if version.startswith(prefix):
            version = version[len(prefix):]
            break
    if not re.fullmatch(r"\d+(?:\.\d+)+", version):
        raise KeeperError("Invalid Brainstem version: {!r}".format(value))
    return ".".join(str(int(part)) for part in version.split("."))


def compare_versions(left, right):
    left_parts = [int(part) for part in normalize_version(left).split(".")]
    right_parts = [int(part) for part in normalize_version(right).split(".")]
    width = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (width - len(left_parts)))
    right_parts.extend([0] * (width - len(right_parts)))
    return (left_parts > right_parts) - (left_parts < right_parts)


def tail_text(value, limit=2000):
    text = str(value or "").strip()
    return text[-limit:]


def shell_join(parts):
    return " ".join(shlex.quote(str(part)) for part in parts)


class Keeper:
    def __init__(
        self,
        brainstem_home=None,
        repo_root=None,
        urlopen_func=None,
        sleep_func=None,
        now_func=None,
    ):
        configured_home = brainstem_home or os.environ.get("BRAINSTEM_HOME")
        self.brainstem_home = Path(
            configured_home or (Path.home() / ".brainstem")
        ).expanduser()
        self.kernel_dir = self.brainstem_home / "src" / "rapp_brainstem"
        self.grail_venv = self.brainstem_home / "venv"
        self.launcher = Path.home() / ".local" / "bin" / "brainstem"
        self.keeper_home = self.brainstem_home / "keeper"
        self.carry_dir = self.keeper_home / "carry"
        self.signin_dir = self.keeper_home / "signin"
        self.signin_holder_path = self.signin_dir / ".copilot_token"
        self.signin_generation_path = self.signin_dir / "generation.json"
        self.signin_manifest_path = self.signin_dir / "manifest.json"
        self.safe_root = self.keeper_home / "safe"
        self.safe_venv = self.keeper_home / "safe-venv"
        self.state_path = self.keeper_home / "state.json"
        self.log_path = self.keeper_home / "keeper.log"
        self.lock_path = self.keeper_home / "keeper.lock"
        self.installed_pid_path = self.keeper_home / "installed.pid"
        self.safe_pid_path = self.keeper_home / "safe.pid"
        self.installed_script_path = self.keeper_home / "keeper.py"
        self.installed_kernel_path = self.keeper_home / "kernel.json"
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[1])
        self.urlopen = urlopen_func or urllib.request.urlopen
        self.sleep = sleep_func or time.sleep
        self.now = now_func or time.time
        self.port = int(os.environ.get("KEEPER_PORT", str(DEFAULT_PORT)))
        self.base_url = "http://127.0.0.1:{}".format(self.port)
        self.health_timeout = float(os.environ.get("KEEPER_HEALTH_TIMEOUT", "45"))
        self.installer_timeout = float(
            os.environ.get("KEEPER_INSTALLER_TIMEOUT", "900")
        )

    def _ensure_keeper_home(self):
        self.keeper_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.keeper_home.chmod(0o700)
        except OSError:
            pass

    def _ensure_carry_dir(self):
        self._ensure_keeper_home()
        self.carry_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.carry_dir.chmod(0o700)
        except OSError:
            pass

    def _ensure_signin_dir(self):
        try:
            self._ensure_keeper_home()
            if os.path.lexists(str(self.signin_dir)) and (
                self.signin_dir.is_symlink()
                or not self.signin_dir.is_dir()
            ):
                raise KeeperError("The shared sign-in directory is invalid.")
            self.signin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.signin_dir.chmod(0o700)
        except OSError:
            raise KeeperError("Cannot create the shared sign-in directory.")

    def _secure_existing_signin_dir(self):
        if not os.path.lexists(str(self.signin_dir)):
            return
        if self.signin_dir.is_symlink() or not self.signin_dir.is_dir():
            raise KeeperError("The shared sign-in directory is invalid.")
        try:
            self.signin_dir.chmod(0o700)
        except OSError:
            raise KeeperError("Cannot secure the shared sign-in directory.")

    def _format_timestamp(self, epoch):
        value = datetime.datetime.fromtimestamp(
            float(epoch), datetime.timezone.utc
        ).isoformat()
        return value.replace("+00:00", "Z")

    def _timestamp(self):
        return self._format_timestamp(self.now())

    def _parse_timestamp(self, value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()

    def _default_state(self):
        return {
            "last_good": None,
            "failed": {},
            "safe_version": None,
            "history": [],
        }

    def load_state(self):
        if not self.state_path.exists():
            return self._default_state()
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError) as exc:
            raise KeeperError(
                "Cannot read keeper state {}: {}".format(self.state_path, exc)
            )
        if not isinstance(state, dict):
            raise KeeperError("Keeper state must be a JSON object.")
        merged = self._default_state()
        merged.update(state)
        if not isinstance(merged["failed"], dict):
            raise KeeperError("Keeper state field 'failed' must be an object.")
        if not isinstance(merged["history"], list):
            raise KeeperError("Keeper state field 'history' must be an array.")
        merged["history"] = merged["history"][-HISTORY_LIMIT:]
        return merged

    def save_state(self, state):
        self._ensure_keeper_home()
        state = dict(state)
        state["history"] = list(state.get("history", []))[-HISTORY_LIMIT:]
        temporary = self.state_path.with_name(
            "{}.tmp-{}".format(self.state_path.name, os.getpid())
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(str(temporary), str(self.state_path))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def record_event(self, state, event, **details):
        entry = {"time": self._timestamp(), "event": event}
        if details:
            entry["details"] = details
        state.setdefault("history", []).append(entry)
        state["history"] = state["history"][-HISTORY_LIMIT:]

    def _write_log(self, message, display=True):
        self._ensure_keeper_home()
        line = "{} {}\n".format(self._timestamp(), str(message).rstrip())
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        try:
            self.log_path.chmod(0o600)
        except OSError:
            pass
        if display:
            print(str(message), flush=True)

    def _fsync_directory(self, directory):
        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write_private_json(self, destination, payload):
        temporary = destination.with_name(
            ".{}.tmp-{}-{}".format(
                destination.name.lstrip("."),
                os.getpid(),
                time.time_ns(),
            )
        )
        handle = None
        try:
            fd = os.open(
                str(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            handle = os.fdopen(fd, "w", encoding="utf-8")
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.chmod(str(temporary), 0o600)
            os.replace(str(temporary), str(destination))
            self._fsync_directory(destination.parent)
        except OSError:
            raise KeeperError("Cannot write shared sign-in metadata.")
        finally:
            if handle is not None:
                handle.close()
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _copy_private_file(self, source, destination, mtime_ns=None):
        temporary = destination.with_name(
            ".{}.tmp-{}-{}".format(
                destination.name.lstrip("."),
                os.getpid(),
                time.time_ns(),
            )
        )
        source_handle = None
        destination_handle = None
        try:
            source_handle = source.open("rb")
            fd = os.open(
                str(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            destination_handle = os.fdopen(fd, "wb")
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
            destination_handle.close()
            destination_handle = None
            os.chmod(str(temporary), 0o600)
            if mtime_ns is not None:
                os.utime(
                    str(temporary),
                    ns=(int(mtime_ns), int(mtime_ns)),
                )
            os.replace(str(temporary), str(destination))
            self._fsync_directory(destination.parent)
        finally:
            if source_handle is not None:
                source_handle.close()
            if destination_handle is not None:
                destination_handle.close()
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _carry_signin_files(self):
        existing = [
            name
            for name in SIGNIN_CARRY_NAMES
            if (self.kernel_dir / name).is_file()
        ]
        if not existing:
            return
        self._ensure_carry_dir()
        for name in existing:
            source = self.kernel_dir / name
            destination = self.carry_dir / name
            try:
                self._copy_private_file(source, destination)
            except FileNotFoundError:
                continue

    def _restore_carried_signin_files(self, remove_restored):
        if not self.carry_dir.is_dir():
            return True
        restored = True
        for name in SIGNIN_CARRY_NAMES:
            carried = self.carry_dir / name
            if not carried.is_file():
                continue
            destination = self.kernel_dir / name
            if not destination.exists():
                if not self.kernel_dir.is_dir():
                    restored = False
                    continue
                try:
                    os.link(str(carried), str(destination))
                except FileExistsError:
                    pass
                except OSError:
                    restored = False
                    continue
            if destination.exists() and remove_restored:
                try:
                    carried.unlink()
                except OSError:
                    restored = False
        if remove_restored:
            try:
                self.carry_dir.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                restored = False
        return restored

    def _signin_guard(self, stop_event):
        while not stop_event.wait(0.05):
            self._restore_carried_signin_files(remove_restored=False)

    def _finish_signin_guard(self, stop_event, guard_thread):
        stop_event.set()
        guard_thread.join(timeout=2)
        return self._restore_carried_signin_files(remove_restored=True)

    def _default_signin_manifest(self):
        return {
            "version": SIGNIN_MANIFEST_VERSION,
            "brainstems": [],
        }

    def _load_signin_manifest(self):
        self._secure_existing_signin_dir()
        if not self.signin_manifest_path.exists():
            return self._default_signin_manifest()
        if self.signin_manifest_path.is_symlink():
            raise KeeperError("The shared sign-in manifest is invalid.")
        try:
            with self.signin_manifest_path.open(
                "r", encoding="utf-8"
            ) as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            raise KeeperError("Cannot read the shared sign-in manifest.")
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != SIGNIN_MANIFEST_VERSION
            or not isinstance(manifest.get("brainstems"), list)
        ):
            raise KeeperError("The shared sign-in manifest is invalid.")
        aliases = set()
        paths = set()
        for entry in manifest["brainstems"]:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("alias"), str)
                or not entry["alias"]
                or not isinstance(entry.get("path"), str)
                or not Path(entry["path"]).is_absolute()
            ):
                raise KeeperError("The shared sign-in manifest is invalid.")
            if entry["alias"] in aliases or entry["path"] in paths:
                raise KeeperError("The shared sign-in manifest is invalid.")
            aliases.add(entry["alias"])
            paths.add(entry["path"])
        try:
            self.signin_manifest_path.chmod(0o600)
        except OSError:
            raise KeeperError("Cannot secure the shared sign-in manifest.")
        return manifest

    def _save_signin_manifest(self, manifest):
        self._ensure_signin_dir()
        self._atomic_write_private_json(self.signin_manifest_path, manifest)

    def _canonical_brainstem_dir(self, value, must_exist=True):
        try:
            path = Path(value).expanduser().resolve(strict=must_exist)
        except (OSError, RuntimeError):
            raise KeeperError("The Brainstem directory is unavailable.")
        if must_exist and not path.is_dir():
            raise KeeperError("The Brainstem directory is unavailable.")
        return path

    def _main_brainstem_dir(self):
        return self.kernel_dir.expanduser().resolve(strict=False)

    def _manifest_entry_for_path(self, manifest, brainstem_dir):
        text = str(brainstem_dir)
        for entry in manifest["brainstems"]:
            if entry["path"] == text:
                return entry
        return None

    def _next_brainstem_alias(self, manifest, brainstem_dir):
        used = {entry["alias"] for entry in manifest["brainstems"]}
        if brainstem_dir == self._main_brainstem_dir() and "main" not in used:
            return "main"
        index = 1
        while "brainstem-{}".format(index) in used:
            index += 1
        return "brainstem-{}".format(index)

    def _ensure_manifest_entry(self, manifest, brainstem_dir):
        existing = self._manifest_entry_for_path(manifest, brainstem_dir)
        if existing is not None:
            return existing, False
        entry = {
            "alias": self._next_brainstem_alias(manifest, brainstem_dir),
            "path": str(brainstem_dir),
        }
        manifest["brainstems"].append(entry)
        return entry, True

    def _signin_entry_sort_key(self, entry):
        alias = entry["alias"]
        if alias == "main":
            return (0, 0)
        match = re.fullmatch(r"brainstem-(\d+)", alias)
        if match:
            return (1, int(match.group(1)))
        return (2, alias)

    def _load_signin_generation(self, required=False):
        self._secure_existing_signin_dir()
        token_exists = self.signin_holder_path.exists()
        generation_exists = self.signin_generation_path.exists()
        if not token_exists and not generation_exists:
            if required:
                raise KeeperError(
                    "No shared sign-in holder exists; run signin adopt first."
                )
            return None
        if not token_exists or not generation_exists:
            raise KeeperError("The shared sign-in holder is incomplete.")
        if (
            self.signin_holder_path.is_symlink()
            or self.signin_generation_path.is_symlink()
        ):
            raise KeeperError("The shared sign-in holder is invalid.")
        try:
            with self.signin_generation_path.open(
                "r", encoding="utf-8"
            ) as handle:
                generation = json.load(handle)
            holder_stat = self.signin_holder_path.stat()
        except (OSError, ValueError):
            raise KeeperError("Cannot read the shared sign-in holder.")
        parsed_generated_at = self._parse_timestamp(
            generation.get("generated_at_iso")
            if isinstance(generation, dict)
            else None
        )
        required_fields = {
            "version",
            "generation",
            "generated_at",
            "generated_at_iso",
            "source_alias",
            "token_size",
            "token_mtime_ns",
        }
        if (
            not isinstance(generation, dict)
            or set(generation) != required_fields
            or generation.get("version") != SIGNIN_MANIFEST_VERSION
            or isinstance(generation.get("generation"), bool)
            or not isinstance(generation.get("generation"), int)
            or generation["generation"] < 1
            or isinstance(generation.get("generated_at"), bool)
            or not isinstance(generation.get("generated_at"), (int, float))
            or not isinstance(generation.get("generated_at_iso"), str)
            or parsed_generated_at is None
            or abs(
                parsed_generated_at - float(generation["generated_at"])
            ) > 0.000001
            or not isinstance(generation.get("source_alias"), str)
            or not generation["source_alias"]
            or isinstance(generation.get("token_size"), bool)
            or not isinstance(generation.get("token_size"), int)
            or generation["token_size"] < 1
            or isinstance(generation.get("token_mtime_ns"), bool)
            or not isinstance(generation.get("token_mtime_ns"), int)
            or generation["token_mtime_ns"] < 1
            or not stat.S_ISREG(holder_stat.st_mode)
        ):
            raise KeeperError("The shared sign-in generation is invalid.")
        if (
            holder_stat.st_size != generation["token_size"]
            or holder_stat.st_mtime_ns != generation["token_mtime_ns"]
        ):
            raise KeeperError("The shared sign-in holder does not match its generation.")
        try:
            self.signin_holder_path.chmod(0o600)
            self.signin_generation_path.chmod(0o600)
        except OSError:
            raise KeeperError("Cannot secure the shared sign-in holder.")
        return generation

    def _write_signin_generation(
        self,
        source_token,
        source_alias,
        event_time=None,
    ):
        previous = self._load_signin_generation(required=False)
        if source_token == self.signin_holder_path:
            raise KeeperError("The holder cannot adopt itself.")
        try:
            source_lstat = os.lstat(str(source_token))
        except OSError:
            raise KeeperError(
                "The source Brainstem has no token record to adopt."
            )
        if (
            not stat.S_ISREG(source_lstat.st_mode)
            or source_lstat.st_size < 1
        ):
            raise KeeperError("The source Brainstem token record is invalid.")

        generation_number = (
            previous["generation"] + 1 if previous is not None else 1
        )
        previous_mtime_ns = (
            previous["token_mtime_ns"] if previous is not None else 0
        )
        generation_time = max(
            float(self.now()),
            float(event_time) if event_time is not None else 0.0,
        )
        generation_seconds = max(
            int(math.ceil(generation_time)),
            (previous_mtime_ns // 1000000000) + 1,
            1,
        )
        generation_mtime_ns = generation_seconds * 1000000000
        generation_time = generation_mtime_ns / 1000000000.0
        self._ensure_signin_dir()

        stable = False
        for _attempt in range(3):
            try:
                before = os.lstat(str(source_token))
            except OSError:
                break
            if not stat.S_ISREG(before.st_mode) or before.st_size < 1:
                break
            try:
                self._copy_private_file(
                    source_token,
                    self.signin_holder_path,
                    mtime_ns=generation_mtime_ns,
                )
            except OSError:
                raise KeeperError(
                    "Cannot write the shared sign-in holder."
                )
            try:
                after = os.lstat(str(source_token))
            except OSError:
                continue
            before_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            if before_identity == after_identity:
                stable = True
                break
        if not stable:
            raise KeeperError(
                "The source Brainstem token changed while it was being adopted."
            )

        try:
            holder_stat = self.signin_holder_path.stat()
        except OSError:
            raise KeeperError("Cannot read the shared sign-in holder.")
        generation = {
            "version": SIGNIN_MANIFEST_VERSION,
            "generation": generation_number,
            "generated_at": generation_time,
            "generated_at_iso": self._format_timestamp(generation_time),
            "source_alias": source_alias,
            "token_size": holder_stat.st_size,
            "token_mtime_ns": holder_stat.st_mtime_ns,
        }
        self._atomic_write_private_json(
            self.signin_generation_path,
            generation,
        )
        return generation

    def _project_signin_entry(self, entry, generation):
        brainstem_dir = Path(entry["path"])
        if not brainstem_dir.is_dir():
            raise KeeperError("Registered Brainstem is unavailable.")
        destination = brainstem_dir / ".copilot_token"
        try:
            self._copy_private_file(
                self.signin_holder_path,
                destination,
                mtime_ns=generation["token_mtime_ns"],
            )
            projected = destination.stat()
        except OSError:
            raise KeeperError("Registered Brainstem could not be updated.")
        if (
            projected.st_size != generation["token_size"]
            or projected.st_mtime_ns != generation["token_mtime_ns"]
            or stat.S_IMODE(projected.st_mode) != 0o600
        ):
            raise KeeperError("Projected token metadata is invalid.")

    def _project_signin_manifest(self, manifest, generation, entries=None):
        selected = list(entries or manifest["brainstems"])
        projected = []
        failures = []
        for entry in sorted(selected, key=self._signin_entry_sort_key):
            try:
                self._project_signin_entry(entry, generation)
            except (KeeperError, OSError):
                failures.append(entry["alias"])
            else:
                projected.append(entry["alias"])
        if failures:
            raise KeeperError(
                "Shared sign-in generation {} could not reach: {}.".format(
                    generation["generation"],
                    ", ".join(failures),
                )
            )
        return projected

    def _flight_signin_state(self, brainstem_dir, holder_time):
        state = {
            "log_state": "missing",
            "newer_login_authorized_at": None,
            "newer_signin_at": None,
            "candidate_at": None,
            "exchange_state": "unknown",
            "error": False,
        }
        flight_path = brainstem_dir / SIGNIN_FLIGHT_LOG
        if not flight_path.exists():
            return state
        try:
            with flight_path.open("r", encoding="utf-8") as handle:
                events = json.load(handle)
        except (OSError, ValueError):
            state["log_state"] = "invalid"
            state["error"] = True
            return state
        if not isinstance(events, list):
            state["log_state"] = "invalid"
            state["error"] = True
            return state
        state["log_state"] = "ok"

        latest_authorized = None
        latest_signin = None
        malformed_relevant_event = False
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type not in (
                SIGNIN_GENERATION_EVENTS
                + SIGNIN_SUCCESS_EVENTS
                + SIGNIN_FAILURE_EVENTS
            ):
                continue
            event_time = self._parse_timestamp(event.get("ts"))
            if event_time is None:
                malformed_relevant_event = True
                continue
            if event_type == "login.authorized":
                if latest_authorized is None or event_time > latest_authorized:
                    latest_authorized = event_time
            if event_type in SIGNIN_GENERATION_EVENTS:
                candidate = (event_time, index, event_type)
                if latest_signin is None or candidate[:2] > latest_signin[:2]:
                    latest_signin = candidate

        if malformed_relevant_event:
            state["log_state"] = "invalid"
            state["error"] = True
        if latest_authorized is not None and (
            holder_time is not None and latest_authorized > holder_time
        ):
            state["newer_login_authorized_at"] = self._format_timestamp(
                latest_authorized
            )
        if latest_signin is None:
            return state

        signin_time, signin_index, _event_type = latest_signin
        exchange_state = "unknown"
        for event in events[signin_index + 1:]:
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type in SIGNIN_SUCCESS_EVENTS:
                exchange_state = "ok"
            elif event_type in SIGNIN_FAILURE_EVENTS:
                exchange_state = "failing"
        state["exchange_state"] = exchange_state
        if holder_time is not None and signin_time > holder_time:
            state["newer_signin_at"] = self._format_timestamp(signin_time)
            if exchange_state != "failing" and not state["error"]:
                state["candidate_at"] = signin_time
        return state

    def _signin_token_status(self, brainstem_dir, generation):
        token_path = brainstem_dir / ".copilot_token"
        try:
            token_stat = os.lstat(str(token_path))
        except FileNotFoundError:
            return {
                "state": "missing",
                "matches_holder": False,
                "modified_at": None,
            }
        except OSError:
            return {
                "state": "unavailable",
                "matches_holder": False,
                "modified_at": None,
            }
        if not stat.S_ISREG(token_stat.st_mode):
            return {
                "state": "invalid",
                "matches_holder": False,
                "modified_at": None,
            }
        matches = bool(
            generation is not None
            and token_stat.st_size == generation["token_size"]
            and token_stat.st_mtime_ns == generation["token_mtime_ns"]
        )
        return {
            "state": "current" if matches else "different",
            "matches_holder": matches,
            "modified_at": self._format_timestamp(token_stat.st_mtime),
        }

    def signin_status(self):
        with self.lock():
            manifest = self._load_signin_manifest()
            holder_error = None
            try:
                generation = self._load_signin_generation(required=False)
            except KeeperError as exc:
                generation = None
                holder_error = str(exc)
            if generation is None:
                holder = {
                    "state": "invalid" if holder_error else "absent",
                    "generation": None,
                    "generated_at": None,
                    "source": None,
                }
                if holder_error:
                    holder["error"] = holder_error
                holder_time = None
            else:
                holder = {
                    "state": "ready",
                    "generation": generation["generation"],
                    "generated_at": generation["generated_at_iso"],
                    "source": generation["source_alias"],
                }
                holder_time = generation["generated_at"]

            brainstems = []
            for entry in sorted(
                manifest["brainstems"], key=self._signin_entry_sort_key
            ):
                brainstem_dir = Path(entry["path"])
                token = self._signin_token_status(brainstem_dir, generation)
                flight = self._flight_signin_state(
                    brainstem_dir,
                    holder_time,
                )
                brainstems.append(
                    {
                        "alias": entry["alias"],
                        "token_state": token["state"],
                        "matches_holder": token["matches_holder"],
                        "generation": (
                            generation["generation"]
                            if token["matches_holder"]
                            else None
                        ),
                        "token_modified_at": token["modified_at"],
                        "flight_log": flight["log_state"],
                        "newer_login_authorized_at": (
                            flight["newer_login_authorized_at"]
                        ),
                        "newer_signin_at": flight["newer_signin_at"],
                        "exchange_state": flight["exchange_state"],
                        "drift": bool(
                            flight["newer_login_authorized_at"]
                        ),
                    }
                )
            return {
                "holder": holder,
                "registered": brainstems,
            }

    def signin_adopt(self, source=None):
        with self.lock():
            brainstem_dir = self._canonical_brainstem_dir(
                source or self.kernel_dir
            )
            manifest = self._load_signin_manifest()
            entry, _added = self._ensure_manifest_entry(
                manifest,
                brainstem_dir,
            )
            generation = self._write_signin_generation(
                brainstem_dir / ".copilot_token",
                entry["alias"],
            )
            self._save_signin_manifest(manifest)
            projected = self._project_signin_manifest(
                manifest,
                generation,
            )
            return {
                "adopted_from": entry["alias"],
                "generation": generation["generation"],
                "generated_at": generation["generated_at_iso"],
                "projected": projected,
            }

    def signin_register(self, value):
        with self.lock():
            generation = self._load_signin_generation(required=True)
            brainstem_dir = self._canonical_brainstem_dir(value)
            manifest = self._load_signin_manifest()
            entry, added = self._ensure_manifest_entry(
                manifest,
                brainstem_dir,
            )
            self._project_signin_entry(entry, generation)
            if added:
                self._save_signin_manifest(manifest)
            return {
                "alias": entry["alias"],
                "generation": generation["generation"],
                "projected": True,
            }

    def signin_unregister(self, value):
        with self.lock():
            brainstem_dir = self._canonical_brainstem_dir(
                value,
                must_exist=False,
            )
            manifest = self._load_signin_manifest()
            entry = self._manifest_entry_for_path(
                manifest,
                brainstem_dir,
            )
            if entry is None:
                raise KeeperError("That Brainstem is not registered.")
            manifest["brainstems"] = [
                item
                for item in manifest["brainstems"]
                if item["path"] != entry["path"]
            ]
            self._save_signin_manifest(manifest)
            return {
                "alias": entry["alias"],
                "registered": False,
                "token_removed": False,
                "message": "Token file was left in place.",
            }

    def signin_sync(self):
        with self.lock():
            generation = self._load_signin_generation(required=True)
            manifest = self._load_signin_manifest()
            candidates = []
            warnings = []
            for entry in manifest["brainstems"]:
                brainstem_dir = Path(entry["path"])
                flight = self._flight_signin_state(
                    brainstem_dir,
                    generation["generated_at"],
                )
                if flight["error"]:
                    warnings.append(entry["alias"])
                if flight["candidate_at"] is None:
                    continue
                token_path = brainstem_dir / ".copilot_token"
                try:
                    token_stat = os.lstat(str(token_path))
                except OSError:
                    warnings.append(entry["alias"])
                    continue
                if (
                    not stat.S_ISREG(token_stat.st_mode)
                    or token_stat.st_size < 1
                ):
                    warnings.append(entry["alias"])
                    continue
                candidates.append(
                    (
                        flight["candidate_at"],
                        entry["alias"],
                        token_path,
                    )
                )

            adopted_from = None
            if candidates:
                candidate_time, adopted_from, source_token = max(
                    candidates,
                    key=lambda item: (item[0], item[1]),
                )
                generation = self._write_signin_generation(
                    source_token,
                    adopted_from,
                    event_time=candidate_time,
                )

            projected = self._project_signin_manifest(
                manifest,
                generation,
            )
            return {
                "generation": generation["generation"],
                "generated_at": generation["generated_at_iso"],
                "adopted_from": adopted_from,
                "projected": projected,
                "warnings": sorted(set(warnings)),
            }

    def _signin_holder_present(self):
        return (
            self.signin_holder_path.exists()
            or self.signin_generation_path.exists()
        )

    @contextlib.contextmanager
    def lock(self):
        self._ensure_keeper_home()
        try:
            import fcntl
        except ImportError as exc:
            raise KeeperError(
                "keeper requires a POSIX file lock; Windows is not supported."
            ) from exc
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def installed_version(self):
        version_path = self.kernel_dir / "VERSION"
        try:
            return normalize_version(version_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise KeeperError(
                "Cannot read installed Brainstem version: {}".format(exc)
            )

    def default_target(self):
        candidates = []
        configured = os.environ.get("KEEPER_KERNEL_JSON")
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.extend(
            [
                self.installed_kernel_path,
                self.repo_root / "plugin" / "kernel.json",
            ]
        )
        kernel_path = next((path for path in candidates if path.is_file()), None)
        if kernel_path is None:
            raise KeeperError(
                "No plugin/kernel.json was found; use upgrade --to <version>."
            )
        try:
            data = json.loads(kernel_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KeeperError(
                "Cannot read kernel pin {}: {}".format(kernel_path, exc)
            )
        if not data.get("version"):
            raise KeeperError(
                "{} has no 'version' pin; use upgrade --to <version>.".format(
                    kernel_path
                )
            )
        return normalize_version(data["version"])

    def _http_json(self, request, timeout):
        try:
            with self.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
                code = response.getcode()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
                code = exc.code
            finally:
                exc.close()
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        return code, data

    def probe_health(self, expected_version=None, timeout=2):
        result = {
            "healthy": False,
            "status": None,
            "version": None,
            "chat_status": None,
            "chat_error": None,
            "reason": None,
        }
        try:
            get_request = urllib.request.Request(
                self.base_url + "/health",
                headers={"Accept": "application/json"},
                method="GET",
            )
            get_code, health_data = self._http_json(get_request, timeout)
            if isinstance(health_data, dict):
                result["status"] = health_data.get("status")
                result["version"] = health_data.get("version")
            if get_code != 200 or not isinstance(health_data, dict):
                result["reason"] = "GET /health did not return 200 JSON"
                return result
            if result["status"] not in ("ok", "unauthenticated"):
                result["reason"] = "GET /health returned status {!r}".format(
                    result["status"]
                )
                return result
            if not result["version"]:
                result["reason"] = "GET /health omitted version"
                return result
            if expected_version is not None:
                expected = normalize_version(expected_version)
                try:
                    observed = normalize_version(result["version"])
                except KeeperError:
                    result["reason"] = "GET /health returned an invalid version"
                    return result
                if observed != expected:
                    result["reason"] = "expected version {}, observed {}".format(
                        expected, observed
                    )
                    return result

            chat_request = urllib.request.Request(
                self.base_url + "/chat",
                data=b"{}",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            chat_code, chat_data = self._http_json(chat_request, timeout)
            result["chat_status"] = chat_code
            if isinstance(chat_data, dict):
                result["chat_error"] = chat_data.get("error")
            if (
                chat_code != 400
                or not isinstance(chat_data, dict)
                or not chat_data.get("error")
            ):
                result["reason"] = "POST /chat {} did not return 400 JSON error"
                return result
            result["healthy"] = True
            return result
        except (OSError, urllib.error.URLError, socket.timeout) as exc:
            result["reason"] = str(exc)
            return result

    def _wait_for_health(self, expected_version, process=None):
        deadline = time.monotonic() + self.health_timeout
        last_result = None
        while time.monotonic() < deadline:
            last_result = self.probe_health(expected_version, timeout=2)
            if last_result["healthy"]:
                return last_result
            if process is not None and process.poll() is not None:
                last_result["reason"] = "server process exited with code {}".format(
                    process.returncode
                )
                return last_result
            self.sleep(0.5)
        if last_result is None:
            last_result = self.probe_health(expected_version, timeout=2)
        last_result["reason"] = "health timeout: {}".format(
            last_result.get("reason") or "no healthy response"
        )
        return last_result

    def _read_pid(self, path):
        try:
            return int(path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, OSError, ValueError):
            return None

    def _pid_alive(self, pid):
        if not pid or pid <= 1:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError as exc:
            return exc.errno == errno.EPERM

    def _pid_is_brainstem(self, pid):
        proc_cmdline = Path("/proc") / str(pid) / "cmdline"
        try:
            command = proc_cmdline.read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
            return "brainstem.py" in command
        except OSError:
            pass
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return "brainstem.py" in result.stdout
        except (OSError, subprocess.SubprocessError):
            return False

    def _stop_pidfile(self, path):
        pid = self._read_pid(path)
        if not pid:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        if self._pid_alive(pid) and self._pid_is_brainstem(pid):
            self._terminate_pid(pid)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _terminate_pid(self, pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self._pid_alive(pid):
            self.sleep(0.1)
        if self._pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def _stop_managed_processes(self):
        self._stop_pidfile(self.safe_pid_path)
        self._stop_pidfile(self.installed_pid_path)

    def _linux_port_pids(self):
        if not Path("/proc/net/tcp").exists():
            return set()
        socket_inodes = set()
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            try:
                lines = table.read_text(encoding="ascii").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 10 or fields[3] != "0A":
                    continue
                try:
                    port = int(fields[1].rsplit(":", 1)[1], 16)
                except (IndexError, ValueError):
                    continue
                if port == self.port:
                    socket_inodes.add(fields[9])
        if not socket_inodes:
            return set()
        pids = set()
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            fd_dir = proc_dir / "fd"
            try:
                descriptors = list(fd_dir.iterdir())
            except OSError:
                continue
            for descriptor in descriptors:
                try:
                    target = os.readlink(str(descriptor))
                except OSError:
                    continue
                match = re.fullmatch(r"socket:\[(\d+)\]", target)
                if match and match.group(1) in socket_inodes:
                    pids.add(int(proc_dir.name))
                    break
        return pids

    def _port_pids(self):
        pids = self._linux_port_pids()
        if pids:
            return pids
        lsof = shutil.which("lsof")
        if not lsof:
            return set()
        try:
            result = subprocess.run(
                [
                    lsof,
                    "-nP",
                    "-t",
                    "-iTCP:{}".format(self.port),
                    "-sTCP:LISTEN",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        return {
            int(line)
            for line in result.stdout.splitlines()
            if line.strip().isdigit()
        }

    def _stop_port_listeners(self):
        for pid in sorted(self._port_pids()):
            if pid in (os.getpid(), os.getppid()) or pid <= 1:
                continue
            self._write_log(
                "Stopping process {} on port {}.".format(pid, self.port)
            )
            self._terminate_pid(pid)

    def _adopt_installed_server(self, expected_version, health=None):
        health = health or self.probe_health(expected_version, timeout=2)
        if not health.get("healthy"):
            return None
        candidates = sorted(
            pid
            for pid in self._port_pids()
            if pid not in (os.getpid(), os.getppid()) and pid > 1
        )
        if not candidates:
            return None
        pid = candidates[0]
        self._write_pid(self.installed_pid_path, pid)
        try:
            self.safe_pid_path.unlink()
        except FileNotFoundError:
            pass
        return pid

    def _write_pid(self, path, pid):
        self._ensure_keeper_home()
        temporary = path.with_name("{}.tmp-{}".format(path.name, os.getpid()))
        temporary.write_text("{}\n".format(pid), encoding="ascii")
        os.replace(str(temporary), str(path))

    def _launch_tree(self, kind, python_path, tree, environment, expected_version):
        brainstem_path = tree / "brainstem.py"
        if not Path(python_path).is_file() or not brainstem_path.is_file():
            return {
                "healthy": False,
                "reason": "{} runtime is incomplete".format(kind),
            }
        self._stop_managed_processes()
        self._stop_port_listeners()
        self._ensure_keeper_home()
        self._write_log(
            "Starting {} Brainstem {}.".format(kind, expected_version)
        )
        log_handle = self.log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [str(python_path), str(brainstem_path)],
                cwd=str(tree),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            log_handle.close()
            return {"healthy": False, "reason": str(exc)}
        finally:
            if not log_handle.closed:
                log_handle.close()
        pid_path = self.safe_pid_path if kind == "safe" else self.installed_pid_path
        self._write_pid(pid_path, process.pid)
        health = self._wait_for_health(expected_version, process=process)
        if not health["healthy"]:
            self._stop_pidfile(pid_path)
        return health

    def launch_installed(self, expected_version):
        current = self.probe_health(expected_version, timeout=2)
        if current["healthy"]:
            pid = self._adopt_installed_server(expected_version, current)
            if pid is not None:
                return current
            current["healthy"] = False
            current["reason"] = "healthy installed server PID could not be adopted"
        python_path = self.grail_venv / "bin" / "python"
        environment = os.environ.copy()
        environment["PORT"] = str(self.port)
        environment["PYTHONUNBUFFERED"] = "1"
        return self._launch_tree(
            "installed",
            python_path,
            self.kernel_dir,
            environment,
            expected_version,
        )

    def _python_usable(self, python_path):
        if not Path(python_path).is_file():
            return False
        try:
            result = subprocess.run(
                [
                    str(python_path),
                    "-c",
                    "import flask, flask_cors, requests, dotenv, pyzipper",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _tracked_kernel_files(self):
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.brainstem_home / "src"),
                    "ls-files",
                    "-z",
                    "--",
                    "rapp_brainstem",
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeeperError("Cannot list tracked kernel files: {}".format(exc))
        if result.returncode != 0:
            raise KeeperError(
                "Cannot list tracked kernel files: {}".format(
                    result.stderr.decode("utf-8", "replace").strip()
                )
            )
        paths = []
        for raw_path in result.stdout.split(b"\0"):
            if not raw_path:
                continue
            text = raw_path.decode("utf-8", "surrogateescape")
            prefix = "rapp_brainstem/"
            if text.startswith(prefix):
                paths.append(Path(text[len(prefix):]))
        if not paths:
            raise KeeperError("The installed grail has no tracked kernel files.")
        return paths

    def _copy_safe_tree(self, version):
        destination = self.safe_root / version
        if destination.is_dir():
            return destination
        self.safe_root.mkdir(parents=True, exist_ok=True)
        temporary = self.safe_root / ".build-{}-{}".format(version, os.getpid())
        if temporary.exists():
            shutil.rmtree(str(temporary))
        temporary.mkdir(parents=True)
        try:
            for relative in self._tracked_kernel_files():
                if (
                    any(part in SENSITIVE_KERNEL_NAMES for part in relative.parts)
                    or ".." in relative.parts
                ):
                    continue
                source = self.kernel_dir / relative
                target = temporary / relative
                if not source.exists() and not source.is_symlink():
                    raise KeeperError(
                        "Tracked kernel file disappeared: {}".format(source)
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_symlink():
                    target.symlink_to(os.readlink(str(source)))
                elif source.is_file():
                    shutil.copy2(str(source), str(target))
            copied_version = normalize_version(
                (temporary / "VERSION").read_text(encoding="utf-8")
            )
            if copied_version != version:
                raise KeeperError(
                    "Safe copy version mismatch: expected {}, copied {}".format(
                        version, copied_version
                    )
                )
            os.replace(str(temporary), str(destination))
        except Exception:
            if temporary.exists():
                shutil.rmtree(str(temporary))
            raise
        return destination

    def _safe_requirements_hash(self, tree):
        requirements = tree / "requirements.txt"
        try:
            return hashlib.sha256(requirements.read_bytes()).hexdigest()
        except OSError as exc:
            raise KeeperError(
                "Cannot read safe-copy requirements: {}".format(exc)
            )

    def _ensure_safe_venv(self, tree):
        expected_hash = self._safe_requirements_hash(tree)
        marker = self.safe_venv / ".keeper-requirements.sha256"
        safe_python = self.safe_venv / "bin" / "python"
        try:
            marker_hash = marker.read_text(encoding="ascii").strip()
        except OSError:
            marker_hash = None
        if marker_hash == expected_hash and self._python_usable(safe_python):
            return safe_python

        self._ensure_keeper_home()
        if not safe_python.is_file():
            if self.safe_venv.exists():
                shutil.rmtree(str(self.safe_venv))
            base_python = shutil.which("python3") or sys.executable
            try:
                create = subprocess.run(
                    [base_python, "-m", "venv", str(self.safe_venv)],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                create = None
                create_error = str(exc)
            else:
                create_error = tail_text(create.stdout + create.stderr)
            if create is None or create.returncode != 0:
                self._write_log(
                    "Safe venv creation failed; trying the grail venv: {}".format(
                        create_error
                    )
                )
                grail_python = self.grail_venv / "bin" / "python"
                if self._python_usable(grail_python):
                    return grail_python
                raise KeeperError("No usable Python environment for the safe copy.")

        try:
            install = subprocess.run(
                [
                    str(safe_python),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(tree / "requirements.txt"),
                ],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            install = None
            install_error = str(exc)
        else:
            install_error = tail_text(install.stdout + install.stderr)
        if install is not None and install.returncode == 0 and self._python_usable(
            safe_python
        ):
            marker.write_text(expected_hash + "\n", encoding="ascii")
            return safe_python

        self._write_log(
            "Safe venv dependency install failed; trying the grail venv: {}".format(
                install_error
            )
        )
        grail_python = self.grail_venv / "bin" / "python"
        if self._python_usable(grail_python):
            return grail_python
        raise KeeperError("No usable Python environment for the safe copy.")

    def capture_safe_copy(self, version):
        version = normalize_version(version)
        tree = self._copy_safe_tree(version)
        python_path = self._ensure_safe_venv(tree)
        self._write_log("Safe copy {} is ready.".format(version))
        return tree, python_path

    def _agents_importable(self, python_path, agents_path):
        if not agents_path.is_dir():
            return False
        code = r"""
import glob
import importlib.util
import os
import sys

root = sys.argv[1]
sys.path.insert(0, root)
for path in sorted(glob.glob(os.path.join(root, "*_agent.py"))):
    name = "_keeper_probe_" + os.path.basename(path).replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load " + path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
"""
        try:
            result = subprocess.run(
                [str(python_path), "-c", code, str(agents_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _link_user_file(self, safe_tree, name):
        destination = safe_tree / name
        source = self.kernel_dir / name
        if os.path.lexists(str(destination)):
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(str(destination))
            else:
                destination.unlink()
        if source.exists():
            destination.symlink_to(source)

    def _safe_python(self):
        safe_python = self.safe_venv / "bin" / "python"
        if self._python_usable(safe_python):
            return safe_python
        grail_python = self.grail_venv / "bin" / "python"
        if self._python_usable(grail_python):
            return grail_python
        return None

    def launch_safe(self, version):
        version = normalize_version(version)
        safe_tree = self.safe_root / version
        python_path = self._safe_python()
        if not safe_tree.is_dir() or python_path is None:
            return {"healthy": False, "reason": "safe copy is incomplete"}
        for name in (".env",) + SIGNIN_CARRY_NAMES:
            self._link_user_file(safe_tree, name)
        user_agents = self.kernel_dir / "agents"
        safe_agents = safe_tree / "agents"
        agents_path = (
            user_agents
            if self._agents_importable(python_path, user_agents)
            else safe_agents
        )
        user_soul = self.kernel_dir / "soul.md"
        soul_path = user_soul if user_soul.is_file() else safe_tree / "soul.md"
        environment = os.environ.copy()
        environment.update(
            {
                "AGENTS_PATH": str(agents_path),
                "PORT": str(self.port),
                "PYTHONUNBUFFERED": "1",
                "SOUL_PATH": str(soul_path),
            }
        )
        return self._launch_tree(
            "safe", python_path, safe_tree, environment, version
        )

    def _origin_reachable(self):
        if os.environ.get("KEEPER_SKIP_ORIGIN_CHECK") == "1":
            return True, None
        request = urllib.request.Request(
            GRAIL_URL, headers={"User-Agent": "rapp-keeper/1"}, method="HEAD"
        )
        try:
            with self.urlopen(request, timeout=5):
                return True, None
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                return True, None
            return False, str(exc)
        except (OSError, urllib.error.URLError, socket.timeout) as exc:
            return False, str(exc)

    def _download_installer(self):
        request = urllib.request.Request(
            INSTALLER_URL,
            headers={"User-Agent": "rapp-keeper/1"},
            method="GET",
        )
        try:
            with self.urlopen(request, timeout=30) as response:
                script = response.read()
        except (OSError, urllib.error.URLError, socket.timeout) as exc:
            raise KeeperError("Cannot download the official installer: {}".format(exc))
        if not script.startswith(b"#!/bin/bash"):
            raise KeeperError("The official installer response was not a Bash script.")
        return script

    def _terminate_installer(self, process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                return

    def run_installer(self, version):
        version = normalize_version(version)
        self._carry_signin_files()
        reachable, reach_error = self._origin_reachable()
        if not reachable:
            self._restore_carried_signin_files(remove_restored=True)
            return False, "grail origin is unreachable: {}".format(reach_error)
        try:
            script = self._download_installer()
        except KeeperError as exc:
            self._restore_carried_signin_files(remove_restored=True)
            return False, str(exc)

        text = script.decode("utf-8", "replace")
        command = [
            "bash",
            "-s",
            "--",
            "--version",
            "brainstem-v{}".format(version),
            "--no-launch",
        ]
        self._ensure_keeper_home()
        log_handle = self.log_path.open("ab", buffering=0)
        try:
            os.chmod(str(self.log_path), 0o600)
        except OSError:
            pass
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=os.environ.copy(),
            )
        except OSError as exc:
            log_handle.close()
            self._restore_carried_signin_files(remove_restored=True)
            return False, "cannot start the official installer: {}".format(exc)
        finally:
            if not log_handle.closed:
                log_handle.close()

        stop_guard = threading.Event()
        guard_thread = threading.Thread(
            target=self._signin_guard,
            args=(stop_guard,),
            name="keeper-signin-guard",
            daemon=True,
        )
        guard_thread.start()
        guard_finished = False

        def finish_guard():
            nonlocal guard_finished
            if guard_finished:
                return True
            guard_finished = True
            return self._finish_signin_guard(stop_guard, guard_thread)

        try:
            if process.stdin is not None:
                try:
                    process.stdin.write(text)
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()
            deadline = time.monotonic() + self.installer_timeout
            while time.monotonic() < deadline:
                health = self.probe_health(version, timeout=1)
                if health["healthy"] and self.installed_version() == version:
                    pid = self._adopt_installed_server(version, health)
                    if pid is not None:
                        if not finish_guard():
                            self._stop_pidfile(self.installed_pid_path)
                            return False, (
                                "official installer launched Brainstem, but "
                                "keeper could not restore sign-in state"
                            )
                        threading.Thread(
                            target=process.wait,
                            name="keeper-installer-reaper",
                            daemon=True,
                        ).start()
                        return True, (
                            "official installer launched healthy Brainstem; "
                            "keeper adopted PID {}".format(pid)
                        )

                return_code = process.poll()
                if return_code is not None:
                    restored = finish_guard()
                    observed = self.installed_version()
                    if not restored:
                        return False, (
                            "official installer exited, but keeper could not "
                            "restore sign-in state"
                        )
                    if return_code == 0 and observed == version:
                        return True, (
                            "official installer completed without leaving a "
                            "server running"
                        )
                    return False, "official installer failed with code {}".format(
                        return_code
                    )
                self.sleep(0.25)

            self._terminate_installer(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                process.wait(timeout=5)
            restored = finish_guard()
            if not restored:
                return False, (
                    "official installer timed out and sign-in state could not "
                    "be restored"
                )
            return False, "official installer timed out"
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if not guard_finished:
                finish_guard()

    def _record_healthy_installed(self, state, version, event):
        version = normalize_version(version)
        state["last_good"] = version
        state["serving"] = "installed"
        try:
            self.capture_safe_copy(version)
        except KeeperError as exc:
            self._write_log("Safe-copy refresh failed: {}".format(exc))
            self.record_event(
                state,
                "safe_copy_failed",
                version=version,
                reason=tail_text(exc),
            )
        else:
            state["safe_version"] = version
            self.record_event(state, "safe_copy_ready", version=version)
        self.record_event(state, event, version=version)

    def _health_is_safe(self, state, health):
        safe_version = state.get("safe_version")
        safe_pid = self._read_pid(self.safe_pid_path)
        if not safe_version or not safe_pid or not self._pid_alive(safe_pid):
            return False
        try:
            return (
                health.get("healthy")
                and normalize_version(health.get("version")) == safe_version
            )
        except KeeperError:
            return False

    def status(self):
        state = self.load_state()
        installed = self.installed_version()
        health = self.probe_health()
        if not health["healthy"]:
            serving = "none"
        elif self._health_is_safe(state, health):
            serving = "safe"
        else:
            try:
                serving = (
                    "installed"
                    if installed
                    and normalize_version(health.get("version")) == installed
                    else state.get("serving", "unknown")
                )
            except KeeperError:
                serving = state.get("serving", "unknown")
        return {
            "installed_version": installed,
            "last_good": state.get("last_good"),
            "failed": state.get("failed", {}),
            "healthy": bool(health["healthy"]),
            "health": health,
            "safe_version": state.get("safe_version"),
            "serving": serving,
        }

    def start(self):
        with self.lock():
            state = self.load_state()
            installed = self.installed_version()
            current = self.probe_health()
            if self._health_is_safe(state, current):
                state["serving"] = "safe"
                self.record_event(
                    state,
                    "start_already_healthy",
                    serving="safe",
                    version=current.get("version"),
                )
                self.save_state(state)
                self._write_log("Safe Brainstem is already healthy.")
                return current
            if current["healthy"] and installed:
                try:
                    current_version = normalize_version(current["version"])
                except KeeperError:
                    current_version = None
                if current_version == installed:
                    self._adopt_installed_server(installed, current)
                    self._record_healthy_installed(
                        state, installed, "start_already_healthy"
                    )
                    self.save_state(state)
                    self._write_log(
                        "Installed Brainstem {} is already healthy.".format(
                            installed
                        )
                    )
                    return current

            if installed:
                health = self.launch_installed(installed)
                if health["healthy"]:
                    self._record_healthy_installed(
                        state, installed, "start_installed"
                    )
                    self.save_state(state)
                    return health
                self._write_log(
                    "Installed Brainstem did not become healthy: {}".format(
                        health.get("reason")
                    )
                )

            last_good = state.get("last_good")
            if last_good:
                self._write_log(
                    "Reinstalling last known-good Brainstem {}.".format(last_good)
                )
                installed_ok, install_reason = self.run_installer(last_good)
                if installed_ok:
                    health = self.launch_installed(last_good)
                    if health["healthy"]:
                        self._record_healthy_installed(
                            state, last_good, "start_reinstalled_last_good"
                        )
                        self.save_state(state)
                        return health
                    install_reason = health.get("reason")
                self._write_log(
                    "Last known-good reinstall failed: {}".format(install_reason)
                )

            safe_version = state.get("safe_version")
            if safe_version:
                health = self.launch_safe(safe_version)
                if health["healthy"]:
                    state["serving"] = "safe"
                    self.record_event(
                        state, "start_safe", version=safe_version
                    )
                    self.save_state(state)
                    return health
                self._write_log(
                    "Safe Brainstem failed to start: {}".format(
                        health.get("reason")
                    )
                )

            state["serving"] = "none"
            self.record_event(
                state,
                "start_failed",
                reason="installed, last-good, and safe copies were unavailable",
            )
            self.save_state(state)
            raise KeeperError(
                "No healthy installed, last-known-good, or safe Brainstem could start."
            )

    def _recover_failed_upgrade(self, state, target, reason, rollback_version):
        state.setdefault("failed", {})[target] = {
            "time": self._timestamp(),
            "reason": tail_text(reason),
        }
        self.record_event(
            state,
            "upgrade_failed",
            version=target,
            reason=tail_text(reason),
        )
        self.save_state(state)
        self._write_log(
            "Upgrade to {} failed: {}".format(target, tail_text(reason))
        )

        if rollback_version:
            self._write_log(
                "Rolling back to last known-good Brainstem {}.".format(
                    rollback_version
                )
            )
            rollback_ok, rollback_reason = self.run_installer(rollback_version)
            if rollback_ok:
                health = self.launch_installed(rollback_version)
                if health["healthy"]:
                    self._record_healthy_installed(
                        state, rollback_version, "upgrade_rolled_back"
                    )
                    self.save_state(state)
                    return {
                        "upgraded": False,
                        "target": target,
                        "recovered": "rollback",
                        "version": rollback_version,
                        "reason": tail_text(reason),
                    }
                rollback_reason = health.get("reason")
            self._write_log(
                "Rollback to {} failed: {}".format(
                    rollback_version, rollback_reason
                )
            )
            self.record_event(
                state,
                "rollback_failed",
                version=rollback_version,
                reason=tail_text(rollback_reason),
            )
            self.save_state(state)

        safe_version = state.get("safe_version")
        if safe_version:
            health = self.launch_safe(safe_version)
            if health["healthy"]:
                state["serving"] = "safe"
                self.record_event(
                    state,
                    "upgrade_recovered_safe",
                    failed_version=target,
                    version=safe_version,
                )
                self.save_state(state)
                return {
                    "upgraded": False,
                    "target": target,
                    "recovered": "safe",
                    "version": safe_version,
                    "reason": tail_text(reason),
                }
            self._write_log(
                "Safe recovery failed: {}".format(health.get("reason"))
            )

        state["serving"] = "none"
        self.record_event(
            state,
            "upgrade_recovery_failed",
            version=target,
            reason=tail_text(reason),
        )
        self.save_state(state)
        raise KeeperError(
            "Upgrade to {} failed and no recovery copy became healthy.".format(
                target
            )
        )

    def upgrade(self, target=None, force=False):
        target = normalize_version(target) if target else self.default_target()
        with self.lock():
            state = self.load_state()
            installed = self.installed_version()
            current = self.probe_health(installed) if installed else self.probe_health()
            if installed and current["healthy"]:
                self._record_healthy_installed(
                    state, installed, "upgrade_recorded_last_good"
                )
                self.save_state(state)

            if not force and target in state.get("failed", {}):
                self.record_event(
                    state, "upgrade_skipped_failed", version=target
                )
                self.save_state(state)
                self._write_log(
                    "Skipping {} because that version already failed.".format(
                        target
                    )
                )
                return {
                    "upgraded": False,
                    "target": target,
                    "skipped": "failed",
                }
            if (
                not force
                and installed
                and compare_versions(target, installed) <= 0
            ):
                self.record_event(
                    state,
                    "upgrade_skipped_not_newer",
                    installed=installed,
                    version=target,
                )
                self.save_state(state)
                self._write_log(
                    "Skipping {} because installed {} is not older.".format(
                        target, installed
                    )
                )
                return {
                    "upgraded": False,
                    "target": target,
                    "skipped": "not-newer",
                }

            rollback_version = state.get("last_good")
            self._stop_managed_processes()
            self._stop_port_listeners()
            self._write_log("Installing Brainstem {}.".format(target))
            installed_ok, install_reason = self.run_installer(target)
            if not installed_ok:
                return self._recover_failed_upgrade(
                    state, target, install_reason, rollback_version
                )
            observed = self.installed_version()
            if observed != target:
                return self._recover_failed_upgrade(
                    state,
                    target,
                    "installer wrote version {!r}".format(observed),
                    rollback_version,
                )
            health = self.launch_installed(target)
            if not health["healthy"]:
                return self._recover_failed_upgrade(
                    state,
                    target,
                    health.get("reason") or "new version was unhealthy",
                    rollback_version,
                )

            state.setdefault("failed", {}).pop(target, None)
            self._record_healthy_installed(
                state, target, "upgrade_succeeded"
            )
            self.save_state(state)
            self._write_log("Brainstem {} is healthy.".format(target))
            return {
                "upgraded": True,
                "target": target,
                "version": target,
                "health": health,
            }

    def watch(self, interval):
        if interval <= 0:
            raise KeeperError("watch interval must be greater than zero")
        self._write_log(
            "Keeper watch started with interval {} seconds.".format(interval)
        )
        while True:
            if self._signin_holder_present():
                try:
                    signin_result = self.signin_sync()
                except KeeperError as exc:
                    self._write_log(
                        "Watch sign-in sync failed: {}".format(exc)
                    )
                else:
                    if signin_result["adopted_from"]:
                        self._write_log(
                            "Shared sign-in generation {} adopted from {}.".format(
                                signin_result["generation"],
                                signin_result["adopted_from"],
                            )
                        )
                    if signin_result["warnings"]:
                        self._write_log(
                            "Shared sign-in flight log warnings: {}.".format(
                                ", ".join(signin_result["warnings"])
                            )
                        )
            try:
                self.start()
            except KeeperError as exc:
                self._write_log("Watch start failed: {}".format(exc))
            state = self.load_state()
            last_check = float(state.get("last_upgrade_check", 0) or 0)
            if self.now() - last_check >= UPGRADE_INTERVAL:
                with self.lock():
                    state = self.load_state()
                    state["last_upgrade_check"] = self.now()
                    self.record_event(state, "daily_upgrade_check")
                    self.save_state(state)
                try:
                    self.upgrade()
                except KeeperError as exc:
                    self._write_log("Daily upgrade failed: {}".format(exc))
            self.sleep(interval)

    def _copy_service_payload(self):
        self._ensure_keeper_home()
        source_script = Path(__file__).resolve()
        if source_script != self.installed_script_path.resolve():
            shutil.copy2(str(source_script), str(self.installed_script_path))
        self.installed_script_path.chmod(0o755)
        source_kernel = self.repo_root / "plugin" / "kernel.json"
        if source_kernel.is_file():
            shutil.copy2(str(source_kernel), str(self.installed_kernel_path))

    def _service_command(self):
        return [
            sys.executable,
            str(self.installed_script_path),
            "watch",
            "--interval",
            str(DEFAULT_INTERVAL),
        ]

    def _systemd_user_available(self):
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return False
        try:
            result = subprocess.run(
                [systemctl, "--user", "show-environment"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _systemd_quote(self, value):
        return '"{}"'.format(
            str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        )

    def install_service(self):
        self._copy_service_payload()
        command = self._service_command()
        if sys.platform == "darwin":
            launch_agents = Path.home() / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True, exist_ok=True)
            plist_path = launch_agents / (SERVICE_LABEL + ".plist")
            payload = {
                "Label": SERVICE_LABEL,
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": True,
                "StandardOutPath": str(self.log_path),
                "StandardErrorPath": str(self.log_path),
            }
            with plist_path.open("wb") as handle:
                plistlib.dump(payload, handle)
            domain = "gui/{}".format(os.getuid())
            subprocess.run(
                ["launchctl", "bootout", domain, str(plist_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            result = subprocess.run(
                ["launchctl", "bootstrap", domain, str(plist_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise KeeperError(
                    "launchctl could not install keeper: {}".format(
                        tail_text(result.stderr)
                    )
                )
            print("Installed macOS LaunchAgent {}.".format(SERVICE_LABEL))
            return

        if sys.platform.startswith("linux") and self._systemd_user_available():
            unit_dir = Path.home() / ".config" / "systemd" / "user"
            unit_dir.mkdir(parents=True, exist_ok=True)
            unit_path = unit_dir / SYSTEMD_SERVICE
            exec_start = " ".join(self._systemd_quote(part) for part in command)
            unit_path.write_text(
                "[Unit]\n"
                "Description=RAPP Brainstem keeper\n"
                "After=network-online.target\n\n"
                "[Service]\n"
                "Type=simple\n"
                "ExecStart={}\n"
                "Restart=always\n"
                "RestartSec=10\n"
                "Environment=PYTHONUNBUFFERED=1\n\n"
                "[Install]\n"
                "WantedBy=default.target\n".format(exec_start),
                encoding="utf-8",
            )
            for args in (
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", SYSTEMD_SERVICE],
            ):
                result = subprocess.run(
                    args, capture_output=True, text=True, check=False
                )
                if result.returncode != 0:
                    raise KeeperError(
                        "{} failed: {}".format(
                            shell_join(args), tail_text(result.stderr)
                        )
                    )
            print("Installed Linux user service {}.".format(SYSTEMD_SERVICE))
            return

        print(shell_join(command))

    def uninstall_service(self):
        plist_path = (
            Path.home() / "Library" / "LaunchAgents" / (SERVICE_LABEL + ".plist")
        )
        if plist_path.exists():
            if shutil.which("launchctl"):
                domain = "gui/{}".format(os.getuid())
                subprocess.run(
                    ["launchctl", "bootout", domain, str(plist_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            plist_path.unlink()

        unit_path = (
            Path.home() / ".config" / "systemd" / "user" / SYSTEMD_SERVICE
        )
        if unit_path.exists():
            if shutil.which("systemctl"):
                subprocess.run(
                    ["systemctl", "--user", "disable", "--now", SYSTEMD_SERVICE],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            unit_path.unlink()
            if shutil.which("systemctl"):
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        print("Keeper service removed.")

    def uninstall(self):
        with self.lock():
            self._stop_managed_processes()
            self.uninstall_service()
        if self.keeper_home.exists():
            shutil.rmtree(str(self.keeper_home))
        print(
            "Keeper files removed. Brainstem installs and their token files "
            "were not changed."
        )


def print_json(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Keep an unchanged RAPP Brainstem install healthy."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show installed and recovery state")
    subparsers.add_parser("start", help="ensure a healthy Brainstem is serving")
    upgrade = subparsers.add_parser("upgrade", help="safely upgrade Brainstem")
    upgrade.add_argument("--to", dest="target", help="target version")
    upgrade.add_argument(
        "--force",
        action="store_true",
        help="retry a failed or non-newer target",
    )
    watch = subparsers.add_parser("watch", help="keep Brainstem healthy")
    watch.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL, help="seconds"
    )
    signin = subparsers.add_parser(
        "signin",
        help="share one GitHub sign-in across registered Brainstems",
    )
    signin_subparsers = signin.add_subparsers(
        dest="signin_command",
        required=True,
    )
    signin_subparsers.add_parser(
        "status",
        help="show holder and registered Brainstem generations",
    )
    adopt = signin_subparsers.add_parser(
        "adopt",
        help="adopt an existing Brainstem token as the machine holder",
    )
    adopt.add_argument(
        "--from",
        dest="source",
        help="Brainstem directory (defaults to the main install)",
    )
    register = signin_subparsers.add_parser(
        "register",
        help="register a Brainstem and project the holder into it",
    )
    register.add_argument("brainstem_dir")
    unregister = signin_subparsers.add_parser(
        "unregister",
        help="remove a Brainstem from the manifest without deleting its token",
    )
    unregister.add_argument("brainstem_dir")
    signin_subparsers.add_parser(
        "sync",
        help="adopt a newer successful sign-in and project the holder",
    )
    subparsers.add_parser(
        "install-service", help="install the per-user keeper service"
    )
    subparsers.add_parser(
        "uninstall-service", help="remove the per-user keeper service"
    )
    subparsers.add_parser(
        "uninstall", help="remove keeper without changing the grail"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    keeper = Keeper()
    try:
        if args.command == "status":
            print_json(keeper.status())
        elif args.command == "start":
            keeper.start()
            print_json(keeper.status())
        elif args.command == "upgrade":
            result = keeper.upgrade(args.target, args.force)
            print_json(result)
            print_json(keeper.status())
        elif args.command == "watch":
            keeper.watch(args.interval)
        elif args.command == "signin":
            if args.signin_command == "status":
                print_json(keeper.signin_status())
            elif args.signin_command == "adopt":
                print_json(keeper.signin_adopt(args.source))
            elif args.signin_command == "register":
                print_json(keeper.signin_register(args.brainstem_dir))
            elif args.signin_command == "unregister":
                print_json(keeper.signin_unregister(args.brainstem_dir))
            elif args.signin_command == "sync":
                print_json(keeper.signin_sync())
        elif args.command == "install-service":
            keeper.install_service()
        elif args.command == "uninstall-service":
            keeper.uninstall_service()
        elif args.command == "uninstall":
            keeper.uninstall()
        return 0
    except KeyboardInterrupt:
        return 130
    except KeeperError as exc:
        print("keeper: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
