#!/usr/bin/env python3
"""
Core design:
- GTK4 overlay with session-aware focus, clipboard, and paste backends for X11
  and Wayland; compositor blur or real locally blurred backdrop pixels.
- Audio capture is independent of a killable local inference worker. Completed
  transcripts and failed recordings remain recoverable.
- WiVRn headset evidence drives confirmed audio transitions, with a persistent
  journal for exact system and application device restoration.
- Local faster-whisper or whisper.cpp inference supports GPU and CPU runtimes.
"""
from __future__ import annotations

import contextlib
import atexit
import ctypes
import ctypes.util
import dataclasses
import errno
import fcntl
import math
import json
import os
import queue
import select
import signal
import socket
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import re
import uuid
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "KDictate"
APP_ID = "dev.kdictate.Cosmic"
APP_DIR = Path(os.environ.get("KDICTATE_APPDIR", Path.home() / ".local/share/kdictate-cosmic")).expanduser()
LOG_FILE = APP_DIR / "kdictate.log"
CONFIG_FILE = APP_DIR / "config.env"

def _load_config_env() -> None:
    """Load simple KEY=VALUE settings written by the installer.

    Environment variables still win, so advanced users can override service config.
    """
    try:
        if not CONFIG_FILE.exists():
            return
        for raw in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    except Exception as exc:
        # Logging may not be initialized yet, so keep this intentionally quiet.
        pass

_load_config_env()

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SOCKET_PATH = RUNTIME_DIR / "kdictate.sock"
LOCK_PATH = RUNTIME_DIR / "kdictate.daemon.lock"
QUALITY_PROFILES = {
    "speed": "base.en",
    "balanced": "small.en",
    "quality": "large-v3",
}

QUALITY_LABELS = {
    "speed": "Speed / base.en",
    "balanced": "Balanced / small.en",
    "quality": "Quality / large-v3",
}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normalize_profile(value: str | None, model: str | None = None) -> str:
    raw = (value or "").strip().lower()
    if raw in QUALITY_PROFILES:
        return raw

    wanted_model = (model or os.environ.get("KDICTATE_MODEL", "small.en")).strip()
    for profile, profile_model in QUALITY_PROFILES.items():
        if wanted_model == profile_model:
            return profile

    return "balanced"


def save_runtime_config(updates: dict[str, str]) -> None:
    """Atomic, serialized updates preserve installer keys and concurrent theme edits."""
    with CONFIG_LOCK:
        try:
            _ensure_dirs()
            existing = CONFIG_FILE.read_text(encoding="utf-8").splitlines() if CONFIG_FILE.exists() else []
            seen = set()
            output = []
            for raw in existing:
                key = raw.split("=", 1)[0].strip() if "=" in raw and not raw.lstrip().startswith("#") else ""
                if key in updates:
                    if key not in seen:
                        output.append(f"{key}={updates[key]}")
                        seen.add(key)
                else:
                    output.append(raw)
            output.extend(f"{key}={value}" for key, value in updates.items() if key not in seen)
            fd, temporary = tempfile.mkstemp(prefix="config-", dir=APP_DIR)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write("\n".join(output).rstrip() + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, CONFIG_FILE)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
            os.environ.update(updates)
        except Exception as exc:
            log(f"Could not persist settings {list(updates)}: {exc!r}")


def realtime_transcription_enabled() -> bool:
    # Default ON unless the user explicitly turns it off.
    return _truthy(os.environ.get("KDICTATE_REALTIME_TRANSCRIPTION", "1"))


def active_silence_to_finish_seconds() -> float:
    if realtime_transcription_enabled():
        return float(os.environ.get("KDICTATE_REALTIME_SILENCE_TO_FINISH_SECONDS", "4"))
    return float(os.environ.get("KDICTATE_SILENCE_TO_FINISH_SECONDS", "3"))


PULSE_SOURCE_PREFIX = "pulse-source:"
WIVRN_AUTO_DEVICE_ID = "auto-wivrn"
VR_AUDIO_STATE = {
    "active": False,

    "restore_source": None,
    "restore_sink": None,
    "last_desktop_source": None,
    "last_desktop_sink": None,
    "restore_deadline": 0.0,
    "restore_attempts": 0,
    "easyeffects_paused": False,
    "easyeffects_was_running": False,
    "easyeffects_kind": None,
    "easyeffects_restore_until": 0.0,
    "easyeffects_last_restore_attempt": 0.0,
}

VR_AUDIO_LOCK = threading.RLock()
BACKGROUND_VR_AUDIO_MONITOR_STARTED = False
BACKGROUND_VR_AUDIO_MONITOR_LOCK = threading.Lock()
CONFIG_LOCK = threading.RLock()

THEME_LABELS = {
    "dark": "Dark", "light": "Light",
    "glass-dark": "Glass dark", "glass-light": "Glass light",
}


def active_theme() -> str:
    value = os.environ.get("KDICTATE_THEME", "glass-dark").strip().lower()
    return value if value in THEME_LABELS else "glass-dark"


def theme_palette() -> dict:
    light = active_theme() in {"light", "glass-light"}
    return {
        "light": light,
        "glass": active_theme().startswith("glass-"),
        "text": (0.09, 0.12, 0.18) if light else (1.0, 1.0, 1.0),
        "surface": (0.97, 0.98, 1.0) if light else (0.070, 0.078, 0.10),
        "accent": (0.23, 0.38, 0.78) if light else (0.72, 0.80, 1.0),
    }


def atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def wivrn_auto_audio_enabled() -> bool:
    return _truthy(os.environ.get("KDICTATE_WIVRN_AUTO_AUDIO", "1"))


def _shorten_label(value: str, limit: int = 48) -> str:
    value = " ".join(str(value or "").split()).strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 3)].rstrip() + "..."


def _pactl_available() -> bool:
    return command_exists("pactl")


def _pactl(args: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str] | None:
    if not _pactl_available():
        return None

    try:
        return subprocess.run(
            ["pactl", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except Exception as exc:
        log(f"pactl {' '.join(args)} failed: {exc!r}")
        return None


def _pactl_default(kind: str) -> str:
    direct = _pactl([f"get-default-{kind}"], timeout=1.0)
    if direct is not None and direct.returncode == 0:
        return direct.stdout.strip()
    proc = _pactl(["info"], timeout=3.0)
    if proc is None or proc.returncode != 0:
        return ""

    wanted = "Default Source:" if kind == "source" else "Default Sink:"
    for raw in proc.stdout.splitlines():
        if raw.startswith(wanted):
            return raw.split(":", 1)[1].strip()

    return ""


def _pactl_nodes(kind: str) -> list[dict[str, str]]:
    # Machine-readable properties preserve identities and avoid localized parsing.
    result = _pactl(["--format=json", "list", kind], timeout=2.0)
    if result is not None and result.returncode == 0:
        try:
            nodes = []
            for item in json.loads(result.stdout):
                props = item.get("properties", {})
                name = str(item.get("name", ""))
                if kind == "sources" and (name.endswith(".monitor") or
                        item.get("monitor_of_sink") not in {None, "n/a", 4294967295}):
                    continue
                nodes.append({"name": name,
                    "label": str(item.get("description") or name),
                    "description": str(item.get("description") or name),
                    "properties": props,
                    "index": str(item.get("index", ""))})
            return nodes
        except (ValueError, TypeError, AttributeError):
            pass
    # kind is "sources" or "sinks". The long form includes the friendly
    # descriptions shown by desktop audio UIs, unlike PortAudio's generic
    # "pulse" / "pipewire" bridge names.
    proc = _pactl(["list", kind], timeout=4.0)
    if proc is None or proc.returncode != 0:
        return []

    nodes: list[dict[str, str]] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get("name"):
            name = current.get("name", "")
            desc = current.get("description", "") or name
            nodes.append({"name": name, "label": desc, "description": desc})

    header = "Source #" if kind == "sources" else "Sink #"

    for raw in proc.stdout.splitlines():
        line = raw.strip()

        if line.startswith(header):
            flush()
            current = {}
            continue

        if line.startswith("Name:"):
            current["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            current["description"] = line.split(":", 1)[1].strip()
        elif line.startswith("node.description =") and not current.get("description"):
            current["description"] = line.split("=", 1)[1].strip().strip('"')

    flush()

    if kind == "sources":
        filtered = []
        for node in nodes:
            hay = f"{node.get('name', '')} {node.get('label', '')}".lower()
            if node.get("name", "").endswith(".monitor") or "monitor" in hay:
                continue
            filtered.append(node)
        return filtered

    return nodes


def _is_wivrn_node(node: dict[str, str]) -> bool:
    hay = f"{node.get('name', '')} {node.get('label', '')} {node.get('description', '')}".lower()
    return "wivrn" in hay


def _find_wivrn_audio() -> tuple[dict[str, str] | None, dict[str, str] | None]:
    source = next((node for node in _pactl_nodes("sources") if _is_wivrn_node(node)), None)
    sink = next((node for node in _pactl_nodes("sinks") if _is_wivrn_node(node)), None)
    return source, sink

def _node_by_name(nodes: list[dict[str, str]], name: str) -> dict[str, str] | None:
    if not name:
        return None
    return next((node for node in nodes if node.get("name") == name), None)


def _is_non_wivrn_audio_name(name: str, nodes: list[dict[str, str]]) -> bool:
    node = _node_by_name(nodes, name)
    return node is not None and not _is_wivrn_node(node)


def _first_non_wivrn_audio_name(nodes: list[dict[str, str]]) -> str:
    for node in nodes:
        name = node.get("name", "")
        if name and not _is_wivrn_node(node):
            return name
    return ""


def _remember_desktop_audio_defaults(
    *,
    current_source: str | None = None,
    current_sink: str | None = None,
    sources: list[dict[str, str]] | None = None,
    sinks: list[dict[str, str]] | None = None,
) -> None:
    """This is intentionally separate from restore_source/restore_sink. It gives us
    a good fallback if the desktop switches to WiVRn before we notice VR entry.
    """
    sources = sources if sources is not None else _pactl_nodes("sources")
    sinks = sinks if sinks is not None else _pactl_nodes("sinks")

    current_source = current_source if current_source is not None else _pactl_default("source")
    current_sink = current_sink if current_sink is not None else _pactl_default("sink")

    changed: list[str] = []

    if current_source and _is_non_wivrn_audio_name(current_source, sources):
        if VR_AUDIO_STATE.get("last_desktop_source") != current_source:
            VR_AUDIO_STATE["last_desktop_source"] = current_source
            changed.append(f"source={current_source!r}")

    if current_sink and _is_non_wivrn_audio_name(current_sink, sinks):
        if VR_AUDIO_STATE.get("last_desktop_sink") != current_sink:
            VR_AUDIO_STATE["last_desktop_sink"] = current_sink
            changed.append(f"sink={current_sink!r}")

    if changed:
        log("Remembered desktop audio default " + " ".join(changed))


def _preferred_restore_target(
    kind: str,
    saved_name: str,
    nodes: list[dict[str, str]],
) -> tuple[str, bool]:
    """Return the best non-WiVRn restore target and whether it is available."""
    saved_name = str(saved_name or "")

    if _is_non_wivrn_audio_name(saved_name, nodes):
        return saved_name, True

    last_key = "last_desktop_source" if kind == "source" else "last_desktop_sink"
    last_name = str(VR_AUDIO_STATE.get(last_key) or "")

    if last_name != saved_name and _is_non_wivrn_audio_name(last_name, nodes):
        return last_name, True

    return saved_name, False


def _restore_audio_default(kind: str, target: str) -> bool:
    if not target:
        return True

    _set_default_audio(kind, target)

    actual = _pactl_default(kind)
    if actual == target:
        return True

    log(
        "WiVRn audio restore verification pending "
        f"kind={kind!r} wanted={target!r} actual={actual!r}"
    )
    return False

def _set_default_audio(kind: str, name: str) -> None:
    if not name:
        return

    cmd = "set-default-source" if kind == "source" else "set-default-sink"
    proc = _pactl([cmd, name], timeout=3.0)

    if proc is not None and proc.returncode != 0:
        log(f"pactl {cmd} {name!r} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def easyeffects_vr_pause_enabled() -> bool:
    return _truthy(os.environ.get("KDICTATE_WIVRN_PAUSE_EASYEFFECTS", "1"))


def _flatpak_app_available(app_id: str) -> bool:
    if not command_exists("flatpak"):
        return False

    try:
        proc = subprocess.run(
            ["flatpak", "info", app_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.2,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _easyeffects_command_prefix(preferred: str | None = None) -> tuple[list[str] | None, str | None]:
    if preferred == "native" and command_exists("easyeffects"):
        return ["easyeffects"], "native"

    if preferred == "flatpak" and _flatpak_app_available("com.github.wwmm.easyeffects"):
        return ["flatpak", "run", "com.github.wwmm.easyeffects"], "flatpak"

    if command_exists("easyeffects"):
        return ["easyeffects"], "native"

    if _flatpak_app_available("com.github.wwmm.easyeffects"):
        return ["flatpak", "run", "com.github.wwmm.easyeffects"], "flatpak"

    return None, None


def _easyeffects_is_running() -> bool:
    if not command_exists("pgrep"):
        return False

    patterns = [
        r"(^|/)easyeffects($| )",
        r"com\.github\.wwmm\.easyeffects",
    ]

    for pattern in patterns:
        try:
            proc = subprocess.run(
                ["pgrep", "-f", pattern],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=0.5,
                check=False,
            )
            if proc.returncode == 0:
                return True
        except Exception:
            continue

    return False


def _pause_easyeffects_for_vr() -> None:
    if not easyeffects_vr_pause_enabled():
        return

    if VR_AUDIO_STATE.get("easyeffects_paused"):
        return

    if not _easyeffects_is_running():
        return

    prefix, kind = _easyeffects_command_prefix()
    if prefix is None:
        return

    VR_AUDIO_STATE["easyeffects_paused"] = True
    VR_AUDIO_STATE["easyeffects_was_running"] = True
    VR_AUDIO_STATE["easyeffects_kind"] = kind

    try:
        proc = subprocess.run(
            [*prefix, "-q"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.5,
            check=False,
        )
        log(f"Paused EasyEffects for WiVRn mode kind={kind!r} rc={proc.returncode}")
    except Exception as exc:
        log(f"Could not pause EasyEffects for WiVRn mode: {exc!r}")


def _hide_easyeffects_window_soon() -> None:
    def worker() -> None:
        time.sleep(2.0)
        deadline = time.time() + 7.0

        commands: list[list[str]] = []

        if command_exists("wlrctl"):
            commands.extend([
                ["wlrctl", "toplevel", "minimize", "app_id:easyeffects"],
                ["wlrctl", "toplevel", "minimize", "app_id:com.github.wwmm.easyeffects"],
                ["wlrctl", "toplevel", "minimize", "title:EasyEffects"],
            ])

        if command_exists("wmctrl"):
            commands.extend([
                ["wmctrl", "-x", "-r", "easyeffects", "-b", "add,hidden"],
                ["wmctrl", "-x", "-r", "com.github.wwmm.easyeffects", "-b", "add,hidden"],
                ["wmctrl", "-r", "EasyEffects", "-b", "add,hidden"],
            ])

        while time.time() < deadline:
            for command in commands:
                try:
                    proc = subprocess.run(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.0,
                        check=False,
                    )
                    if proc.returncode == 0:
                        log("Asked Linux desktop to minimize EasyEffects window after restore")
                        return
                except Exception as exc:
                    log(f"Could not minimize EasyEffects window with {command[0]!r}: {exc!r}")

            time.sleep(0.35)

        log("Could not minimize EasyEffects window after restore; leaving it open")

    threading.Thread(target=worker, daemon=True, name="KDictateEasyEffectsMinimize").start()


def _start_easyeffects_windowed_then_hide(reason: str) -> None:
    # If it is already running, the remaining requirement is to hide the window.
    if _easyeffects_is_running():
        log(f"EasyEffects already running after WiVRn mode reason={reason!r}; hiding window")
        _hide_easyeffects_window_soon()
        return

    preferred = str(VR_AUDIO_STATE.get("easyeffects_kind") or "")
    prefix, kind = _easyeffects_command_prefix(preferred or None)

    if prefix is None:
        return

    now = time.time()
    last_attempt = float(VR_AUDIO_STATE.get("easyeffects_last_restore_attempt") or 0.0)

    # Do not hammer EasyEffects if the desktop is slow or the user has just left vr
    if now - last_attempt < 20.0:
        return

    VR_AUDIO_STATE["easyeffects_last_restore_attempt"] = now

    try:
        # Launch the normal app, not --gapplication-service.
        subprocess.Popen(
            prefix,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        log(f"Started EasyEffects normal app after WiVRn mode kind={kind!r} reason={reason!r}")
        _hide_easyeffects_window_soon()
    except Exception as exc:
        log(f"Could not start EasyEffects normal app after WiVRn mode: {exc!r}")


def _restore_easyeffects_after_vr() -> None:
    now = time.time()
    restore_until = float(VR_AUDIO_STATE.get("easyeffects_restore_until") or 0.0)

    should_restore = bool(
        VR_AUDIO_STATE.get("easyeffects_paused")
        or VR_AUDIO_STATE.get("easyeffects_was_running")
    )

    if should_restore:

        VR_AUDIO_STATE["easyeffects_paused"] = False
        VR_AUDIO_STATE["easyeffects_was_running"] = False
        VR_AUDIO_STATE["easyeffects_restore_until"] = now + 600.0
        _start_easyeffects_windowed_then_hide("vr-exit")
        return

    # For a few minutes after VR exit, if EasyEffects disappears again, reopen
    # it once in the normal app mode and hide it. This avoids a permanent
    # aggressive restart loop while still fixing the post-VR drop-out.
    if now < restore_until and not _easyeffects_is_running():
        _start_easyeffects_windowed_then_hide("post-vr-keepalive")
        return

    # If it is running during the restore window, still enforce the minimized
    # hidden state. This handles the case where launch worked but hide raced.
    if now < restore_until and _easyeffects_is_running():
        _hide_easyeffects_window_soon()
        return


def _restore_vr_audio_defaults() -> None:
    if not VR_AUDIO_STATE.get("active"):
        _remember_desktop_audio_defaults()
        _restore_easyeffects_after_vr()
        return

    now = time.time()
    restore_deadline = float(VR_AUDIO_STATE.get("restore_deadline") or 0.0)

    if restore_deadline <= 0.0:
        restore_deadline = now + 30.0
        VR_AUDIO_STATE["restore_deadline"] = restore_deadline

    saved_source = str(VR_AUDIO_STATE.get("restore_source") or "")
    saved_sink = str(VR_AUDIO_STATE.get("restore_sink") or "")

    sources = _pactl_nodes("sources")
    sinks = _pactl_nodes("sinks")

    restore_source, source_ready = _preferred_restore_target("source", saved_source, sources)
    restore_sink, sink_ready = _preferred_restore_target("sink", saved_sink, sinks)

    waiting_for: list[str] = []

    if restore_source and not source_ready:
        waiting_for.append(f"source={restore_source!r}")

    if restore_sink and not sink_ready:
        waiting_for.append(f"sink={restore_sink!r}")

    if waiting_for:
        VR_AUDIO_STATE["restore_attempts"] = int(VR_AUDIO_STATE.get("restore_attempts") or 0) + 1

        if now >= restore_deadline:
            VR_AUDIO_STATE["restore_deadline"] = now + 30.0

        log(
            "WiVRn audio restore waiting for previous device(s) "
            f"{' '.join(waiting_for)} "
            f"attempt={VR_AUDIO_STATE['restore_attempts']}"
        )
        _restore_easyeffects_after_vr()
        return

    source_done = True
    sink_done = True

    if restore_source and source_ready:
        source_done = _restore_audio_default("source", restore_source)

    if restore_sink and sink_ready:
        sink_done = _restore_audio_default("sink", restore_sink)

    if not (source_done and sink_done) and now < restore_deadline:
        VR_AUDIO_STATE["restore_attempts"] = int(VR_AUDIO_STATE.get("restore_attempts") or 0) + 1
        log(
            "WiVRn audio restore not verified yet; keeping restore state "
            f"attempt={VR_AUDIO_STATE['restore_attempts']} "
            f"source_done={source_done} sink_done={sink_done}"
        )
        _restore_easyeffects_after_vr()
        return

    _restore_easyeffects_after_vr()

    log(
        "WiVRn audio disappeared; restored previous desktop audio defaults "
        f"source={restore_source!r} sink={restore_sink!r}"
    )

    VR_AUDIO_STATE.update({
        "active": False,
        "restore_source": None,
        "restore_sink": None,
        "restore_deadline": 0.0,
        "restore_attempts": 0,
        "easyeffects_was_running": False,
    })

    _remember_desktop_audio_defaults()


class DiscordVoiceRPC:
    """Optional approved Discord RPC adapter; live streams are always routed separately.

    Voice scopes are restricted by Discord. Never read account tokens from client
    storage or assume a Rich Presence socket grants voice-setting access.
    """
    def __init__(self) -> None:
        self.data: dict = {}
        self.next_check = 0.0
        self.status = "stream routing (voice RPC not configured)"

    @staticmethod
    def _receive(sock: socket.socket) -> tuple[int, dict]:
        def exact(length: int) -> bytes:
            chunks = bytearray()
            while len(chunks) < length:
                block = sock.recv(length - len(chunks))
                if not block:
                    raise ConnectionError("Discord RPC closed")
                chunks.extend(block)
            return bytes(chunks)
        opcode, length = struct.unpack("<II", exact(8))
        if length > 1024 * 1024:
            raise ValueError("Discord RPC frame too large")
        return opcode, json.loads(exact(length).decode("utf-8"))

    @staticmethod
    def _send(sock: socket.socket, opcode: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        sock.sendall(struct.pack("<II", opcode, len(data)) + data)

    def _request(self, sock: socket.socket, command: str, args: dict) -> dict:
        nonce = uuid.uuid4().hex
        self._send(sock, 1, {"cmd": command, "args": args, "nonce": nonce})
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            opcode, reply = self._receive(sock)
            if opcode == 3:
                self._send(sock, 4, reply)
                continue
            if reply.get("nonce") != nonce:
                continue
            if reply.get("evt") == "ERROR":
                raise RuntimeError(str(reply.get("data", {}).get("message", "Discord RPC rejected request")))
            return reply.get("data", {})
        raise TimeoutError("Discord voice RPC response timed out")

    def sync(self, vr: bool, persist: Callable[[], bool]) -> None:
        client_id = os.environ.get("KDICTATE_DISCORD_RPC_CLIENT_ID", "").strip()
        token = os.environ.get("KDICTATE_DISCORD_RPC_ACCESS_TOKEN", "").strip()
        if not client_id or not token or time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 10.0
        if not hasattr(self, "owners"):
            self.owners = {}
        roots = [RUNTIME_DIR, RUNTIME_DIR / "app/com.discordapp.Discord",
                 RUNTIME_DIR / "app/dev.vencord.Vesktop", Path("/tmp")]
        paths = list(dict.fromkeys(root / f"discord-ipc-{i}" for root in roots for i in range(10)))
        found = False
        for path in paths:
            key = str(path)
            if not path.is_socket():
                owner = self.owners.pop(key, None)
                if owner:
                    owner.close()
                continue
            found = True
            sock = self.owners.get(key)
            try:
                if sock is None:
                    # A persistent authenticated connection owns the temporary
                    # override. Reconnecting every scan would oscillate preferences.
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    sock.connect(key)
                    self._send(sock, 0, {"v": 1, "client_id": client_id})
                    opcode, hello = self._receive(sock)
                    if opcode != 1 or hello.get("evt") != "READY":
                        raise RuntimeError("Voice RPC handshake unavailable")
                    self._request(sock, "AUTHENTICATE", {"access_token": token})
                    self.owners[key] = sock
                settings = self._request(sock, "GET_VOICE_SETTINGS", {})
                baseline = self.data.setdefault(key, {})
                pending = set(baseline.get("_pending", []))
                changes = {}
                for direction in ("input", "output"):
                    current = settings.get(direction, {}).get("device_id")
                    if current is None:
                        continue
                    if vr:
                        # Snapshot the current user choice for each new VR
                        # session. A pending recovery retains its old original.
                        if direction not in pending:
                            baseline[direction] = current
                        devices = settings.get(direction, {}).get("available_devices", [])
                        targets = [d for d in devices if "wivrn" in str(d.get("name", "")).lower()]
                        if len(targets) == 1 and current != targets[0]["id"]:
                            changes[direction] = {"device_id": targets[0]["id"]}
                    elif direction in pending:
                        if current != baseline.get(direction):
                            changes[direction] = {"device_id": baseline[direction]}
                        else:
                            pending.discard(direction)
                    else:
                        # Desktop observation is read-only. Never enforce an
                        # old baseline after the user changes their preferences.
                        baseline[direction] = current
                if changes:
                    if vr:
                        pending.update(changes)
                    baseline["_pending"] = sorted(pending)
                    if not persist():
                        continue
                    self._request(sock, "SET_VOICE_SETTINGS", changes)
                    settings = self._request(sock, "GET_VOICE_SETTINGS", {})
                    if not all(settings.get(k, {}).get("device_id") == v["device_id"] for k, v in changes.items()):
                        raise RuntimeError("Discord device selection readback did not match")
                    if not vr:
                        pending.difference_update(changes)
                baseline["_pending"] = sorted(pending)
                persist()
                if not vr:
                    # Closing RPC also releases Discord's temporary preference lock.
                    sock.close()
                    self.owners.pop(key, None)
                self.status = "voice preferences verified" if not vr else "voice preference override active"
            except Exception as exc:
                self.status = "stream routing; approved voice RPC unavailable"
                # Never include authentication credentials in status/log messages.
                if sock is not None:
                    sock.close()
                self.owners.pop(key, None)
        if not found:
            self.status = "client closed; stream routing resumes when voice starts"

    def release(self) -> None:
        for owner in getattr(self, "owners", {}).values():
            owner.close()
        self.owners = {}


@dataclasses.dataclass
class HeadsetDebouncer:
    """Connection evidence is independent of audio-node or VR-app lifetime."""
    stable: bool = False
    candidate: bool | None = None
    since: float = 0.0
    midpoint_checked: bool = False

    def observe(self, connected: bool | None, now: float) -> bool:
        if connected is None:
            self.candidate = None  # uncertainty cannot count as sustained evidence
            self.midpoint_checked = False
            return False
        if connected == self.stable:
            self.candidate = None
            self.midpoint_checked = False
            return False
        if self.candidate != connected:
            self.candidate = connected
            self.since = now
            self.midpoint_checked = False
            return False
        elapsed = now - self.since
        if connected and elapsed >= 7.0:
            self.stable = True
            self.candidate = None
            return True
        if not connected and elapsed >= 7.0 and not self.midpoint_checked:
            # The second seven seconds starts at this fresh check, not an old timer.
            self.midpoint_checked = True
            self.since = now
        elif not connected and self.midpoint_checked and elapsed >= 7.0:
            self.stable = False
            self.candidate = None
            self.midpoint_checked = False
            return True
        return False


class WiVRnTruth:
    def __init__(self) -> None:
        self.bus = None
        self.last_error = ""
        self.owner = ""

    def read(self) -> bool | None:
        try:
            from gi.repository import Gio, GLib
            if self.bus is None or self.bus.is_closed():
                self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            owner = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "GetNameOwner",
                GLib.Variant("(s)", ("io.github.wivrn.Server",)),
                GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 750, None)
            current_owner = owner.unpack()[0]
            if self.owner and self.owner != current_owner:
                self.owner = current_owner
                return None  # invalidate evidence across server restarts
            self.owner = current_owner
            value = self.bus.call_sync(current_owner, "/io/github/wivrn/Server",
                "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("io.github.wivrn.Server", "HeadsetConnected")),
                GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 750, None)
            connected = value.unpack()[0]
            self.last_error = ""
            return connected if isinstance(connected, bool) else None
        except Exception as exc:
            error = str(exc)
            if "NameHasNoOwner" in error or "ServiceUnknown" in error:
                self.owner = ""
                self.last_error = ""
                return False
            if error != self.last_error:
                log(f"WiVRn connection evidence unavailable: {error}")
            self.last_error = error
            return None


def _audio_identity(node: dict | None) -> dict:
    if not node:
        return {}
    props = node.get("properties", {})
    return {"name": node.get("name", ""), "properties": {
        key: str(props[key]) for key in ("device.serial", "device.bus_path",
            "device.name", "device.profile.name", "node.name", "alsa.card_name")
        if props.get(key)}}


def _resolve_audio_identity(identity: dict, nodes: list[dict]) -> str:
    exact = _node_by_name(nodes, str(identity.get("name", "")))
    if exact and not _is_wivrn_node(exact):
        return str(exact["name"])
    saved = identity.get("properties", {})
    strong = {k: v for k, v in saved.items() if k in {"device.serial", "device.bus_path", "device.name"}}
    if not strong:
        return ""
    matches = [n for n in nodes if not _is_wivrn_node(n) and all(
        str(n.get("properties", {}).get(k, "")) == v for k, v in strong.items()) and
        (not saved.get("device.profile.name") or
         n.get("properties", {}).get("device.profile.name") == saved["device.profile.name"])]
    return str(matches[0]["name"]) if len(matches) == 1 else ""


def _discord_stream_identity(item: dict, kind: str) -> str:
    props = item.get("properties", {})
    binary = Path(str(props.get("application.process.binary", ""))).name.lower()
    app_id = str(props.get("application.id", "")).lower()
    name = str(props.get("application.name", "")).lower()
    known = {"discord", "discordcanary", "discordptb", "vesktop", "legcord", "equicord"}
    client = binary if binary in known else ""
    if not client:
        ids = {"com.discordapp.discord": "discord", "dev.vencord.vesktop": "vesktop",
               "xyz.armcord.armcord": "legcord", "io.github.legcord.legcord": "legcord"}
        client = ids.get(app_id, "")
    if not client and name in known:
        client = name
    if not client:
        return ""
    role = str(props.get("media.role", "")).lower()
    media_name = str(props.get("media.name", "")).lower()
    if kind == "source-outputs" and (role in {"screen", "production"} or
            any(token in media_name for token in ("screenshare", "screen capture", "monitor capture"))):
        return ""
    restore_id = str(props.get("module-stream-restore.id", ""))
    return f"{client}|{kind}|{restore_id or role or 'voice'}"


class AudioCoordinator:
    """One background writer; dictation and UI only read its cached result."""
    def __init__(self) -> None:
        self.path = APP_DIR / "audio-state.json"
        self.truth = WiVRnTruth()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.pulse_changed = threading.Event()
        self.subscriber = None
        self.last_scan = 0.0
        atexit.register(self.shutdown)
        self.data = {"version": 1, "in_vr": False, "original": {},
                     "desktop": {}, "streams": {}, "last_applied": {}, "overridden": []}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("version") == 1:
                self.data.update(saved)
        except (OSError, ValueError, AttributeError):
            pass
        self.machine = HeadsetDebouncer(stable=bool(self.data["in_vr"]))
        self.last_saved = ""
        self.current = None
        self.next_scan = 0.0
        self.evidence = None
        self.evidence_lock = threading.RLock()
        self.preferences = DiscordVoiceRPC()
        self.preferences.data = self.data.setdefault("discord_preferences", {})
        VR_AUDIO_STATE.update({"active": self.machine.stable,
            "last_desktop_source": self.data["desktop"].get("source", {}).get("name", ""),
            "last_desktop_sink": self.data["desktop"].get("sink", {}).get("name", ""),
            "easyeffects_was_running": bool(self.data.get("easyeffects_was_running")),
            "easyeffects_kind": self.data.get("easyeffects_kind"),
            "easyeffects_paused": bool(self.data.get("easyeffects_paused"))})

    def persist(self) -> bool:
        encoded = json.dumps(self.data, sort_keys=True)
        if encoded == self.last_saved:
            return True
        try:
            atomic_json_write(self.path, self.data)
            self.last_saved = encoded
            return True
        except OSError as exc:
            log(f"Cannot save audio restoration journal; routing deferred: {exc}")
            return False

    def observe(self, observed: bool | None, now: float) -> None:
        if not wivrn_auto_audio_enabled():
            observed = False
        with self.evidence_lock:
            self.evidence = observed
            self.machine.observe(observed, now)
            VR_AUDIO_STATE["connection"] = "unknown" if observed is None else ("connected" if observed else "disconnected")
            VR_AUDIO_STATE["confirmation"] = self.machine.candidate
            VR_AUDIO_STATE["active"] = self.machine.stable
        self.wake.set()

    def may_route(self, vr: bool) -> bool:
        with self.evidence_lock:
            return self.evidence is not None and self.machine.stable == vr and (not vr or self.evidence is True)

    def process_state(self, now: float) -> None:
        with self.evidence_lock:
            observed, stable = self.evidence, self.machine.stable
        changed = stable != self.data["in_vr"]
        if changed:
            log(f"WiVRn confirmed headset {'connected' if stable else 'disconnected'}")
            self.data["in_vr"] = stable
            self.data["overridden"] = []
            if stable:
                if not self.data["original"]:
                    self.data["original"] = dict(self.data["desktop"])
                self.data["last_applied"] = {}
                self.data["easyeffects_was_running"] = _easyeffects_is_running()
            self.next_scan = 0.0
            self.preferences.next_check = 0.0
            self.persist()
        if self.pulse_changed.is_set() and now - self.last_scan >= 0.2:
            self.pulse_changed.clear()
            self.next_scan = 0.0
        if now >= self.next_scan:
            self.last_scan = now
            self.next_scan = now + 2.0
            self.reconcile(observed)

    def step(self, observed: bool | None, now: float) -> None:
        # Deterministic entry point for tests and one-shot diagnostics.
        self.observe(observed, now)
        self.process_state(now)

    def reconcile(self, observed: bool | None) -> None:
        if observed is None or (self.machine.stable and observed is False):
            self.current = None
            return
        sources, sinks = _pactl_nodes("sources"), _pactl_nodes("sinks")
        if not sources and not sinks:
            return
        defaults = {"source": _pactl_default("source"), "sink": _pactl_default("sink")}
        all_nodes = {"source": sources, "sink": sinks}
        original_source = self.data["original"].get("source") or self.data["desktop"].get("source", {})
        self.capture_desktop_source = _resolve_audio_identity(original_source, sources) or _first_non_wivrn_audio_name(sources)
        if not self.machine.stable:
            self.current = None
            # Only update baseline after originals have been restored.
            if not self.data["original"] and self.machine.candidate is not True:
                for kind, nodes in all_nodes.items():
                    node = _node_by_name(nodes, defaults[kind])
                    if node and not _is_wivrn_node(node):
                        self.data["desktop"][kind] = _audio_identity(node)
                        VR_AUDIO_STATE[f"last_desktop_{kind}"] = node["name"]
            for kind, identity in list(self.data["original"].items()):
                nodes = all_nodes[kind]
                target = _resolve_audio_identity(identity, nodes)
                if not target:
                    # A temporary desktop fallback never replaces the saved original.
                    target = _first_non_wivrn_audio_name(nodes)
                if target and defaults[kind] != target and self.may_route(False):
                    _set_default_audio(kind, target)
                if target and _pactl_default(kind) == target and target == _resolve_audio_identity(identity, nodes):
                    self.data["desktop"][kind] = identity
                    del self.data["original"][kind]
                    self.data["last_applied"].pop(kind, None)
            self._route_discord(False, sources, sinks)
            self.preferences.sync(False, lambda: self.may_route(False) and self.persist())
            if self.data.get("easyeffects_paused"):
                _restore_easyeffects_after_vr()
                if _easyeffects_is_running():
                    self.data["easyeffects_paused"] = False
                    self.data["easyeffects_was_running"] = False
            self.persist()
            return
        vr_source = next((n for n in sources if _is_wivrn_node(n)), None)
        vr_sink = next((n for n in sinks if _is_wivrn_node(n)), None)
        if not vr_source or not vr_sink:
            self.current = None
            return
        if not self.data["original"]:
            self.data["original"] = dict(self.data["desktop"])
        for kind, nodes in all_nodes.items():
            if kind not in self.data["original"]:
                node = _node_by_name(nodes, defaults[kind])
                if node and not _is_wivrn_node(node):
                    self.data["original"][kind] = _audio_identity(node)
        if not self.persist():
            return
        for kind, node in (("source", vr_source), ("sink", vr_sink)):
            previous = self.data["last_applied"].get(kind)
            if previous and defaults[kind] not in {previous, node["name"]}:
                if kind not in self.data["overridden"]:
                    self.data["overridden"].append(kind)
                    log(f"Preserving deliberate manual {kind} change during VR")
            if kind not in self.data["overridden"]:
                if defaults[kind] != node["name"] and self.may_route(True):
                    _set_default_audio(kind, node["name"])
                if _pactl_default(kind) == node["name"]:
                    self.data["last_applied"][kind] = node["name"]
        self.current = {"source": vr_source["name"], "sink": vr_sink["name"],
            "source_label": vr_source["label"], "sink_label": vr_sink["label"],
            "device_id": PULSE_SOURCE_PREFIX + vr_source["name"]}
        VR_AUDIO_STATE["easyeffects_was_running"] = bool(self.data.get("easyeffects_was_running"))
        # Journal intent before pausing; preserve package choice across a daemon restart.
        if easyeffects_vr_pause_enabled() and self.data.get("easyeffects_was_running"):
            self.data["easyeffects_paused"] = True
            if self.persist() and self.may_route(True):
                _pause_easyeffects_for_vr()
                self.data["easyeffects_kind"] = VR_AUDIO_STATE.get("easyeffects_kind")
        self._route_discord(True, sources, sinks)
        self.preferences.sync(True, lambda: self.may_route(True) and self.persist())
        self.persist()

    def _route_discord(self, vr: bool, sources: list[dict], sinks: list[dict]) -> None:
        for kind, nodes, endpoint, command in (("sink-inputs", sinks, "sink", "move-sink-input"),
                ("source-outputs", sources, "source", "move-source-output")):
            result = _pactl(["--format=json", "list", kind], timeout=2.0)
            if result is None or result.returncode:
                continue
            try:
                streams = json.loads(result.stdout)
            except ValueError:
                continue
            by_index = {str(n.get("index", "")): n for n in nodes}
            for stream in streams:
                identity = _discord_stream_identity(stream, kind)
                if not identity:
                    continue
                current = by_index.get(str(stream.get(endpoint, "")))
                if current is None:
                    continue
                saved = self.data["streams"].get(identity)
                owned = self.data.setdefault("stream_vr_active", {}).get(identity, False)
                if not vr:
                    if not owned and not _is_wivrn_node(current):
                        self.data["streams"][identity] = _audio_identity(current)
                        continue
                    original = _resolve_audio_identity(saved or {}, nodes)
                    target = original or _resolve_audio_identity(self.data["desktop"].get(endpoint, {}), nodes)
                    if not target:
                        target = _first_non_wivrn_audio_name(nodes)
                else:
                    if not owned and not _is_wivrn_node(current):
                        self.data["streams"][identity] = _audio_identity(current)
                    target = next((n["name"] for n in nodes if _is_wivrn_node(n)), "")
                if not target:
                    continue
                if target == current["name"]:
                    if vr or target == _resolve_audio_identity(saved or {}, nodes):
                        self.data["stream_vr_active"][identity] = vr
                    continue
                # Persist ownership intent before writes, so a crash after move
                # cannot erase the device to which this stream must be restored.
                self.data["stream_vr_active"][identity] = True
                if not self.persist():
                    return
                if not self.may_route(vr):
                    return
                moved = _pactl([command, str(stream["index"]), target], timeout=1.0)
                if moved is not None and moved.returncode == 0:
                    actual = _pactl(["--format=json", "list", kind], timeout=1.0)
                    try:
                        verified = next((item for item in json.loads(actual.stdout) if item["index"] == stream["index"]), None)
                        target_node = _node_by_name(nodes, target)
                        if verified and str(verified.get(endpoint)) == str(target_node.get("index")):
                            if vr or target == _resolve_audio_identity(saved or {}, nodes):
                                self.data["stream_vr_active"][identity] = vr
                    except (AttributeError, ValueError, TypeError):
                        pass

    def shutdown(self) -> None:
        self.stop.set()
        self.wake.set()
        self.preferences.release()
        if self.subscriber and self.subscriber.poll() is None:
            with contextlib.suppress(Exception):
                self.subscriber.terminate()

    def pulse_events(self) -> None:
        # New Discord streams (including an app launched during VR) route as
        # soon as they appear. Periodic reconciliation still recovers missed events.
        while not self.stop.is_set():
            try:
                env = {**os.environ, "LC_ALL": "C"}
                self.subscriber = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, env=env)
                for event in self.subscriber.stdout:
                    if self.stop.is_set():
                        break
                    if any(f" on {kind} " in event for kind in ("server", "source", "sink", "sink-input", "source-output")):
                        self.pulse_changed.set()
                        self.wake.set()
                self.subscriber.wait(timeout=1.0)
            except Exception:
                if self.subscriber and self.subscriber.poll() is None:
                    with contextlib.suppress(Exception):
                        self.subscriber.terminate()
            self.stop.wait(2.0)

    def run(self) -> None:
        def watch() -> None:
            while not self.stop.is_set():
                try:
                    self.observe(self.truth.read(), time.monotonic())
                except Exception as exc:
                    self.observe(None, time.monotonic())
                    log(f"WiVRn observer retrying: {exc!r}")
                self.stop.wait(0.5)
        # Headset timing never waits for pactl, application RPC, or inference.
        threading.Thread(target=watch, name="VerbatimHeadsetTruth", daemon=True).start()
        threading.Thread(target=self.pulse_events, name="VerbatimPulseEvents", daemon=True).start()
        while not self.stop.is_set():
            try:
                self.process_state(time.monotonic())
            except Exception as exc:
                log(f"Audio coordinator retrying after error: {exc!r}")
            self.wake.wait(0.5)
            self.wake.clear()


AUDIO_COORDINATOR: AudioCoordinator | None = None


def apply_wivrn_audio_if_available(*, force: bool = False) -> dict[str, str] | None:
    # Compatibility entry point. 'force' cannot bypass headset confirmation.
    return AUDIO_COORDINATOR.current if AUDIO_COORDINATOR and VR_AUDIO_STATE.get("active") else None


def start_background_vr_audio_monitor() -> None:
    global AUDIO_COORDINATOR, BACKGROUND_VR_AUDIO_MONITOR_STARTED
    with BACKGROUND_VR_AUDIO_MONITOR_LOCK:
        if BACKGROUND_VR_AUDIO_MONITOR_STARTED:
            return
        AUDIO_COORDINATOR = AudioCoordinator()
        BACKGROUND_VR_AUDIO_MONITOR_STARTED = True
    threading.Thread(target=AUDIO_COORDINATOR.run, name="VerbatimAudioCoordinator", daemon=True).start()


def _portaudio_pulse_bridge_index() -> int | None:
    try:
        import sounddevice as sd

        candidates: list[tuple[int, int]] = []

        for idx, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels") or 0) <= 0:
                continue

            name = str(dev.get("name") or "").strip().lower()

            if name == "pulse":
                candidates.append((0, idx))
            elif name == "pipewire":
                candidates.append((1, idx))
            elif "pulse" in name:
                candidates.append((2, idx))
            elif "pipewire" in name:
                candidates.append((3, idx))

        if candidates:
            return sorted(candidates)[0][1]
    except Exception as exc:
        log(f"Could not find PortAudio Pulse/PipeWire bridge: {exc!r}")

    return None

_PORTAUDIO_PULSE_BRIDGE_INDEX_CACHE: int | None | bool = False
_PORTAUDIO_PULSE_BRIDGE_INDEX_LOCK = threading.Lock()


def _cached_portaudio_pulse_bridge_index() -> int | None:
    global _PORTAUDIO_PULSE_BRIDGE_INDEX_CACHE

    with _PORTAUDIO_PULSE_BRIDGE_INDEX_LOCK:
        if _PORTAUDIO_PULSE_BRIDGE_INDEX_CACHE is not False:
            return _PORTAUDIO_PULSE_BRIDGE_INDEX_CACHE

        _PORTAUDIO_PULSE_BRIDGE_INDEX_CACHE = _portaudio_pulse_bridge_index()
        return _PORTAUDIO_PULSE_BRIDGE_INDEX_CACHE


def resolve_microphone_device_for_keybind(selected_mic: str) -> tuple[int | str | None, str]:
    """Fast keybind resolver.

    The background WiVRn monitor already keeps system defaults routed.
    On the keybind path, avoid fresh pactl scans unless the user selected a specific non-default device.
    """
    selected_mic = (selected_mic or "").strip()

    if not FAST_START_AUDIO:
        return resolve_microphone_device(selected_mic)

    if selected_mic in {"", WIVRN_AUTO_DEVICE_ID}:
        os.environ.pop("PULSE_SOURCE", None)
        # During the disconnect grace interval, capture from the cached desktop
        # input without changing global routing before its 14-second confirmation.
        if VR_AUDIO_STATE.get("connection") == "disconnected" and AUDIO_COORDINATOR:
            fallback = getattr(AUDIO_COORDINATOR, "capture_desktop_source", "")
            if fallback:
                os.environ["PULSE_SOURCE"] = fallback
        bridge_idx = _cached_portaudio_pulse_bridge_index()
        if bridge_idx is not None:
            return bridge_idx, "confirmed system audio via Pulse"
        return None, "system default fast path"

    return resolve_microphone_device(selected_mic)

def resolve_microphone_device(selected_mic: str) -> tuple[int | str | None, str]:
    selected_mic = (selected_mic or "").strip()

    if selected_mic in {"", WIVRN_AUTO_DEVICE_ID}:
        os.environ.pop("PULSE_SOURCE", None)
        active = apply_wivrn_audio_if_available(force=(selected_mic == WIVRN_AUTO_DEVICE_ID))

        if active is not None:
            bridge_idx = _portaudio_pulse_bridge_index()
            if bridge_idx is not None:
                return bridge_idx, f"WiVRn microphone via Pulse ({bridge_idx})"
            return None, "WiVRn microphone via system default"

    if selected_mic.startswith(PULSE_SOURCE_PREFIX):
        source_name = selected_mic[len(PULSE_SOURCE_PREFIX):]
        source = next((node for node in _pactl_nodes("sources") if node["name"] == source_name), None)

        if source is not None and _is_wivrn_node(source) and not VR_AUDIO_STATE.get("active"):
            # Persistent WiVRn nodes do not imply a connected headset.
            source = None
            source_name = _pactl_default("source")
        # PULSE_SOURCE selects this client's input without changing system defaults.
        if source_name:
            os.environ["PULSE_SOURCE"] = source_name

        bridge_idx = _portaudio_pulse_bridge_index()
        label = source.get("label") if source else source_name

        if bridge_idx is not None:
            return bridge_idx, f"{label} via Pulse ({bridge_idx})"

        return None, f"{label} via system default"

    device_arg: int | str | None = int(selected_mic) if selected_mic.isdigit() else (selected_mic or None)
    return device_arg, "default" if device_arg is None else str(device_arg)


def list_input_microphones() -> list[dict[str, str]]:
    devices = [{"id": "", "label": "System default"}]
    seen_ids = {""}

    wivrn_source, _wivrn_sink = _find_wivrn_audio()

    if wivrn_source is not None:
        devices.append({"id": WIVRN_AUTO_DEVICE_ID, "label": "WiVRn microphone (auto)"})
        seen_ids.add(WIVRN_AUTO_DEVICE_ID)

        source_id = PULSE_SOURCE_PREFIX + wivrn_source["name"]
        devices.append({
            "id": source_id,
            "label": _shorten_label(wivrn_source.get("label") or "WiVRn microphone"),
        })
        seen_ids.add(source_id)

    try:
        import sounddevice as sd

        for idx, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels") or 0) <= 0:
                continue

            name = str(dev.get("name") or f"Input {idx}").strip()
            device_id = str(idx)

            if device_id in seen_ids:
                continue

            devices.append({"id": device_id, "label": f"{name} ({idx})"})
            seen_ids.add(device_id)
    except Exception as exc:
        log(f"Could not enumerate microphones: {exc!r}")

    return devices


def set_microphone_device(device_id: str) -> None:
    global MIC_DEVICE
    MIC_DEVICE = device_id.strip()
    save_runtime_config({"KDICTATE_MIC_DEVICE": MIC_DEVICE})


def set_realtime_transcription(enabled: bool) -> None:
    global REALTIME_TRANSCRIPTION
    REALTIME_TRANSCRIPTION = enabled
    save_runtime_config({"KDICTATE_REALTIME_TRANSCRIPTION": "1" if enabled else "0"})


def apply_quality_profile(profile: str) -> tuple[str, str]:
    global KDICTATE_PROFILE, MODEL_NAME, WHISPER_CPP_MODEL

    KDICTATE_PROFILE = _normalize_profile(profile)
    MODEL_NAME = QUALITY_PROFILES[KDICTATE_PROFILE]

    updates = {
        "KDICTATE_PROFILE": KDICTATE_PROFILE,
        "KDICTATE_MODEL": MODEL_NAME,
    }

    if BACKEND == "whisper.cpp":
        WHISPER_CPP_MODEL = str(APP_DIR / f"models/ggml-{MODEL_NAME}.bin")
        updates["KDICTATE_WHISPER_CPP_MODEL"] = WHISPER_CPP_MODEL

    save_runtime_config(updates)

    manager = globals().get("ModelManager")
    if manager is not None:
        with contextlib.suppress(Exception):
            manager.clear()

    return KDICTATE_PROFILE, MODEL_NAME


KDICTATE_PROFILE = _normalize_profile(os.environ.get("KDICTATE_PROFILE"), os.environ.get("KDICTATE_MODEL"))
MODEL_NAME = os.environ.get("KDICTATE_MODEL", QUALITY_PROFILES[KDICTATE_PROFILE])
BACKEND = os.environ.get("KDICTATE_BACKEND", "faster-whisper").strip().lower()
DEVICE = os.environ.get("KDICTATE_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("KDICTATE_COMPUTE_TYPE", "float16")
LANGUAGE = os.environ.get("KDICTATE_LANGUAGE", "en")
WHISPER_CPP_BIN = os.environ.get("KDICTATE_WHISPER_CPP_BIN", str(APP_DIR / "whisper.cpp/build/bin/whisper-cli"))
WHISPER_CPP_MODEL = os.environ.get("KDICTATE_WHISPER_CPP_MODEL", str(APP_DIR / f"models/ggml-{MODEL_NAME}.bin"))
MIC_DEVICE = os.environ.get("KDICTATE_MIC_DEVICE", "").strip()
WIVRN_AUTO_AUDIO = wivrn_auto_audio_enabled()
REALTIME_TRANSCRIPTION = realtime_transcription_enabled()

# Keep KDictate hot like a system process.
# The keybind path should only show UI and start recording.
KEEP_MODEL_WARM = _truthy(os.environ.get("KDICTATE_KEEP_MODEL_WARM", "1"))
PRECREATE_OVERLAY = _truthy(os.environ.get("KDICTATE_PRECREATE_OVERLAY", "1"))
MODEL_WARMUP_DELAY_SECONDS = float(os.environ.get("KDICTATE_MODEL_WARMUP_DELAY_SECONDS", "1.0"))

# Fast path defaults.
# Avoid slow caret/AT-SPI scans and slow Pulse/WiVRn probing on the keybind path.
FAST_KEYBIND_START = _truthy(os.environ.get("KDICTATE_FAST_KEYBIND_START", "1"))
FAST_START_AUDIO = _truthy(os.environ.get("KDICTATE_FAST_START_AUDIO", "1"))
CARET_POSITION_ON_KEYBIND = _truthy(os.environ.get("KDICTATE_CARET_POSITION_ON_KEYBIND", "0"))

# Fast paste path.
# This reduces the delay after the UI says "Typing".
FAST_PASTE = _truthy(os.environ.get("KDICTATE_FAST_PASTE", "1"))
FAST_PASTE_RESTORE_CLIPBOARD = _truthy(os.environ.get("KDICTATE_FAST_PASTE_RESTORE_CLIPBOARD", "1"))
FAST_PASTE_CONTEXT_PROBE = _truthy(os.environ.get("KDICTATE_FAST_PASTE_CONTEXT_PROBE", "0"))
FAST_PASTE_OLD_CLIPBOARD_TIMEOUT = float(os.environ.get("KDICTATE_FAST_PASTE_OLD_CLIPBOARD_TIMEOUT", "0.16"))
FAST_PASTE_PRE_PASTE_DELAY = float(os.environ.get("KDICTATE_FAST_PASTE_PRE_PASTE_DELAY", "0.18"))
FAST_PASTE_RESTORE_DELAY = float(os.environ.get("KDICTATE_FAST_PASTE_RESTORE_DELAY", "1.4"))

# Start live preview sooner. The preview worker can catch up from recorded audio.
REALTIME_FIRST_CHUNK_SECONDS = float(os.environ.get("KDICTATE_REALTIME_FIRST_CHUNK_SECONDS", "0.75"))
REALTIME_MIN_INTERVAL_SECONDS = float(os.environ.get("KDICTATE_REALTIME_MIN_INTERVAL_SECONDS", "0.75"))
REALTIME_MIN_ADVANCE_SECONDS = float(os.environ.get("KDICTATE_REALTIME_MIN_ADVANCE_SECONDS", "0.45"))
MAX_RECORD_SECONDS = float(os.environ.get("KDICTATE_MAX_RECORD_SECONDS", "90"))
SILENCE_TO_FINISH_SECONDS = float(os.environ.get("KDICTATE_SILENCE_TO_FINISH_SECONDS", "1.55"))
REALTIME_SILENCE_TO_FINISH_SECONDS = float(os.environ.get("KDICTATE_REALTIME_SILENCE_TO_FINISH_SECONDS", "2.85"))


AUDIO_RMS_AVERAGE_WINDOW = max(2, int(os.environ.get("KDICTATE_RMS_AVERAGE_WINDOW", "10")))
AUDIO_NOISE_CALIBRATION_SECONDS = float(os.environ.get("KDICTATE_NOISE_CALIBRATION_SECONDS", "0.75"))
AUDIO_MIN_SPEECH_THRESHOLD = float(os.environ.get("KDICTATE_MIN_SPEECH_THRESHOLD", "0.0065"))
AUDIO_SPEECH_THRESHOLD_MULTIPLIER = float(os.environ.get("KDICTATE_SPEECH_THRESHOLD_MULTIPLIER", "2.65"))
AUDIO_LOWEST_FLOOR_HEADROOM = float(os.environ.get("KDICTATE_LOWEST_FLOOR_HEADROOM", "1.12"))

# Long dictations need the floor to keep learning because fan noise can ramp after the first calibration window.
AUDIO_ADAPTIVE_NOISE_SECONDS = float(os.environ.get("KDICTATE_ADAPTIVE_NOISE_SECONDS", "12.0"))
AUDIO_ADAPTIVE_NOISE_PERCENTILE = float(os.environ.get("KDICTATE_ADAPTIVE_NOISE_PERCENTILE", "22"))
AUDIO_ADAPTIVE_NOISE_RISE_SECONDS = float(os.environ.get("KDICTATE_ADAPTIVE_NOISE_RISE_SECONDS", "5.5"))
AUDIO_ADAPTIVE_NOISE_FALL_SECONDS = float(os.environ.get("KDICTATE_ADAPTIVE_NOISE_FALL_SECONDS", "28.0"))
AUDIO_NOISE_LEARN_MAX_SPEECH_RATIO = float(os.environ.get("KDICTATE_NOISE_LEARN_MAX_SPEECH_RATIO", "1.55"))

WINDOW_W = 330
WINDOW_H = 118
LISTENING_PREVIEW_EXTRA_H = 144
LISTENING_PREVIEW_WINDOW_H = WINDOW_H + LISTENING_PREVIEW_EXTRA_H
SETTINGS_EXTRA_H = 224
SETTINGS_MAX_EXTRA_H = 440
SETTINGS_WINDOW_H = WINDOW_H + SETTINGS_EXTRA_H


def _ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    _ensure_dirs()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def run(cmd: list[str], *, input_text: str | None = None, timeout: float = 4.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None

def repair_gui_environment_for_user_service() -> None:
    """Use the login session's imported environment, never guess another display."""
    os.environ.setdefault("XDG_RUNTIME_DIR", str(RUNTIME_DIR))
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and (RUNTIME_DIR / "bus").exists():
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={RUNTIME_DIR / 'bus'}"
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        try:
            result = run(["systemctl", "--user", "show-environment"], timeout=1.0)
            allowed = {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP"}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in allowed:
                    os.environ.setdefault(key, value)
        except Exception:
            pass


_SENTENCE_BOUNDARY_STARTERS = (
    "This", "That", "It", "There", "These", "Those",
    "Then", "So", "But", "However", "Now", "Next",
    "Also", "Finally", "Basically", "Actually", "Overall",
    "The", "A", "An", "I", "You", "We", "They", "He", "She",
    "If", "When", "Because", "Maybe", "Sometimes", "Today",
)

_REALTIME_LOOP_MIN_WORDS = int(os.environ.get("KDICTATE_REALTIME_LOOP_MIN_WORDS", "18"))
_REALTIME_REPEAT_MAX_RUNS = int(os.environ.get("KDICTATE_REALTIME_REPEAT_MAX_RUNS", "3"))
_REALTIME_MAX_TEXT_GROWTH_RATIO = float(os.environ.get("KDICTATE_REALTIME_MAX_TEXT_GROWTH_RATIO", "2.35"))


def _word_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text or "")


def looks_like_realtime_loop(text: str) -> bool:
    """Detect obvious Whisper realtime repetition before it reaches the preview.

    This only rejects pathological interim text. Final transcription still gets
    its normal full pass.
    """
    words = [word.lower() for word in _word_tokens(text)]
    if len(words) < _REALTIME_LOOP_MIN_WORDS:
        return False

    for ngram_size in range(2, 8):
        run = 1
        previous: tuple[str, ...] | None = None

        for idx in range(0, len(words) - ngram_size + 1, ngram_size):
            current = tuple(words[idx:idx + ngram_size])
            if current == previous:
                run += 1
                if run >= _REALTIME_REPEAT_MAX_RUNS:
                    return True
            else:
                run = 1
                previous = current

    tail = words[-24:]
    if len(tail) >= 18:
        unique_ratio = len(set(tail)) / float(len(tail))
        if unique_ratio < 0.34:
            return True

    return False


def repair_missing_sentence_punctuation(text: str) -> str:
    """Conservatively add a period where Whisper likely missed a sentence boundary."""
    if not text:
        return ""

    # Avoid very common noun-phrase starters like "The", "A", and "An".
    # They are too likely to be normal mid-sentence words.
    sentence_starters = set(_SENTENCE_BOUNDARY_STARTERS) - {"The", "A", "An", "That"}

    continuation_words = {
        "and", "or", "but", "because", "if", "when", "while", "although",
        "though", "unless", "since", "than", "that", "which", "who",
        "whom", "whose", "where", "whether", "as",
        "using", "called", "named", "like", "with", "from", "into",
        "onto", "about", "inside", "outside", "before", "after",
        "between", "during", "without", "within", "including",
        "for", "to", "of", "in", "on", "at", "by",
        "is", "are", "was", "were", "be", "been", "being", "am",
        "do", "does", "did", "can", "could", "would", "should",
        "will", "shall", "may", "might", "must", "have", "has", "had",
    }

    pronoun_like_starters = {
        "This", "It", "There", "These", "Those",
        "You", "We", "They", "He", "She",
    }

    likely_sentence_verbs = {
        "is", "are", "was", "were", "will", "would", "can", "could",
        "should", "has", "have", "had", "does", "do", "did", "looks",
        "seems", "feels", "works", "means", "needs", "goes",
    }

    def fix_boundary(match: re.Match) -> str:
        previous_word = match.group(1)
        next_word = match.group(2)

        if next_word not in sentence_starters:
            return match.group(0)

        if previous_word.lower() in continuation_words:
            return match.group(0)

        if len(previous_word) < 4 and not previous_word.isdigit() and next_word != "I":
            return match.group(0)

        before_words = _word_tokens(text[max(0, match.start() - 120):match.start(2)])
        after_words = _word_tokens(text[match.start(2):match.end(2) + 120])

        # Require a little context on both sides so short phrases do not get chopped up.
        if len(before_words) < 3 or len(after_words) < 2:
            return match.group(0)

        # "It/This/They/etc." are only split when the next word looks sentence-like.
        # This avoids cases like "delete This file" or "move These folders".
        if next_word in pronoun_like_starters:
            following_word = after_words[1].lower() if len(after_words) > 1 else ""
            if following_word not in likely_sentence_verbs:
                return match.group(0)

        return f"{previous_word}. {next_word}"

    return re.sub(r"\b([a-z0-9][a-z0-9']*)\s+([A-Z][A-Za-z']+)\b", fix_boundary, text)


def normalize_transcript_text(text: str, *, final: bool = False) -> str:
    """Lightly clean Whisper dictation without rewriting the user's words."""
    text = " ".join((text or "").split()).strip()
    if not text:
        return ""

    # A single spoken word is usually a spelling lookup rather than a sentence.
    if final and len(_word_tokens(text)) == 1:
        return text.rstrip(".").lower()

    # Normal spacing around punctuation.
    text = re.sub(r"\s+([,.;:!?%)\]\}])", r"\1", text)
    text = re.sub(r"([(\[\{])\s+", r"\1", text)
    text = re.sub(r"([.!?])([A-Za-z])", r"\1 \2", text)

    text = repair_missing_sentence_punctuation(text)

    # Capitalize only after clear sentence boundaries.
    def cap_sentence(match: re.Match) -> str:
        return match.group(1) + match.group(2).upper()

    text = re.sub(r"(^|[.!?]\s+)([a-z])", cap_sentence, text)

    # Final dictation should usually land as a complete sentence.
    if final and len(text) > 24 and text[-1] not in ".!?)]}\"'":
        text += "."

    return text


def send_socket(message: str, timeout: float = 2.0) -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(SOCKET_PATH))
    sock.sendall(message.encode("utf-8"))
    data = sock.recv(65536).decode("utf-8", "replace")
    sock.close()
    return data.strip()


def daemon_is_running() -> bool:
    try:
        return send_socket("status", timeout=0.5) == "running"
    except Exception:
        return False


def start_daemon_if_needed() -> bool:
    if daemon_is_running():
        return True

    wrapper = Path(os.environ.get("KDICTATE_WRAPPER", Path.home() / ".local/bin/kdictate")).expanduser()
    env = os.environ.copy()
    env.setdefault("KDICTATE_APPDIR", str(APP_DIR))
    with open(LOG_FILE, "a", encoding="utf-8") as out:
        subprocess.Popen(
            [str(wrapper), "daemon"],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

    deadline = time.time() + 8.0
    while time.time() < deadline:
        if daemon_is_running():
            return True
        time.sleep(0.12)
    return False


def _focused_text_interface():
    """Return the focused AT-SPI text interface, if the focused app exposes one."""
    try:
        import pyatspi
    except Exception:
        return None

    try:
        desktop = pyatspi.Registry.getDesktop(0)
        seen = 0

        def find_focused(obj, depth=0):
            nonlocal seen
            seen += 1

            if seen > 1400 or depth > 12:
                return None

            try:
                state = obj.getState()
                if state.contains(pyatspi.STATE_FOCUSED):
                    return obj
            except Exception:
                pass

            try:
                count = obj.childCount
            except Exception:
                count = 0

            for idx in range(count):
                try:
                    found = find_focused(obj[idx], depth + 1)
                    if found:
                        return found
                except Exception:
                    continue

            return None

        focused = find_focused(desktop)
        if not focused:
            return None

        try:
            return focused.queryText()
        except Exception:
            return None
    except Exception as exc:
        log(f"AT-SPI focused text lookup failed: {exc!r}")
        return None


def _focused_text_insertion_offset(text_iface) -> int:
    try:
        selection_count = int(text_iface.getNSelections())
        if selection_count > 0:
            start, end = text_iface.getSelection(0)
            return max(0, int(min(start, end)))
    except Exception:
        pass

    try:
        return max(0, int(text_iface.caretOffset))
    except Exception:
        return 0


def should_prefix_space_before_paste(text: str) -> bool:
    """Use AT-SPI when available to decide whether pasted dictation continues text."""
    if not text or text[:1].isspace():
        return False

    # Never prepend a space before punctuation-like dictated output.
    if text[:1] in ".,!?;:%)]}":
        return False

    text_iface = _focused_text_interface()
    if text_iface is None:
        return False

    # If the user selected text, paste should replace it directly.
    try:
        selection_count = int(text_iface.getNSelections())
        if selection_count > 0:
            start, end = text_iface.getSelection(0)
            if int(start) != int(end):
                return False
    except Exception:
        pass

    offset = _focused_text_insertion_offset(text_iface)
    if offset <= 0:
        return False

    previous = ""

    with contextlib.suppress(Exception):
        previous = text_iface.getText(max(0, offset - 1), offset) or ""

    if not previous:
        with contextlib.suppress(Exception):
            previous = text_iface.getText(max(0, offset - 64), offset) or ""

    if not previous:
        return False

    last = previous[-1]
    return not last.isspace() and last not in "([{"


def apply_contextual_leading_space(text: str) -> str:
    if should_prefix_space_before_paste(text):
        return " " + text
    return text


def desktop_session_type() -> str:
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session in {"wayland", "x11"}:
        return session
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11" if os.environ.get("DISPLAY") else "unknown"


_CONTEXT_PROBE_LOCK = threading.Lock()


def bounded_prefix_space(text: str) -> bool:
    if not _CONTEXT_PROBE_LOCK.acquire(blocking=False):
        return False
    reply = queue.Queue(maxsize=1)
    def probe():
        try:
            reply.put(should_prefix_space_before_paste(text))
        except Exception:
            reply.put(False)
        finally:
            _CONTEXT_PROBE_LOCK.release()
    threading.Thread(target=probe, name="VerbatimTextContext", daemon=True).start()
    try:
        return reply.get(timeout=0.18)
    except queue.Empty:
        return False


class DesktopTarget:
    def __init__(self) -> None:
        self.window = ""
        self.terminal = False
        self.clipboard_backend = ""

    def capture(self) -> None:
        self.window = ""
        self.terminal = False
        if desktop_session_type() != "x11" or not command_exists("xdotool"):
            return
        try:
            result = run(["xdotool", "getactivewindow"], timeout=0.35)
            if result.returncode == 0 and result.stdout.strip().isdigit():
                self.window = result.stdout.strip()
                # Stable Mint/Ubuntu releases can ship an xdotool version
                # predating getwindowclassname. WM_CLASS is available on X11.
                if command_exists("xprop"):
                    klass = run(["xprop", "-id", self.window, "WM_CLASS"], timeout=0.35)
                else:
                    klass = run(["xdotool", "getwindowclassname", self.window], timeout=0.35)
                self.terminal = any(s in klass.stdout.lower() for s in
                    ("terminal", "konsole", "alacritty", "kitty", "xterm", "tilix", "wezterm"))
        except Exception:
            pass

    def ready(self) -> bool:
        if not self.window or desktop_session_type() != "x11":
            return True
        try:
            active = run(["xdotool", "getactivewindow"], timeout=0.4)
            if active.stdout.strip() == self.window:
                return True
            title = run(["xdotool", "getwindowname", active.stdout.strip()], timeout=0.4)
            # Restore only a focus change caused by our overlay, never a user switch.
            if title.stdout.strip() in {APP_NAME, "Verbatim"}:
                return run(["xdotool", "windowactivate", "--sync", self.window], timeout=0.6).returncode == 0
            return False
        except Exception:
            return False


DESKTOP_TARGET = DesktopTarget()


def clipboard_backends() -> list[str]:
    x11 = [name for name in ("xclip", "xsel") if os.environ.get("DISPLAY") and command_exists(name)]
    wayland = ["wayland"] if os.environ.get("WAYLAND_DISPLAY") and command_exists("wl-copy") and command_exists("wl-paste") else []
    return (x11 + wayland) if desktop_session_type() == "x11" else (wayland + x11)


def clipboard_read(backend: str, timeout: float = 0.6) -> tuple[bool, str | None]:
    commands = {"wayland": ["wl-paste", "--no-newline", "--type", "text"],
                "xclip": ["xclip", "-selection", "clipboard", "-out"],
                "xsel": ["xsel", "--clipboard", "--output"]}
    try:
        result = run(commands[backend], timeout=timeout)
        return (True, result.stdout) if result.returncode == 0 else (False, None)
    except Exception:
        return False, None


def clipboard_publish(backend: str, value: str) -> tuple[bool, subprocess.Popen | None, str]:
    commands = {"wayland": ["wl-copy", "--foreground", "--type", "text/plain;charset=utf-8"],
                "xclip": ["xclip", "-selection", "clipboard", "-in", "-quiet"],
                "xsel": ["xsel", "--clipboard", "--input", "--nodetach"]}
    process = None
    confirmed = False
    try:
        process = subprocess.Popen(commands[backend], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
            start_new_session=True)
        process.stdin.write(value)
        process.stdin.close()
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            if process.poll() is not None and process.returncode != 0:
                return False, None, f"{backend} clipboard owner exited with status {process.returncode}"
            ok, actual = clipboard_read(backend, timeout=0.15)
            if ok and actual == value:
                confirmed = True
                return True, process if process.poll() is None else None, "clipboard verified"
            time.sleep(0.025)
        return False, None, f"{backend} clipboard readback did not match"
    except Exception as exc:
        return False, None, str(exc)
    finally:
        if process is not None:
            if not confirmed and process.poll() is None:
                with contextlib.suppress(Exception):
                    process.terminate()
                    process.wait(timeout=0.3)
                if process.poll() is None:
                    process.kill()
            with contextlib.suppress(Exception):
                process.stderr.close()


def configure_native_overlay(window) -> None:
    """Set X11 nonfocus hints before mapping, and request KDE's real blur."""
    surface = window.get_surface()
    if surface is not None and hasattr(surface, "set_focusable"):
        with contextlib.suppress(Exception):
            surface.set_focusable(False)
    if desktop_session_type() != "x11":
        return
    try:
        from gi.repository import GdkX11
        surface = window.get_surface()
        xid = GdkX11.X11Surface.get_xid(surface)
        lib = ctypes.CDLL(ctypes.util.find_library("X11"))
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        display = lib.XOpenDisplay(os.environ.get("DISPLAY", "").encode())
        if not display:
            return
        try:
            atom = lambda name: lib.XInternAtom(display, name.encode(), 0)
            def property_values(name: str, type_name: str, values: list[int]) -> None:
                array = (ctypes.c_ulong * max(1, len(values)))(*values)
                lib.XChangeProperty(display, xid, atom(name), atom(type_name), 32, 0, array, len(values))
            # ICCCM WM_HINTS: InputHint, input=false, initial state=normal.
            property_values("WM_HINTS", "WM_HINTS", [1, 0, 1, 0, 0, 0, 0, 0, 0])
            property_values("_NET_WM_WINDOW_TYPE", "ATOM", [atom("_NET_WM_WINDOW_TYPE_UTILITY")])
            property_values("_NET_WM_STATE", "ATOM", [atom("_NET_WM_STATE_SKIP_TASKBAR"), atom("_NET_WM_STATE_SKIP_PAGER")])
            if active_theme().startswith("glass-"):
                property_values("_KDE_NET_WM_BLUR_BEHIND_REGION", "CARDINAL", [])
            else:
                lib.XDeleteProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
                lib.XDeleteProperty(display, xid, atom("_KDE_NET_WM_BLUR_BEHIND_REGION"))
            lib.XFlush(display)
        finally:
            lib.XCloseDisplay(display)
    except Exception as exc:
        log(f"Optional X11 overlay hints unavailable: {exc!r}")


def position_native_overlay(window, x: int, y: int) -> None:
    if desktop_session_type() != "x11" or not window.get_realized():
        return
    try:
        from gi.repository import GdkX11
        xid = GdkX11.X11Surface.get_xid(window.get_surface())
        if command_exists("xdotool"):
            run(["xdotool", "windowmove", str(xid), str(x), str(y)], timeout=0.25)
    except Exception:
        pass


class GlassBackdrop:
    """Native compositor blur first; real Gaussian-blurred pixels as fallback.

    Fallback snapshots are frozen while the overlay is visible, preventing
    recursive self-capture and avoiding continuous screen-recording overhead.
    """
    def __init__(self) -> None:
        self.native = False
        self.bridge = None
        self.surface = None
        self.pixels = None
        self.bus = None
        self.pipeline = None
        self.session = None
        self.remote_fd = None
        self.portal_attempted = False
        self.permission_pending = False
        self.permission_finished = threading.Event()
        self.permission_finished.set()
        self.busy = False
        self.status = "not initialized"
        self.origin = (0, 0)
        self.logical_size = None
        self.lock = threading.RLock()
        atexit.register(self.close)

    def close(self) -> None:
        try:
            if self.pipeline is not None:
                from gi.repository import Gst
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
            if self.session and self.bus:
                from gi.repository import Gio
                self.bus.call_sync("org.freedesktop.portal.Desktop", self.session,
                    "org.freedesktop.portal.Session", "Close", None, None,
                    Gio.DBusCallFlags.NONE, 500, None)
                self.session = None
        except Exception:
            pass
        if self.remote_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.remote_fd)
            self.remote_fd = None

    def configure(self, window, width: int, height: int) -> bool:
        enabled = active_theme().startswith("glass-")
        if desktop_session_type() == "wayland":
            try:
                if self.bridge is None:
                    path = APP_DIR / "native/libverbatim-blur.so"
                    if not path.exists():
                        path = Path(__file__).resolve().parents[1] / "native/libverbatim-blur.so"
                    self.bridge = ctypes.CDLL(str(path))
                    self.bridge.verbatim_blur.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
                    self.bridge.verbatim_blur.restype = ctypes.c_int
                self.native = bool(self.bridge.verbatim_blur(hash(window), width, height, int(enabled))) and enabled
            except (OSError, AttributeError):
                self.native = False
        else:
            configure_native_overlay(window)
            self.native = False
            if enabled and command_exists("xprop"):
                try:
                    advertised = run(["xprop", "-root", "_NET_SUPPORTED"], timeout=0.25)
                    self.native = "_KDE_NET_WM_BLUR_BEHIND_REGION" in advertised.stdout
                except Exception:
                    pass
        if self.native:
            self.status = "native compositor blur"
        if not enabled:
            self.close()
            self.surface = self.pixels = None
        return self.native

    def _portal_request(self, method: str, signature: str, arguments: tuple) -> dict:
        from gi.repository import Gio, GLib
        token = "verbatim_" + uuid.uuid4().hex
        options = dict(arguments[-1])
        options["handle_token"] = GLib.Variant("s", token)
        expected = "/org/freedesktop/portal/desktop/request/" + self.bus.get_unique_name()[1:].replace(".", "_") + "/" + token
        response = queue.Queue(maxsize=1)
        def replied(connection, sender, path, interface, signal_name, parameters):
            if path == expected:
                with contextlib.suppress(queue.Full):
                    response.put_nowait(parameters.unpack())
        subscription = self.bus.signal_subscribe("org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request", "Response", expected, None,
            Gio.DBusSignalFlags.NONE, replied)
        try:
            self.bus.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.ScreenCast", method,
                GLib.Variant(signature, arguments[:-1] + (options,)), None,
                Gio.DBusCallFlags.NONE, 3000, None)
            # Permission dialogs are handled by the user's desktop, only when needed.
            code, results = response.get(timeout=60.0)
            if code:
                raise PermissionError("Screen permission was not granted")
            return results
        except Exception:
            # A timed-out dialog must close before any dictation paste proceeds.
            with contextlib.suppress(Exception):
                self.bus.call_sync("org.freedesktop.portal.Desktop", expected,
                    "org.freedesktop.portal.Request", "Close", None, None,
                    Gio.DBusCallFlags.NONE, 500, None)
            raise
        finally:
            self.bus.signal_unsubscribe(subscription)

    def _ensure_portal(self) -> None:
        if self.pipeline is not None:
            return
        self.permission_pending = True
        self.permission_finished.clear()
        try:
            self._open_portal()
        finally:
            self.permission_pending = False
            self.permission_finished.set()

    def _open_portal(self) -> None:
        if self.pipeline is not None:
            return
        if self.portal_attempted:
            raise RuntimeError("Screen permission unavailable; reselect a glass theme to retry")
        self.portal_attempted = True
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gio, GLib, Gst
        Gst.init(None)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        created = self._portal_request("CreateSession", "(a{sv})", ({
            "session_handle_token": GLib.Variant("s", "verbatim_" + uuid.uuid4().hex)},))
        self.session = created["session_handle"]
        options = {"types": GLib.Variant("u", 1), "multiple": GLib.Variant("b", False),
                   "cursor_mode": GLib.Variant("u", 1)}
        try:
            version = self.bus.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("org.freedesktop.portal.ScreenCast", "version")),
                None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
            if int(version) >= 4:
                options["persist_mode"] = GLib.Variant("u", 2)
                restore = os.environ.get("KDICTATE_GLASS_RESTORE_TOKEN", "")
                if restore:
                    options["restore_token"] = GLib.Variant("s", restore)
        except Exception:
            pass
        self.status = "waiting for desktop screen permission"
        self._portal_request("SelectSources", "(oa{sv})", (self.session, options))
        started = self._portal_request("Start", "(osa{sv})", (self.session, "", {}))
        streams = started.get("streams", [])
        if not streams:
            raise RuntimeError("The desktop returned no screen source")
        node, properties = streams[0]
        self.origin = tuple(properties.get("position", (0, 0)))
        self.logical_size = properties.get("logical_size") or properties.get("size")
        token = started.get("restore_token")
        if token:
            save_runtime_config({"KDICTATE_GLASS_RESTORE_TOKEN": str(token)})
        reply, descriptors = self.bus.call_with_unix_fd_list_sync("org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop", "org.freedesktop.portal.ScreenCast", "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session, {})), GLib.VariantType.new("(h)"),
            Gio.DBusCallFlags.NONE, 3000, None, None)
        self.remote_fd = descriptors.get(reply.unpack()[0])
        self.pipeline = Gst.parse_launch(f"pipewiresrc fd={self.remote_fd} path={int(node)} do-timestamp=true "
            "! videoconvert ! video/x-raw,format=RGB ! appsink name=backdrop emit-signals=false max-buffers=1 drop=true sync=false")
        self.status = "local PipeWire backdrop capture"

    def _portal_image(self):
        from PIL import Image
        from gi.repository import Gst
        self._ensure_portal()
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            sink = self.pipeline.get_by_name("backdrop")
            # Drop a queued older frame; the overlay has been hidden before this call.
            sink.emit("try-pull-sample", 0)
            sample = sink.emit("try-pull-sample", int(1.5 * Gst.SECOND))
            if sample is None:
                raise RuntimeError("No screen frame was delivered")
            structure = sample.get_caps().get_structure(0)
            width, height = structure.get_value("width"), structure.get_value("height")
            buffer = sample.get_buffer()
            ok, mapping = buffer.map(Gst.MapFlags.READ)
            if not ok:
                raise RuntimeError("Cannot read the screen frame")
            try:
                stride = len(mapping.data) // height
                return Image.frombytes("RGB", (width, height), bytes(mapping.data), "raw", "RGB", stride, 1)
            finally:
                buffer.unmap(mapping)
        finally:
            # Keep authorization/session, suspend frame processing until next opening.
            self.pipeline.set_state(Gst.State.PAUSED)

    def capture(self, x: int, y: int, width: int, height: int, scale: float = 1.0) -> bool:
        try:
            from PIL import ImageGrab, ImageFilter
            import cairo
            if desktop_session_type() == "x11":
                image = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY"))
                self.status = "local X11 Gaussian backdrop blur"
            else:
                self._ensure_portal()
                image = self._portal_image()
                if self.logical_size:
                    scale = image.width / max(1, self.logical_size[0])
            ox, oy = self.origin
            left, top = int((x - ox) * scale), int((y - oy) * scale)
            if left < 0 or top < 0 or left + int(width * scale) > image.width:
                raise RuntimeError("Choose the monitor containing the dictation card for glass capture")
            # Blur a small padded crop, rather than an entire high-resolution desktop.
            padding = 40
            physical_padding = int(padding * scale)
            crop = image.crop((left - physical_padding, top - physical_padding,
                left + int(width * scale) + physical_padding, top + int(height * scale) + physical_padding))
            crop = crop.resize((width + padding * 2, height + padding * 2))
            crop = crop.filter(ImageFilter.GaussianBlur(radius=22)).crop((padding, padding, width + padding, height + padding))
            pixels = bytearray(crop.convert("RGBA").tobytes("raw", "BGRA"))
            surface = cairo.ImageSurface.create_for_data(pixels, cairo.FORMAT_ARGB32, width, height, width * 4)
            with self.lock:
                self.pixels, self.surface = pixels, surface
            return True
        except Exception as exc:
            self.status = str(exc)
            log(f"Glass backdrop: {exc}")
            return False


class InputInjector:
    """Persistent virtual keyboard used to send editing shortcuts on Wayland."""

    KEY_LEFTCTRL = 29
    KEY_RIGHTCTRL = 97
    KEY_LEFTSHIFT = 42
    KEY_C = 46
    KEY_V = 47
    KEY_LEFT = 105
    KEY_RIGHT = 106

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ui = None
        self._ready_at = 0.0
        self._failure: str | None = None

    @property
    def failure(self) -> str | None:
        return self._failure

    def ensure(self) -> bool:
        with self._lock:
            if self._ui is not None:
                return True
            try:
                from evdev import UInput, ecodes

                caps = {
                    ecodes.EV_KEY: [
                        self.KEY_LEFTCTRL,
                        self.KEY_RIGHTCTRL,
                        self.KEY_LEFTSHIFT,
                        self.KEY_C,
                        self.KEY_V,
                        self.KEY_LEFT,
                        self.KEY_RIGHT,
                    ]
                }
                self._ui = UInput(caps, name="KDictate Virtual Keyboard", version=0x0003)
                self._ready_at = time.time() + 0.85
                self._failure = None
                log("Created persistent /dev/uinput virtual keyboard")
                return True
            except Exception as exc:
                self._ui = None
                self._failure = repr(exc)
                log(f"Could not create /dev/uinput virtual keyboard: {exc!r}")
                return False

    def _ydotool_env(self) -> dict[str, str]:
        env = os.environ.copy()
        runtime_socket = RUNTIME_DIR / ".ydotool_socket"
        if runtime_socket.exists():
            env["YDOTOOL_SOCKET"] = str(runtime_socket)
        elif Path("/tmp/.ydotool_socket").exists():
            env["YDOTOOL_SOCKET"] = "/tmp/.ydotool_socket"
        return env

    def _emit_raw_key_events(self, events: list[tuple[int, int]], *, delay: float = 0.022) -> bool:
        if self.ensure() and self._ui is not None:
            from evdev import ecodes
            pressed = set()
            ready_delay = self._ready_at - time.time()
            if ready_delay > 0:
                time.sleep(ready_delay)
            with self._lock:
                try:
                    for code, value in events:
                        self._ui.write(ecodes.EV_KEY, code, value)
                        if value:
                            pressed.add(code)
                        else:
                            pressed.discard(code)
                        self._ui.syn()
                        time.sleep(delay)
                    return True
                except Exception as exc:
                    log(f"uinput injection interrupted: {exc!r}")
                    # An injected prefix is ambiguous: retry could duplicate paste.
                    return False
                finally:
                    for code in pressed:
                        with contextlib.suppress(Exception):
                            self._ui.write(ecodes.EV_KEY, code, 0)
                    with contextlib.suppress(Exception):
                        self._ui.syn()
        if command_exists("ydotool"):
            try:
                result = subprocess.run(["ydotool", "key", *[f"{code}:{value}" for code, value in events]],
                    env=self._ydotool_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=2.0, check=False)
                return result.returncode == 0
            except Exception as exc:
                log(f"ydotool injection unavailable: {exc!r}")
        return False

    def paste_shortcut(self, *, terminal: bool = False) -> bool:
        if desktop_session_type() == "x11" and command_exists("xdotool"):
            try:
                shortcut = "ctrl+shift+v" if terminal else "ctrl+v"
                result = run(["xdotool", "key", "--clearmodifiers", shortcut], timeout=0.8)
                # Once XTEST has been sent, do not inject a second paste on uncertainty.
                return result.returncode == 0
            except Exception:
                return False
        events = [
            (self.KEY_LEFTCTRL, 1),
            (self.KEY_V, 1),
            (self.KEY_V, 0),
            (self.KEY_LEFTCTRL, 0),
        ]
        if terminal:
            events.insert(1, (self.KEY_LEFTSHIFT, 1))
            events.insert(-1, (self.KEY_LEFTSHIFT, 0))
        return self._emit_raw_key_events(events, delay=0.030)

    def copy_shortcut(self) -> bool:
        if desktop_session_type() == "x11" and command_exists("xdotool"):
            with contextlib.suppress(Exception):
                return run(["xdotool", "key", "--clearmodifiers", "ctrl+c"], timeout=0.8).returncode == 0
        return self._emit_raw_key_events([
            (self.KEY_LEFTCTRL, 1),
            (self.KEY_C, 1),
            (self.KEY_C, 0),
            (self.KEY_LEFTCTRL, 0),
        ], delay=0.026)

    def select_previous_character(self) -> bool:
        return self._emit_raw_key_events([
            (self.KEY_LEFTSHIFT, 1),
            (self.KEY_LEFT, 1),
            (self.KEY_LEFT, 0),
            (self.KEY_LEFTSHIFT, 0),
        ], delay=0.022)

    def collapse_selection_to_original_caret(self) -> bool:
        # After Shift+Left selects the previous character, Right collapses the
        # selection at the original caret position.
        return self._emit_raw_key_events([
            (self.KEY_RIGHT, 1),
            (self.KEY_RIGHT, 0),
        ], delay=0.022)



class ClipboardPaster:
    def __init__(self, injector: InputInjector, pause_monitor: Callable[[float], None]) -> None:
        self.injector = injector
        self.pause_monitor = pause_monitor
        self.delivery_lock = threading.RLock()
        self.serial = 0
        self.backend = ""

    def _read_clipboard_text(self, timeout: float = 0.8) -> tuple[bool, str | None]:
        backend = getattr(self, "backend", "")
        candidates = [backend] if backend else clipboard_backends()
        for candidate in candidates:
            ok, value = clipboard_read(candidate, timeout)
            if ok:
                return True, value
        return False, None

    def _set_clipboard_text(self, value: str, label: str) -> tuple[bool, str, subprocess.Popen | None]:
        for backend in ([self.backend] if getattr(self, "backend", "") else clipboard_backends()):
            ok, owner, message = clipboard_publish(backend, value)
            if ok:
                self.backend = backend
                return True, message, owner
        return False, "No working clipboard backend for this desktop session", None

    def _target_has_active_selection(self) -> bool:
        sentinel = f"__KDICTATE_SELECTION_PROBE_{os.getpid()}_{time.time_ns()}__"

        ok, _msg, _owner = self._set_clipboard_text(sentinel, "selection probe")
        if not ok:
            return False

        time.sleep(0.06)

        if not self.injector.copy_shortcut():
            return False

        time.sleep(0.11)

        read_ok, copied = self._read_clipboard_text(timeout=0.7)
        if not read_ok or copied is None:
            return False

        return copied != sentinel and copied != ""

    def _probe_previous_character_for_spacing(self) -> bool:
        """Fallback for browsers/Electron apps that do not expose AT-SPI text.

        This probes the focused text field without permanently changing it:
        set sentinel clipboard -> Shift+Left -> Ctrl+C -> read selected char
        -> Right to collapse selection back to the original caret.
        """
        if not command_exists("wl-copy") or not command_exists("wl-paste"):
            return False

        # If text is already selected, the dictation should replace it directly.
        # Do not add a leading space.
        if self._target_has_active_selection():
            return False

        sentinel = f"__KDICTATE_CHAR_PROBE_{os.getpid()}_{time.time_ns()}__"

        ok, _msg, _owner = self._set_clipboard_text(sentinel, "character probe")
        if not ok:
            return False

        time.sleep(0.06)

        if not self.injector.select_previous_character():
            return False

        time.sleep(0.06)

        try:
            if not self.injector.copy_shortcut():
                return False

            time.sleep(0.12)

            read_ok, copied = self._read_clipboard_text(timeout=0.7)
            if not read_ok or copied is None:
                return False

            if copied == sentinel or copied == "":
                return False

            previous_char = copied[-1]
            return not previous_char.isspace() and previous_char not in "([{"
        finally:
            self.injector.collapse_selection_to_original_caret()
            time.sleep(0.035)

    def _should_prefix_space(self, text: str) -> bool:
        if not text or text[:1].isspace():
            return False

        if text[:1] in ".,!?;:%)]}":
            return False

        if should_prefix_space_before_paste(text):
            return True

        return self._probe_previous_character_for_spacing()

    def paste_text(self, text: str) -> tuple[bool, str]:
        # Serialized publication/injection prevents two sessions interleaving.
        if not hasattr(self, "delivery_lock"):
            self.delivery_lock = threading.RLock()
            self.serial = 0
        with self.delivery_lock:
            return self.paste_text_fast(text)

    def paste_text_fast(self, text: str) -> tuple[bool, str]:
        if not clipboard_backends():
            return False, "No usable clipboard. Install xclip for X11 or wl-clipboard for Wayland."
        if not DESKTOP_TARGET.ready():
            return False, "The focused window changed. Your transcript is available with kdictate last-transcript."
        self.backend = ""
        self.serial += 1
        serial = self.serial
        previous = {backend: clipboard_read(backend, 0.25) for backend in clipboard_backends()}
        if text and not text[:1].isspace() and text[:1] not in ".,!?;:%)]}" and bounded_prefix_space(text):
            text = " " + text
        ok, message, owner = self._set_clipboard_text(text, "dictation")
        if not ok:
            return False, message
        old_ok, old_clip = previous.get(self.backend, (False, None))
        self.pause_monitor(2.0)
        time.sleep(max(0.03, FAST_PASTE_PRE_PASTE_DELAY))
        if not DESKTOP_TARGET.ready():
            return False, "Focus changed before paste. Text is on the clipboard and in kdictate last-transcript."
        if not self.injector.paste_shortcut(terminal=DESKTOP_TARGET.terminal):
            # After ambiguous injection, keep the transcript; never retry blindly.
            return False, "Paste could not be confirmed. Text is on the clipboard and in kdictate last-transcript."
        if old_ok and old_clip is not None and FAST_PASTE_RESTORE_CLIPBOARD:
            backend = self.backend
            def restore() -> None:
                time.sleep(max(0.5, FAST_PASTE_RESTORE_DELAY))
                with self.delivery_lock:
                    if self.serial != serial or (owner is not None and owner.poll() is not None):
                        return
                    ok, actual = clipboard_read(backend, 0.25)
                    if ok and actual == text:
                        clipboard_publish(backend, old_clip)
            threading.Thread(target=restore, name="VerbatimClipboardRestore", daemon=True).start()
        return True, "paste shortcut delivered"


class KeyboardMonitor:
    """Cancels dictation when the real user starts typing.

    This intentionally does not log or store keys. It only detects that some real
    key went down while dictation is active. Permission is usually granted by the
    installer through udev/uaccess or the input group.
    """

    def __init__(self, on_manual_key: Callable[[], None]) -> None:
        self.on_manual_key = on_manual_key
        self.enabled = False
        self.ignore_until = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._devices = []
        self._last_fire = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def arm(self, ignore_for: float = 0.75) -> None:
        self.enabled = True
        self.ignore_until = time.time() + ignore_for

    def disarm(self) -> None:
        self.enabled = False

    def pause_for(self, seconds: float) -> None:
        self.ignore_until = max(self.ignore_until, time.time() + seconds)

    def _open_devices(self):
        from evdev import InputDevice, ecodes, list_devices

        devices = []
        for path in list_devices():
            try:
                dev = InputDevice(path)
                name = (dev.name or "").lower()
                if "kdictate virtual keyboard" in name:
                    dev.close()
                    continue
                caps = dev.capabilities(absinfo=False)
                keys = caps.get(ecodes.EV_KEY, [])
                if not keys:
                    dev.close()
                    continue
                keyset = set(keys if isinstance(keys, list) else list(keys))
                has_letters = any(k in keyset for k in range(ecodes.KEY_A, ecodes.KEY_Z + 1))
                has_space = ecodes.KEY_SPACE in keyset
                if has_letters or has_space:
                    dev.grab_context = None
                    devices.append(dev)
                else:
                    dev.close()
            except Exception:
                continue
        return devices

    def _run(self) -> None:
        try:
            from evdev import ecodes
        except Exception as exc:
            log(f"Keyboard monitor unavailable: {exc!r}")
            return

        while not self._stop.is_set():
            if not self._devices:
                try:
                    self._devices = self._open_devices()
                    if len(self._devices) > 0:
                        log(f"Keyboard monitor opened {len(self._devices)} keyboard device(s)")
                except Exception as exc:
                    log(f"Keyboard monitor could not open input devices: {exc!r}")
                    time.sleep(4.0)
                    continue

            try:
                r, _, _ = select.select(self._devices, [], [], 1.0)
            except Exception as exc:
                log(f"Keyboard monitor select failed: {exc!r}")
                for dev in self._devices:
                    with contextlib.suppress(Exception):
                        dev.close()
                self._devices = []
                time.sleep(1.0)
                continue

            for dev in r:
                try:
                    for ev in dev.read():
                        if ev.type != ecodes.EV_KEY or ev.value != 1:
                            continue
                        if not self.enabled or time.time() < self.ignore_until:
                            continue
                        now = time.time()
                        if now - self._last_fire < 0.35:
                            continue
                        self._last_fire = now
                        log("Manual keypress detected; cancelling dictation")
                        self.on_manual_key()
                except OSError:
                    with contextlib.suppress(Exception):
                        dev.close()
                    if dev in self._devices:
                        self._devices.remove(dev)
                except Exception as exc:
                    log(f"Keyboard monitor read failed: {exc!r}")



class ModelManager:
    _model = None
    _model_name: str | None = None
    _lock = threading.Lock()
    _loading = False
    _load_error: str | None = None

    @classmethod
    def is_loading(cls) -> bool:
        return cls._loading

    @classmethod
    def clear(cls) -> None:
        if not os.environ.get("KDICTATE_INFERENCE_FD"):
            threading.Thread(target=INFERENCE.close, daemon=True).start()
        with cls._lock:
            cls._model = None
            cls._model_name = None
            cls._load_error = None

    @classmethod
    def load(cls):
        """Load only the Python faster-whisper model.

        whisper.cpp is an external binary backend and does not need a long-lived
        Python model object.
        """
        if BACKEND == "whisper.cpp":
            return None

        with cls._lock:
            if cls._model is not None and cls._model_name == MODEL_NAME:
                return cls._model

            cls._model = None
            cls._model_name = None
            cls._loading = True
            cls._load_error = None

            log(f"Loading faster-whisper model={MODEL_NAME} device={DEVICE} compute_type={COMPUTE_TYPE}")

            try:
                from faster_whisper import WhisperModel
                cls._model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
                cls._model_name = MODEL_NAME
                log("Whisper model loaded")
                return cls._model
            except Exception as exc:
                cls._load_error = repr(exc)
                log(f"Whisper model failed to load: {exc!r}\n{traceback.format_exc()}")
                raise
            finally:
                cls._loading = False

    @classmethod
    def is_loaded(cls) -> bool:
        if not os.environ.get("KDICTATE_INFERENCE_FD"):
            return INFERENCE.loaded
        return cls._model is not None and cls._model_name == MODEL_NAME

    @classmethod
    def warm_async(cls, *, reason: str = "warmup", delay: float = 0.0) -> None:
        if cls.is_loaded() or cls._loading:
            return

        def worker() -> None:
            if delay > 0:
                time.sleep(delay)

            if cls.is_loaded() or cls._loading:
                return

            try:
                log(f"Starting async model warmup reason={reason!r}")
                cls._loading = True
                INFERENCE.request(None)
                log(f"Async model warmup complete reason={reason!r}")
            except Exception as exc:
                log(f"Async model warmup failed reason={reason!r}: {exc!r}")
            finally:
                cls._loading = False

        threading.Thread(
            target=worker,
            daemon=True,
            name="KDictateModelWarmup",
        ).start()

def transcribe_with_whisper_cpp(wav_path: str) -> str:
    """Transcribe via whisper.cpp, usually Vulkan on AMD/Intel/non-NVIDIA GPUs."""
    bin_path = Path(WHISPER_CPP_BIN).expanduser()
    model_path = Path(WHISPER_CPP_MODEL).expanduser()

    if not bin_path.exists():
        raise RuntimeError(f"whisper.cpp binary not found: {bin_path}")
    if not model_path.exists():
        # Quality can now be changed after install. If the requested ggml model
        # was not installed originally, download it using the existing
        # whisper.cpp helper before failing.
        download_script = bin_path.parents[2] / "models" / "download-ggml-model.sh" if len(bin_path.parents) >= 3 else None

        if download_script is not None and download_script.exists():
            model_path.parent.mkdir(parents=True, exist_ok=True)
            log(f"Downloading missing whisper.cpp model {MODEL_NAME} to {model_path.parent}")

            proc = subprocess.run(
                [str(download_script), MODEL_NAME, str(model_path.parent)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
                check=False,
            )

            if proc.returncode != 0:
                raise RuntimeError(
                    f"Could not download whisper.cpp model {MODEL_NAME}: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                )

        if not model_path.exists():
            raise RuntimeError(f"whisper.cpp model not found: {model_path}")

    # whisper-cli writes plain text to stdout with -otxt disabled by default in
    # newer builds; -nt removes timestamps, -np removes progress noise.
    cmd = [
        str(bin_path),
        "-m", str(model_path),
        "-f", wav_path,
        "-l", LANGUAGE,
        "-nt",
        "-np",
    ]

    log(f"Running whisper.cpp backend: {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=max(90.0, MAX_RECORD_SECONDS * 4.0),
        check=False,
    )

    if proc.returncode != 0:
        raise RuntimeError(f"whisper.cpp failed: {proc.stderr.strip() or proc.stdout.strip()}")

    text = proc.stdout.strip()
    # Some builds put useful text in stderr when progress/logging is enabled.
    if not text:
        lines = [
            line.strip()
            for line in proc.stderr.splitlines()
            if line.strip()
            and "whisper_" not in line
            and "system_info" not in line
            and "main:" not in line
        ]
        text = " ".join(lines).strip()

    return " ".join(text.split())


@dataclasses.dataclass
class AudioState:
    frames: list
    samplerate: int = 48000
    latest_rms: float = 0.0
    latest_avg_rms: float = 0.0
    lowest_avg_rms: float = 0.0
    noise_floor: float = 0.0
    adaptive_noise_floor: float = 0.0
    last_noise_adapt_at: float = 0.0
    started_at: float = 0.0
    last_speech_at: float = 0.0
    speech_seen: bool = False
    noise: list[float] = dataclasses.field(default_factory=list)
    rms_window: list[float] = dataclasses.field(default_factory=list)
    recent_avg_rms: list[tuple[float, float]] = dataclasses.field(default_factory=list)


class InferenceService:
    """A warm, killable worker. Native inference never runs on the GTK thread."""
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.process = None
        self.responses = queue.Queue()
        self.loaded = False
        self.active_kind = ""

    def close(self) -> None:
        with self.lock:
            process, self.process = self.process, None
            self.loaded = False
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=1.0)
                for stream in (process.stdin, process.stdout):
                    with contextlib.suppress(Exception):
                        stream.close()

    def _start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.loaded = False
        responses = queue.Queue()
        self.responses = responses
        # A socket isolates structured replies from libraries writing to stdout.
        parent, child = socket.socketpair()
        child.set_inheritable(True)
        env = os.environ.copy()
        env["KDICTATE_INFERENCE_FD"] = str(child.fileno())
        log_file = open(LOG_FILE, "a", encoding="utf-8")
        try:
            self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_inference-worker"],
                stdin=subprocess.PIPE, stdout=log_file, stderr=log_file,
                text=True, env=env, pass_fds=(child.fileno(),), start_new_session=True)
        except Exception:
            parent.close()
            raise
        finally:
            child.close()
            log_file.close()
        def read() -> None:
            try:
                with parent, parent.makefile("r", encoding="utf-8") as stream:
                    for line in stream:
                        responses.put(json.loads(line))
            except Exception:
                pass
            finally:
                responses.put({"error": "Inference worker exited"})
        threading.Thread(target=read, name="VerbatimInferenceReplies", daemon=True).start()

    def request(self, path: str | None, realtime: bool = False, audio_seconds: float = 0.0) -> str:
        # Preview work never waits behind a final job and can be discarded safely.
        if realtime:
            if not self.loaded or not self.lock.acquire(blocking=False):
                return ""
        elif not self.lock.acquire(timeout=1.0):
            # A loading model gets its bounded warmup budget; a preview gets one
            # second of grace, then final transcription takes priority.
            grace = 0.0 if self.active_kind == "preview" else float(os.environ.get("KDICTATE_INFERENCE_TIMEOUT", "180"))
            if not self.lock.acquire(timeout=grace):
                process = self.process
                if process is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                if not self.lock.acquire(timeout=4.0):
                    raise RuntimeError("Inference worker could not release the final job; audio retained")
        self.active_kind = "preview" if realtime else ("warmup" if path is None else "final")
        try:
            attempts = 1 if realtime else 2
            for attempt in range(attempts):
                self._start()
                identity = uuid.uuid4().hex
                request = {"id": identity, "path": path, "realtime": realtime,
                    "model": MODEL_NAME, "backend": BACKEND, "device": DEVICE,
                    "compute_type": COMPUTE_TYPE, "language": LANGUAGE,
                    "cpp_bin": WHISPER_CPP_BIN, "cpp_model": WHISPER_CPP_MODEL}
                try:
                    self.process.stdin.write(json.dumps(request) + "\n")
                    self.process.stdin.flush()
                    budget = float(os.environ.get("KDICTATE_INFERENCE_TIMEOUT", "180"))
                    budget = min(30.0, max(5.0, audio_seconds * 2.0)) if realtime else max(15.0, budget, audio_seconds * (8.0 if DEVICE == "cpu" else 3.0))
                    # Loading a local model is included, but downloads are never an
                    # unbounded operation in the visible dictation lifecycle.
                    deadline = time.monotonic() + budget
                    while True:
                        reply = self.responses.get(timeout=max(0.01, deadline - time.monotonic()))
                        if reply.get("id") not in {None, identity}:
                            continue
                        if reply.get("event") == "cpu-fallback":
                            deadline = max(deadline, time.monotonic() + max(180.0, audio_seconds * 8.0))
                            continue
                        if reply.get("error"):
                            raise RuntimeError(reply["error"])
                        self.loaded = True
                        return str(reply.get("text", ""))
                except (queue.Empty, OSError, RuntimeError) as exc:
                    self.close()
                    if attempt + 1 >= attempts:
                        raise RuntimeError("Transcription could not finish; captured audio was retained for recovery") from exc
                    log(f"Restarting inference worker after a failed final job: {exc}")
            return ""
        finally:
            self.active_kind = ""
            self.lock.release()


INFERENCE = InferenceService()
atexit.register(INFERENCE.close)


def inference_worker_main() -> int:
    global MODEL_NAME, BACKEND, DEVICE, COMPUTE_TYPE, LANGUAGE, WHISPER_CPP_BIN, WHISPER_CPP_MODEL
    fd = int(os.environ["KDICTATE_INFERENCE_FD"])
    force_cpu = False
    with socket.socket(fileno=fd) as replies:
        for line in sys.stdin:
            identity = None
            try:
                request = json.loads(line)
                identity = request["id"]
                MODEL_NAME, BACKEND = request["model"], request["backend"]
                DEVICE, COMPUTE_TYPE, LANGUAGE = request["device"], request["compute_type"], request["language"]
                WHISPER_CPP_BIN, WHISPER_CPP_MODEL = request["cpp_bin"], request["cpp_model"]
                if force_cpu and BACKEND == "faster-whisper":
                    DEVICE, COMPUTE_TYPE = "cpu", "int8"
                path = request.get("path")
                for attempt in range(2):
                    try:
                        if BACKEND == "whisper.cpp":
                            if not Path(WHISPER_CPP_BIN).is_file():
                                raise RuntimeError("whisper.cpp executable is missing")
                            text = transcribe_with_whisper_cpp(path) if path else ""
                        else:
                            model = ModelManager.load()
                            if path:
                                segments, _info = model.transcribe(path, language=LANGUAGE, task="transcribe",
                                    beam_size=1 if request["realtime"] else 5, vad_filter=False,
                                    condition_on_previous_text=False, temperature=0.0,
                                    no_speech_threshold=0.35, compression_ratio_threshold=2.4)
                                text = " ".join(segment.text.strip() for segment in segments).strip()
                            else:
                                text = ""
                        break
                    except Exception as exc:
                        gpu_error = any(word in str(exc).lower() for word in ("cuda", "cudnn", "cublas", "out of memory", "unsupported compute type"))
                        if attempt or DEVICE != "cuda" or not gpu_error:
                            raise
                        force_cpu = True
                        DEVICE, COMPUTE_TYPE = "cpu", "int8"
                        ModelManager.clear()
                        replies.sendall((json.dumps({"id": identity, "event": "cpu-fallback"}) + "\n").encode())
                        log("GPU runtime unavailable; retrying captured audio on CPU")
                reply = {"id": identity, "text": text}
            except Exception as exc:
                reply = {"id": identity, "error": str(exc)}
            replies.sendall((json.dumps(reply) + "\n").encode("utf-8"))
    return 0


class DictationEngine:
    def __init__(self, ui) -> None:
        self.ui = ui
        self.state = AudioState(frames=[])
        self.stream = None
        self.lock = threading.RLock()
        self.session_id = 0
        self.recording = False
        self.cancelled = False
        self.finalizing = False
        self.realtime_busy = False
        self.realtime_last_start = 0.0
        self.realtime_last_audio_seconds = 0.0
        self.realtime_preview_audio_seconds = 0.0
        self.realtime_last_good_text = ""
        self.realtime_last_good_audio_seconds = 0.0
        self.start_requested_at = 0.0
        self.last_audio_at = 0.0

    def _post(self, session: int, kind: str, *args) -> None:
        if session == self.session_id and not self.cancelled:
            self.ui.invoke_for_session(session, kind, *args)

    def start_async(self) -> None:
        session = self._begin()
        threading.Thread(target=self._open_audio, args=(session,), daemon=True,
                         name="VerbatimAudioStart").start()

    def start(self) -> None:
        self._open_audio(self._begin())

    def _begin(self) -> int:
        self.cancel("new session")
        with self.lock:
            self.session_id += 1
            self.state = AudioState(frames=[])
            self.cancelled = False
            self.recording = True
            self.finalizing = False
            self.realtime_busy = False
            self.realtime_last_start = 0.0
            self.realtime_last_audio_seconds = 0.0
            self.realtime_preview_audio_seconds = 0.0
            self.realtime_last_good_text = ""
            self.realtime_last_good_audio_seconds = 0.0
            self.start_requested_at = time.monotonic()
            self.last_audio_at = 0.0
            return self.session_id

    @staticmethod
    def _close_stream(stream) -> None:
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.abort()
            with contextlib.suppress(Exception):
                stream.close()

    def _open_audio(self, session: int) -> None:
        stream = None
        try:
            import numpy as np
            import sounddevice as sd
            device, label = resolve_microphone_device_for_keybind(os.environ.get("KDICTATE_MIC_DEVICE", "").strip())
            dev = sd.query_devices(device=device, kind="input") if device is not None else sd.query_devices(kind="input")
            samplerate = int(dev.get("default_samplerate") or 48000)
            with self.lock:
                if session != self.session_id or not self.recording:
                    return
                state = self.state
                state.samplerate = samplerate
            def callback(indata, count, time_info, status):
                if session != self.session_id or not self.recording:
                    return
                mono = indata[:, 0].astype(np.float32).copy()
                rms = float(np.sqrt(np.mean(mono * mono)))
                now = time.monotonic()
                with self.lock:
                    if session != self.session_id or not self.recording:
                        return
                    if not state.started_at:
                        state.started_at = now
                        state.last_speech_at = now
                    state.frames.append(mono)
                    state.latest_rms = rms
                    state.rms_window = (state.rms_window + [rms])[-AUDIO_RMS_AVERAGE_WINDOW:]
                    state.latest_avg_rms = float(np.mean(state.rms_window))
                    self.last_audio_at = now
                if status:
                    log(f"Audio callback status: {status}")
            stream = sd.InputStream(samplerate=samplerate, channels=1, dtype="float32",
                blocksize=0, device=device, callback=callback)
            with self.lock:
                if session != self.session_id or not self.recording:
                    self._close_stream(stream)
                    return
                self.stream = stream
            stream.start()
            if session != self.session_id or not self.recording:
                self._close_stream(stream)
                return
            log(f"Recording session={session} rate={samplerate} microphone={label!r}")
            threading.Thread(target=self._speech_worker, args=(session, state), daemon=True,
                             name="VerbatimSpeechDetector").start()
        except Exception as exc:
            self._close_stream(stream)
            if session == self.session_id:
                self.recording = False
                self._post(session, "error", f"Microphone failed: {exc}")

    def cancel(self, reason: str) -> None:
        with self.lock:
            self.cancelled = True
            self.recording = False
            self.finalizing = False
            self.session_id += 1
            stream, self.stream = self.stream, None
        if stream is not None:
            threading.Thread(target=self._close_stream, args=(stream,), daemon=True).start()

    def _speech_worker(self, session: int, state: AudioState) -> None:
        import numpy as np
        try:
            from faster_whisper.vad import get_speech_timestamps, VadOptions
            options = VadOptions(threshold=0.45, min_speech_duration_ms=64,
                min_silence_duration_ms=150, speech_pad_ms=0)
        except Exception:
            get_speech_timestamps = None
        previous_samples = 0
        floor = 0.001
        while session == self.session_id and self.recording:
            time.sleep(0.12)
            with self.lock:
                recent = []
                total = sum(len(f) for f in state.frames)
                remaining = int(state.samplerate * 1.5)
                for frame in reversed(state.frames):
                    recent.append(frame)
                    remaining -= len(frame)
                    if remaining <= 0:
                        break
                rms = state.latest_avg_rms
            if total == previous_samples or not recent:
                continue
            previous_samples = total
            audio = np.concatenate(list(reversed(recent)))
            speech_end = None
            if get_speech_timestamps is not None:
                try:
                    count = max(1, int(len(audio) * 16000 / state.samplerate))
                    resampled = np.interp(np.arange(count) * state.samplerate / 16000,
                        np.arange(len(audio)), audio).astype(np.float32)
                    segments = get_speech_timestamps(resampled, options, sampling_rate=16000)
                    if segments:
                        speech_end = (total - len(audio)) / state.samplerate + segments[-1]["end"] / 16000
                except Exception as exc:
                    log(f"Local speech detector falling back to energy: {exc}")
                    get_speech_timestamps = None
            else:
                # No startup calibration: early speech cannot poison the floor.
                if rms >= max(0.0018, floor * 2.2):
                    speech_end = total / state.samplerate
                elif rms > 1e-7:
                    floor += (rms - floor) * (0.04 if rms < floor else 0.01)
            with self.lock:
                if session != self.session_id or not self.recording:
                    return
                if speech_end is not None:
                    state.speech_seen = True
                    state.last_speech_at = max(state.last_speech_at, state.started_at + speech_end)
                state.noise_floor = floor

    def tick(self) -> None:
        if not self.recording:
            return
        now = time.monotonic()
        state = self.state
        self.ui.set_level(state.latest_avg_rms)
        if not state.started_at:
            if now - self.start_requested_at > 5.0:
                session = self.session_id
                with self.lock:
                    self.recording = False
                    stream, self.stream = self.stream, None
                threading.Thread(target=self._close_stream, args=(stream,), daemon=True).start()
                self._post(session, "error", "Microphone did not deliver audio. Check the selected input.")
            return
        age = now - state.started_at
        if self.last_audio_at and now - self.last_audio_at > 3.0:
            self.finish()  # preserve the usable part after a device interruption
            return
        if state.speech_seen:
            self.ui.set_status("listening", "Listening live" if realtime_transcription_enabled() else "Listening",
                "Transcribing as you speak." if realtime_transcription_enabled() else "Pause to finish.")
        if realtime_transcription_enabled():
            self._maybe_realtime_transcribe(now)
        if state.speech_seen and now - state.last_speech_at >= active_silence_to_finish_seconds() and age >= 0.7:
            self.finish()
        elif age >= MAX_RECORD_SECONDS or (not state.speech_seen and age >= 21.0):
            self.finish()  # uncertainty is resolved by final transcription, not discard

    def finish(self) -> None:
        with self.lock:
            if not self.recording or self.finalizing:
                return
            self.recording = False
            self.finalizing = True
            session = self.session_id
            stream, self.stream = self.stream, None
            frames, samplerate = list(self.state.frames), self.state.samplerate
        threading.Thread(target=self._close_stream, args=(stream,), daemon=True).start()
        self.ui.set_status("transcribing", "Transcribing", f"Whisper {MODEL_NAME} via {BACKEND}")
        threading.Thread(target=self._transcribe_worker, args=(frames, samplerate, session), daemon=True,
                         name="VerbatimFinalTranscription").start()

    def _write_wav_and_transcribe(self, frames, samplerate: int, *, realtime: bool, session: int) -> str:
        import numpy as np
        import soundfile as sf
        if not frames:
            return ""
        audio = np.concatenate(frames).astype(np.float32)
        if audio.size < samplerate * (0.65 if realtime else 0.15):
            return ""
        if float(np.max(np.abs(audio))) < 0.0001:
            return ""
        directory = RUNTIME_DIR if realtime else APP_DIR / "recovery"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, path = tempfile.mkstemp(prefix=f"verbatim-{session}-", suffix=".wav", dir=directory)
        os.close(fd)
        keep = False
        try:
            sf.write(path, audio, samplerate)
            if session != self.session_id or self.cancelled:
                return ""
            return INFERENCE.request(path, realtime, len(audio) / samplerate)
        except Exception:
            keep = not realtime
            if keep:
                log(f"Captured audio retained at {path}")
            raise
        finally:
            if not keep:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    def _maybe_realtime_transcribe(self, now: float) -> None:
        if self.realtime_busy or self.cancelled or now - self.realtime_last_start < REALTIME_MIN_INTERVAL_SECONDS:
            return
        with self.lock:
            frames, samplerate = list(self.state.frames), self.state.samplerate
        seconds = sum(len(f) for f in frames) / samplerate
        if seconds < REALTIME_FIRST_CHUNK_SECONDS or seconds - self.realtime_last_audio_seconds < REALTIME_MIN_ADVANCE_SECONDS:
            return
        self.realtime_busy = True
        self.realtime_last_start, self.realtime_last_audio_seconds = now, seconds
        threading.Thread(target=self._realtime_worker, args=(frames, samplerate, self.session_id), daemon=True).start()

    def _realtime_worker(self, frames, samplerate: int, session: int) -> None:
        try:
            text = self._write_wav_and_transcribe(frames, samplerate, realtime=True, session=session)
            text = normalize_transcript_text(text, final=False)
            if text and not looks_like_realtime_loop(text) and self.recording and session == self.session_id:
                seconds = sum(len(f) for f in frames) / samplerate
                self.realtime_last_good_text, self.realtime_last_good_audio_seconds = text, seconds
                self._post(session, "realtime", text, seconds)
        except Exception as exc:
            log(f"Live preview skipped: {exc}")
        finally:
            if session == self.session_id:
                self.realtime_busy = False

    def _transcribe_worker(self, frames, samplerate: int, session: int) -> None:
        try:
            text = self._write_wav_and_transcribe(frames, samplerate, realtime=False, session=session)
            text = normalize_transcript_text(text, final=True)
            if session != self.session_id or self.cancelled:
                return
            if text and not looks_like_realtime_loop(text):
                atomic_json_write(APP_DIR / "last-transcript.json", {"text": text, "time": time.time()})
                self._post(session, "transcribed", text)
            else:
                self._post(session, "cancel", "no speech recognized")
        except Exception as exc:
            self._post(session, "error", str(exc))
        finally:
            if session == self.session_id:
                self.finalizing = False


class SocketServer:
    def __init__(self, handler: Callable[[str], str]) -> None:
        self.handler = handler
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        _ensure_dirs()
        
        # Remove stale socket files. If another live daemon owns the socket,
        # bind() below will fail and this daemon will exit cleanly.
        with contextlib.suppress(FileNotFoundError):
            SOCKET_PATH.unlink()
        
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        
        try:
            srv.bind(str(SOCKET_PATH))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                log(f"Socket already in use at {SOCKET_PATH}; another daemon is already running")
                return
            raise
        
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(12)
        log(f"Socket listening at {SOCKET_PATH}")
        while True:
            conn, _ = srv.accept()
            with conn:
                try:
                    msg = conn.recv(65536).decode("utf-8", "replace").strip()
                    resp = self.handler(msg)
                    conn.sendall((resp + "\n").encode("utf-8"))
                except Exception as exc:
                    log(f"Socket command failed: {exc!r}")
                    with contextlib.suppress(Exception):
                        conn.sendall(f"error: {exc}\n".encode("utf-8"))


def atspi_caret_position() -> tuple[int, int] | None:
    try:
        import pyatspi
    except Exception as exc:
        log(f"AT-SPI unavailable: {exc!r}")
        return None

    try:
        desktop = pyatspi.Registry.getDesktop(0)
        seen = 0

        def find_focused(obj, depth=0):
            nonlocal seen
            seen += 1
            if seen > 1400 or depth > 12:
                return None
            try:
                state = obj.getState()
                if state.contains(pyatspi.STATE_FOCUSED):
                    return obj
            except Exception:
                pass
            try:
                count = obj.childCount
            except Exception:
                count = 0
            for idx in range(count):
                try:
                    found = find_focused(obj[idx], depth + 1)
                    if found:
                        return found
                except Exception:
                    continue
            return None

        focused = find_focused(desktop)
        if not focused:
            return None
        try:
            text = focused.queryText()
        except Exception:
            return None
        try:
            offset = max(0, int(text.caretOffset))
        except Exception:
            offset = 0
        for off in [offset, max(0, offset - 1), 0]:
            try:
                x, y, w, h = text.getCharacterExtents(off, pyatspi.DESKTOP_COORDS)
                if x > 0 and y > 0:
                    return int(x + 14), int(y + max(h, 22) + 10)
            except Exception:
                pass
    except Exception as exc:
        log(f"AT-SPI caret lookup failed: {exc!r}")
    return None


def daemon_main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    _ensure_dirs()

    # Prevent duplicate daemon instances from racing for the same UNIX socket.
    lock_fh = open(LOCK_PATH, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.seek(0)
        lock_fh.truncate()
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()
    except BlockingIOError:
        log("Another KDictate/Verbatim daemon already holds the daemon lock; exiting")
        return 0

    repair_gui_environment_for_user_service()

    # GTK imports are intentionally delayed so CLI commands stay lightweight.
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk, Gdk
        import cairo  # noqa: F401
    except Exception as exc:
        print(f"GTK4/PyGObject failed to load: {exc}", file=sys.stderr)
        log(f"GTK4/PyGObject failed to load: {exc!r}")
        return 1

    # Optional layer-shell. This is what keeps focus in the text field on COSMIC/Wayland.
    LayerShell = None
    try:
        from ctypes import CDLL
        with contextlib.suppress(Exception):
            CDLL("libgtk4-layer-shell.so")
        import gi as _gi
        _gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as _LayerShell
        LayerShell = _LayerShell
        log("Gtk4LayerShell available")
    except Exception as exc:
        log(f"Gtk4LayerShell unavailable; falling back to normal GTK window: {exc!r}")

    injector = InputInjector()
    injector.ensure()
    start_background_vr_audio_monitor()
    
    app = Gtk.Application(application_id=APP_ID)
    
    # This is a background daemon. Without hold(), Gtk.Application may exit cleanly
    # when no visible window is active, which makes the systemd service appear
    # "successful" but dead.
    app.hold()

    class Overlay:
        def __init__(self, gtk_app) -> None:
            self.gtk_app = gtk_app
            self.glass = GlassBackdrop()
            self.window = Gtk.ApplicationWindow(application=gtk_app, title=APP_NAME)
            self.window.set_default_size(WINDOW_W, WINDOW_H)
            self.window.set_decorated(False)
            self.window.set_resizable(False)
            with contextlib.suppress(Exception):
                self.window.set_focusable(False)
                
            # Make the actual GTK window background fully transparent so only the
            # custom drawn popup card is visible.
            self.window.add_css_class("kdictate-window")

            provider = Gtk.CssProvider()
            provider.load_from_data(b"""
            .kdictate-window,
            .kdictate-window:backdrop,
            window.kdictate-window,
            window.kdictate-window:backdrop,
            drawingarea {
                background-color: transparent;
                background-image: none;
                box-shadow: none;
            }
            """)

            display = Gdk.Display.get_default()
            if display is not None:
                Gtk.StyleContext.add_provider_for_display(
                    display,
                    provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                )
                
            self.level = 0.0
            self.level_smooth = 0.0
            self.mode = "idle"
            self.title = "Ready"
            self.subtitle = "Press Super+V and speak."
            self.visible = False
            self.fade_alpha = 0.0
            self.fade_target = 0.0
            self.open_anim = 0.0
            self.open_target = 0.0
            self.last_frame = time.time()
            self.engine = DictationEngine(self)
            self.monitor = KeyboardMonitor(lambda: None if self.glass.permission_pending else GLib.idle_add(self.invoke_cancel, "manual typing"))
            self.monitor.start()
            self.paster = ClipboardPaster(injector, self.monitor.pause_for)
            self.layer_enabled = False

            self.settings_open = False
            self.settings_anim = 0.0
            self.settings_extra = float(SETTINGS_EXTRA_H)
            self.settings_extra_target = float(SETTINGS_EXTRA_H)
            self.open_dropdown: str | None = None
            self.dropdown_anim = {"mic": 0.0, "quality": 0.0, "realtime": 0.0, "theme": 0.0}
            self.preview_anim = 0.0
            self.preview_draw_chars = 0.0
            self.preview_scroll = 0.0
            self.preview_scroll_target = 0.0
            self.preview_user_scroll_lines = 0
            self.keep_preview_during_finish = False
            self.current_window_h = WINDOW_H
            self.window_x = 0
            self.window_y = 0
            self.dragging_window = False
            self.drag_origin_x = 0
            self.drag_origin_y = 0
            self.microphones = list_input_microphones()
            self.settings_options_cache: dict[str, list[tuple[str, str]]] = {}
            self.audio_poll_busy = False
            self.rebuild_settings_options_cache()
            self.realtime_preview = ""

            self.area = Gtk.DrawingArea()
            self.area.set_content_width(WINDOW_W)
            self.area.set_content_height(WINDOW_H)
            self.area.set_draw_func(self.draw)
            self.window.set_child(self.area)

            click = Gtk.GestureClick.new()
            click.connect("pressed", self.on_click)
            self.area.add_controller(click)

            drag = Gtk.GestureDrag.new()
            drag.connect("drag-begin", self.on_drag_begin)
            drag.connect("drag-update", self.on_drag_update)
            drag.connect("drag-end", self.on_drag_end)
            self.area.add_controller(drag)

            scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            scroll.connect("scroll", self.on_scroll)
            self.area.add_controller(scroll)

            self.window.connect("close-request", self.on_close_request)

            self.window.connect("realize", configure_native_overlay)
            if LayerShell is not None and LayerShell.is_supported():
                try:
                    LayerShell.init_for_window(self.window)
                    LayerShell.set_namespace(self.window, "kdictate")
                    LayerShell.set_layer(self.window, LayerShell.Layer.TOP)
                    LayerShell.set_keyboard_mode(self.window, LayerShell.KeyboardMode.NONE)
                    LayerShell.set_anchor(self.window, LayerShell.Edge.TOP, True)
                    LayerShell.set_anchor(self.window, LayerShell.Edge.LEFT, True)
                    self.layer_enabled = True
                except Exception as exc:
                    self.layer_enabled = False
                    log(f"Layer shell init failed: {exc!r}")

            GLib.timeout_add(16, self.animate)
            GLib.timeout_add(55, self.tick)
            GLib.timeout_add_seconds(3, self.poll_vr_audio)

        def on_close_request(self, *args):
            self.invoke_cancel("closed")
            return True

        def _hit_circle(self, x: float, y: float, cx: float, cy: float, radius: float = 17.0) -> bool:
            return ((x - cx) * (x - cx) + (y - cy) * (y - cy)) <= radius * radius

        def on_click(self, gesture, n_press, x, y):
            # Settings/back control is on the left of the close button.
            if self._hit_circle(x, y, WINDOW_W - 58, 27):
                if self.settings_open:
                    self.show_and_record(preserve_position=True)
                else:
                    self.open_settings()
                return

            if self._hit_circle(x, y, WINDOW_W - 27, 27):
                self.invoke_cancel("clicked close")
                return

            if self.settings_open:
                self.on_settings_click(x, y)

        def on_drag_begin(self, gesture, start_x, start_y):
            top_band = max(30.0, self.current_window_h * 0.25)
            on_button = self._hit_circle(start_x, start_y, WINDOW_W - 58, 27) or self._hit_circle(start_x, start_y, WINDOW_W - 27, 27)
            self.dragging_window = bool(start_y <= top_band and not on_button)
            self.drag_origin_x = self.window_x
            self.drag_origin_y = self.window_y

        def on_drag_update(self, gesture, offset_x, offset_y):
            if not self.dragging_window:
                return

            self.move_overlay(self.drag_origin_x + int(offset_x), self.drag_origin_y + int(offset_y))

        def on_drag_end(self, gesture, offset_x, offset_y):
            if self.dragging_window:
                self.move_overlay(self.drag_origin_x + int(offset_x), self.drag_origin_y + int(offset_y))
            self.dragging_window = False
            if active_theme().startswith("glass-") and not self.glass.native:
                self.present_with_glass()

        def on_scroll(self, controller, dx, dy):
            if not (self.engine.recording and realtime_transcription_enabled() and self.preview_anim > 0.05):
                return False

            if dy < 0:
                self.preview_user_scroll_lines = min(40, self.preview_user_scroll_lines + 1)
            elif dy > 0:
                self.preview_user_scroll_lines = max(0, self.preview_user_scroll_lines - 1)

            self.area.queue_draw()
            return True

        def move_overlay(self, x: int, y: int) -> None:
            self.window_x = int(x)
            self.window_y = int(y)

            if self.layer_enabled and LayerShell is not None:
                with contextlib.suppress(Exception):
                    LayerShell.set_margin(self.window, LayerShell.Edge.LEFT, self.window_x)
                    LayerShell.set_margin(self.window, LayerShell.Edge.TOP, self.window_y)
            else:
                with contextlib.suppress(Exception):
                    position_native_overlay(self.window, self.window_x, self.window_y)

        def poll_vr_audio(self):
            if not self.visible:
                return True
            if self.audio_poll_busy:
                return True

            self.audio_poll_busy = True

            def worker() -> None:
                try:
                    apply_wivrn_audio_if_available()
                    devices = list_input_microphones()
                    GLib.idle_add(self._on_audio_devices_changed, devices)
                finally:
                    self.audio_poll_busy = False

            threading.Thread(target=worker, daemon=True).start()
            return True

        def _on_audio_devices_changed(self, devices):
            self.microphones = devices
            self.rebuild_settings_options_cache()

            if self.settings_open:
                self.area.queue_draw()

            return False

        def rebuild_settings_options_cache(self) -> None:
            self.settings_options_cache["theme"] = list(THEME_LABELS.items())
            self.settings_options_cache["mic"] = [(dev["id"], dev["label"]) for dev in self.microphones]
            self.settings_options_cache["quality"] = [
                ("speed", QUALITY_LABELS["speed"]),
                ("balanced", QUALITY_LABELS["balanced"]),
                ("quality", QUALITY_LABELS["quality"]),
            ]
            self.settings_options_cache["realtime"] = [
                ("1", "On - live transcript"),
                ("0", "Off - final paste only"),
            ]

        def refresh_microphones(self) -> None:
            self.microphones = list_input_microphones()
            self.rebuild_settings_options_cache()

        def selected_mic_label(self) -> str:
            selected = os.environ.get("KDICTATE_MIC_DEVICE", "").strip()

            if selected in {"", WIVRN_AUTO_DEVICE_ID} and VR_AUDIO_STATE.get("active"):
                return "WiVRn microphone (auto)"

            for dev in self.microphones:
                if dev["id"] == selected:
                    return dev["label"]

            return "System default"

        def settings_options(self, key: str) -> list[tuple[str, str]]:
            options = list(self.settings_options_cache.get(key, []))

            if key == "mic":
                selected = os.environ.get("KDICTATE_MIC_DEVICE", "").strip()

                # Keep the dropdown elegant even on systems with many input devices,
                # while ensuring WiVRn and the currently selected mic stay visible.
                visible = options[:7]

                for required in [WIVRN_AUTO_DEVICE_ID, selected]:
                    if required and all(value != required for value, _label in visible):
                        for value, label in options:
                            if value == required:
                                if len(visible) >= 7:
                                    visible[-1] = (value, label)
                                else:
                                    visible.append((value, label))
                                break

                return visible

            return options

        def selected_setting_value(self, key: str) -> str:
            if key == "theme":
                return active_theme()
            if key == "mic":
                return os.environ.get("KDICTATE_MIC_DEVICE", "").strip()
            if key == "quality":
                return _normalize_profile(os.environ.get("KDICTATE_PROFILE"), os.environ.get("KDICTATE_MODEL"))
            if key == "realtime":
                return "1" if realtime_transcription_enabled() else "0"
            return ""

        def setting_row_value(self, key: str) -> str:
            if key == "theme":
                return THEME_LABELS[active_theme()]
            if key == "mic":
                return self.selected_mic_label()
            if key == "quality":
                quality = _normalize_profile(os.environ.get("KDICTATE_PROFILE"), os.environ.get("KDICTATE_MODEL"))
                return QUALITY_LABELS.get(quality, QUALITY_LABELS["balanced"])
            if key == "realtime":
                return "On - live transcript" if realtime_transcription_enabled() else "Off - final paste only"
            return ""

        def settings_layout(self) -> tuple[list[dict], float]:
            items: list[dict] = []
            y = 116.0

            for key, label in [
                ("mic", "Microphone"),
                ("quality", "Quality"),
                ("realtime", "Realtime transcription"),
                ("theme", "Appearance"),
            ]:
                items.append({"kind": "row", "key": key, "label": label, "y": y, "h": 38.0})
                y += 44.0

                anim_amount = max(0.0, min(1.0, self.dropdown_anim.get(key, 0.0)))

                if self.open_dropdown == key or anim_amount > 0.015:
                    reveal = self.ease_out_cubic(anim_amount)
                    options = self.settings_options(key)
                    option_base_y = y

                    for idx, (value, option_label) in enumerate(options):
                        items.append({
                            "kind": "option",
                            "key": key,
                            "value": value,
                            "label": option_label,
                            "y": option_base_y + idx * 30.0 * reveal,
                            "h": 30.0,
                            "reveal": reveal,
                        })

                    y += (len(options) * 30.0 + 6.0) * reveal

            return items, y + 12.0

        def update_settings_height(self) -> None:
            _items, bottom = self.settings_layout()
            wanted_extra = max(SETTINGS_EXTRA_H, min(SETTINGS_MAX_EXTRA_H, bottom - WINDOW_H + 18.0))
            self.settings_extra_target = wanted_extra
            self.set_window_height(WINDOW_H + int(math.ceil(wanted_extra)))

        def toggle_dropdown(self, key: str) -> None:
            self.open_dropdown = None if self.open_dropdown == key else key

            self.update_settings_height()
            self.area.queue_draw()

        def apply_setting_choice(self, key: str, value: str) -> None:
            if key == "theme" and value in THEME_LABELS:
                save_runtime_config({"KDICTATE_THEME": value})
                self.update_glass()
            if key == "mic":
                set_microphone_device(value)
            elif key == "quality":
                profile, _model = apply_quality_profile(value)
                if BACKEND != "whisper.cpp":
                    ModelManager.warm_async()
            elif key == "realtime":
                set_realtime_transcription(value == "1")

            self.open_dropdown = None
            self.update_settings_height()
            self.set_status("settings", "Settings", "")
            self.area.queue_draw()

        def on_settings_click(self, x, y) -> None:
            for item in self.settings_layout()[0]:
                if item["y"] <= y <= item["y"] + item["h"]:
                    if item["kind"] == "row":
                        self.toggle_dropdown(item["key"])
                    elif item["kind"] == "option" and self.open_dropdown == item["key"]:
                        self.apply_setting_choice(item["key"], item["value"])
                    return

        def open_settings(self):
            self.engine.cancel("settings opened")
            self.monitor.disarm()
            self.settings_open = True
            self.open_dropdown = None
            self.realtime_preview = ""
            self.preview_draw_chars = 0.0
            self.settings_extra_target = float(SETTINGS_EXTRA_H)
            self.set_window_height(SETTINGS_WINDOW_H)
            if self.visible:
                self.move_overlay(self.window_x, self.window_y)
            else:
                self.position()
            self.set_status("settings", "Settings", "")
            self.fade_target = 1.0
            self.open_target = 1.0
            self.visible = True
            self.present_with_glass()
            self.area.queue_draw()

        def update_glass(self) -> None:
            self.glass.portal_attempted = False
            if self.window.get_realized():
                self.glass.configure(self.window, WINDOW_W, self.current_window_h)
            if self.visible:
                self.present_with_glass()

        def present_with_glass(self) -> None:
            self.window.realize()
            configure_native_overlay(self.window)
            position_native_overlay(self.window, self.window_x, self.window_y)
            enabled = active_theme().startswith("glass-")
            native = self.glass.configure(self.window, WINDOW_W, self.current_window_h)
            if not enabled or native:
                self.window.set_visible(True)
                return
            if self.glass.busy:
                return
            self.glass.busy = True
            generation = getattr(self, "glass_generation", 0) + 1
            self.glass_generation = generation
            x, y = self.window_x, self.window_y
            scale = float(self.window.get_scale_factor())
            # Authorization never holds up listening or leaves the card invisible.
            needs_portal = desktop_session_type() == "wayland" and self.glass.pipeline is None
            if needs_portal:
                self.window.set_visible(True)
            else:
                self.window.hide()
            def capture():
                ok = False
                try:
                    if needs_portal:
                        self.glass._ensure_portal()
                        hidden = threading.Event()
                        def hide_ready():
                            if generation == self.glass_generation and self.visible:
                                self.window.hide()
                            hidden.set()
                            return False
                        GLib.idle_add(hide_ready)
                        if not hidden.wait(1.0):
                            raise RuntimeError("Backdrop capture deferred while the desktop is busy")
                    time.sleep(0.08)
                    ok = self.glass.capture(x, y, WINDOW_W, WINDOW_H + SETTINGS_MAX_EXTRA_H, scale)
                except Exception as exc:
                    self.glass.status = str(exc)
                    log(f"Glass authorization: {exc}")
                def present():
                    self.glass.busy = False
                    if not self.visible or generation != self.glass_generation:
                        return False
                    if not ok and self.mode == "settings":
                        self.subtitle = "Glass: screen access needed. Reselect Appearance to retry."
                    self.window.set_visible(True)
                    position_native_overlay(self.window, self.window_x, self.window_y)
                    self.area.queue_draw()
                    return False
                GLib.idle_add(present)
            threading.Thread(target=capture, name="VerbatimGlassBackdrop", daemon=True).start()

        def set_window_height(self, height: int) -> None:
            if self.current_window_h == height:
                return

            self.current_window_h = height
            self.area.set_content_height(height)
            self.window.set_default_size(WINDOW_W, height)
            if self.glass.native:
                self.glass.configure(self.window, WINDOW_W, height)

            with contextlib.suppress(Exception):
                self.window.set_size_request(WINDOW_W, height)

        def tick(self):
            self.engine.tick()
            return True

        def animate(self):
            now = time.time()
            dt = max(0.001, min(0.033, now - self.last_frame))
            self.last_frame = now

            self.level_smooth += (self.level - self.level_smooth) * min(1.0, dt * 12.0)

            fade_rate = 6.6 if self.fade_target > self.fade_alpha else 5.4
            open_rate = 7.0 if self.open_target > self.open_anim else 5.8
            self.fade_alpha += (self.fade_target - self.fade_alpha) * min(1.0, dt * fade_rate)
            self.open_anim += (self.open_target - self.open_anim) * min(1.0, dt * open_rate)

            target_settings = 1.0 if self.settings_open else 0.0
            self.settings_anim += (target_settings - self.settings_anim) * min(1.0, dt * 13.0)
            self.settings_extra += (self.settings_extra_target - self.settings_extra) * min(1.0, dt * 12.0)

            for key in self.dropdown_anim:
                target = 1.0 if self.open_dropdown == key else 0.0
                rate = 12.0 if target > self.dropdown_anim[key] else 16.0
                self.dropdown_anim[key] += (target - self.dropdown_anim[key]) * min(1.0, dt * rate)

            if self.settings_open or any(value > 0.02 for value in self.dropdown_anim.values()):
                self.update_settings_height()

            target_preview = 1.0 if (
                (self.engine.recording or self.keep_preview_during_finish)
                and realtime_transcription_enabled()
                and not self.settings_open
            ) else 0.0
            self.preview_anim += (target_preview - self.preview_anim) * min(1.0, dt * 13.0)

            if self.realtime_preview:
                target_chars = float(len(self.realtime_preview))
                if self.preview_draw_chars < target_chars:
                    # Keep the existing typing feel, but make long updates feel fluid
                    # by revealing faster as the transcript grows.
                    speed = 58.0 + min(42.0, target_chars * 0.06)
                    self.preview_draw_chars = min(target_chars, self.preview_draw_chars + max(1.0, dt * speed))
                else:
                    self.preview_draw_chars = target_chars
            else:
                self.preview_draw_chars = 0.0

            if not self.realtime_preview:
                self.preview_scroll_target = 0.0

            self.preview_scroll += (self.preview_scroll_target - self.preview_scroll) * min(1.0, dt * 10.0)

            if self.fade_target == 0.0 and self.fade_alpha < 0.025 and self.open_anim < 0.04 and self.visible:
                self.window.hide()
                self.visible = False
                self.keep_preview_during_finish = False
                self.realtime_preview = ""
                self.preview_anim = 0.0
                self.preview_draw_chars = 0.0
                self.preview_scroll = 0.0
                self.preview_scroll_target = 0.0
                self.preview_user_scroll_lines = 0
                self.set_window_height(WINDOW_H)

            if (
                not self.settings_open
                and not self.keep_preview_during_finish
                and self.settings_anim < 0.03
                and self.preview_anim < 0.015
            ):
                self.set_window_height(WINDOW_H)

            with contextlib.suppress(Exception):
                self.window.set_opacity(max(0.0, min(1.0, self.fade_alpha)))

            if self.visible:
                self.area.queue_draw()
            return True

        def position(self):
            # Prefer caret-relative placement if AT-SPI exposes it. Otherwise use a calm
            # top-center placement that does not cover the common typing line.
            x, y = 0, 0
            caret = atspi_caret_position()
            display = Gdk.Display.get_default()
            geom = None
            try:
                monitors = display.get_monitors()
                monitor = monitors.get_item(0) if monitors.get_n_items() else None
                geom = monitor.get_geometry() if monitor else None
            except Exception:
                geom = None

            if geom is not None:
                sw, sh = int(geom.width), int(geom.height)
                ox, oy = int(geom.x), int(geom.y)
            else:
                sw, sh, ox, oy = 1920, 1080, 0, 0

            window_h = self.current_window_h

            if caret:
                cx, cy = caret
                x = max(16, min(cx, sw - WINDOW_W - 16))
                y = max(16, min(cy, sh - window_h - 16))
            else:
                x = ox + max(16, int((sw - WINDOW_W) / 2))
                y = oy + max(16, int(sh * 0.18))

            self.move_overlay(int(x), int(y))

        def show_and_record(self, *, preserve_position: bool = False):
            if self.engine.recording:
                self.invoke_cancel("toggle")
                return

            DESKTOP_TARGET.capture()
            self.settings_open = False
            self.open_dropdown = None
            self.realtime_preview = ""
            self.preview_draw_chars = 0.0
            self.preview_scroll = 0.0
            self.preview_scroll_target = 0.0
            self.preview_user_scroll_lines = 0
            self.keep_preview_during_finish = False

            if realtime_transcription_enabled():
                self.set_window_height(LISTENING_PREVIEW_WINDOW_H)
                subtitle = "Speak now. Live transcript is on."
            else:
                self.set_window_height(WINDOW_H)
                subtitle = "Speak now. Pause to finish."

            was_visible = self.visible

            if preserve_position and was_visible:
                self.move_overlay(self.window_x, self.window_y)
            elif FAST_KEYBIND_START and not CARET_POSITION_ON_KEYBIND:
                display = Gdk.Display.get_default()
                geom = None
                try:
                    monitors = display.get_monitors()
                    monitor = monitors.get_item(0) if monitors.get_n_items() else None
                    geom = monitor.get_geometry() if monitor else None
                except Exception:
                    geom = None
            
                if geom is not None:
                    sw, sh, ox, oy = int(geom.width), int(geom.height), int(geom.x), int(geom.y)
                else:
                    sw, sh, ox, oy = 1920, 1080, 0, 0
            
                self.move_overlay(
                    ox + max(16, int((sw - WINDOW_W) / 2)),
                    oy + max(16, int(sh * 0.18)),
                )
            else:
                self.position()
            self.set_status("listening", "Listening", subtitle)
            self.level = 0.0

            if preserve_position and was_visible:
                self.fade_alpha = max(self.fade_alpha, 0.92)
                self.open_anim = max(self.open_anim, 0.92)
            else:
                self.fade_alpha = 0.0
                self.open_anim = 0.0
                with contextlib.suppress(Exception):
                    self.window.set_opacity(0.0)

            self.fade_target = 1.0
            self.open_target = 1.0
            self.last_frame = time.time()
            self.visible = True
            self.monitor.arm(ignore_for=0.8)
            self.present_with_glass()
            self.area.queue_draw()

            # Fallback only. Normal startup should already have the model hot.
            if KEEP_MODEL_WARM and not ModelManager.is_loaded():
                ModelManager.warm_async(reason="keybind-fallback")

            if FAST_KEYBIND_START:
                self.engine.start_async()
            else:
                self.engine.start()

        def close_smoothly(self):
            self.monitor.disarm()
            self.glass_generation = getattr(self, "glass_generation", 0) + 1
            self.settings_open = False
            self.open_dropdown = None
            self.fade_target = 0.0
            self.open_target = 0.0

        def set_level(self, rms: float) -> None:
            # Map microphone RMS into a stable visual 0..1 range.
            self.level = max(0.0, min(1.0, math.log10(1.0 + rms * 85.0)))

        def set_status(self, mode: str, title: str, subtitle: str) -> None:
            self.mode = mode
            self.title = title
            self.subtitle = subtitle
            GLib.idle_add(self.area.queue_draw)

        def invoke_for_session(self, session: int, kind: str, *args):
            def apply():
                if session != self.engine.session_id or self.engine.cancelled:
                    return False
                callbacks = {"transcribed": self._on_transcribed,
                    "realtime": self._on_realtime_text, "error": self.show_error,
                    "cancel": self._cancel_on_ui}
                callback = callbacks.get(kind)
                if callback:
                    callback(*args)
                return False
            GLib.idle_add(apply)

        def invoke_transcribed(self, text: str):
            GLib.idle_add(self._on_transcribed, text)

        def invoke_realtime_text(self, text: str, audio_seconds: float = 0.0):
            GLib.idle_add(self._on_realtime_text, text, audio_seconds)

        def invoke_error(self, msg: str):
            GLib.idle_add(self.show_error, msg)

        def invoke_cancel(self, reason: str):
            GLib.idle_add(self._cancel_on_ui, reason)

        def _cancel_on_ui(self, reason: str):
            self.engine.cancel(reason)
            self.close_smoothly()
            return False

        def _on_realtime_text(self, text: str, audio_seconds: float = 0.0):
            if self.engine.recording and realtime_transcription_enabled():
                text = normalize_transcript_text(text, final=False)

                if not text or looks_like_realtime_loop(text):
                    log(f"Ignored unsafe realtime preview text={text[:180]!r}")
                    return False

                previous_text = self.realtime_preview
                previous_len = len(previous_text)
                new_len = len(text)

                if previous_len > 80 and new_len < previous_len * 0.72:
                    log(
                        "Ignored short realtime preview regression "
                        f"old_len={previous_len} new_len={new_len}"
                    )
                    return False

                if previous_len > 45 and new_len > previous_len * _REALTIME_MAX_TEXT_GROWTH_RATIO:
                    log(
                        "Ignored oversized realtime preview growth "
                        f"old_len={previous_len} new_len={new_len}"
                    )
                    return False

                self.realtime_preview = text
                self.engine.realtime_preview_audio_seconds = max(
                    self.engine.realtime_preview_audio_seconds,
                    float(audio_seconds or 0.0),
                )

                if new_len < previous_len:
                    self.preview_draw_chars = min(self.preview_draw_chars, float(new_len))

                if previous_text and not text.startswith(previous_text[: min(32, len(previous_text))]):
                    self.preview_draw_chars = min(self.preview_draw_chars, float(new_len))

                if self.preview_user_scroll_lines == 0:
                    self.preview_scroll_target = max(0.0, self.preview_scroll_target)

                self.set_status("listening", "Listening live", "Transcribing as you speak.")
            return False

        def _on_transcribed(self, text: str):
            if realtime_transcription_enabled() and self.realtime_preview:
                self.keep_preview_during_finish = True
                self.set_window_height(LISTENING_PREVIEW_WINDOW_H)
                self.preview_anim = max(self.preview_anim, 1.0)
                self.preview_draw_chars = max(
                    self.preview_draw_chars,
                    float(len(self.realtime_preview)),
                )

            self.set_status("typing", "Typing", text[:68] + ("..." if len(text) > 68 else ""))

            session = self.engine.session_id
            self.glass_generation = getattr(self, "glass_generation", 0) + 1
            # A normal GTK fallback window must be out of the way before injection.
            if not self.layer_enabled:
                self.window.hide()
            def paste_worker() -> None:
                if session != self.engine.session_id or self.engine.cancelled:
                    return
                if self.glass.permission_pending:
                    # The first non-native Wayland glass authorization may take
                    # keyboard focus. Keep captured speech, wait for the dialog,
                    # and never paste the transcript into the permission prompt.
                    if not self.glass.permission_finished.wait(90.0):
                        self.invoke_for_session(session, "error", "Desktop permission is still open. Your text is saved in kdictate last-transcript.")
                        return
                    if session != self.engine.session_id or self.engine.cancelled:
                        return
                    time.sleep(0.18)
                ok, msg = self.paster.paste_text(text)

                if session != self.engine.session_id or self.engine.cancelled:
                    return
                if not ok:
                    self.invoke_for_session(session, "error", msg)
                else:
                    def close_current():
                        if session == self.engine.session_id and not self.engine.cancelled:
                            self.close_smoothly()
                        return False
                    GLib.idle_add(close_current)

            threading.Thread(target=paste_worker, daemon=True).start()
            return False

        def show_error(self, msg: str):
            self.monitor.disarm()
            self.settings_open = False
            self.set_status("error", "Needs attention", msg[:96])
            self.fade_target = 1.0
            self.visible = True
            self.window.set_visible(True)
            session = self.engine.session_id
            def later():
                if session == self.engine.session_id and self.mode == "error":
                    self.close_smoothly()
                return False
            GLib.timeout_add(7000, later)
            return False

        def draw_round_rect(self, cr, x, y, w, h, r):
            cr.new_sub_path()
            cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()

        def ease_out_cubic(self, t: float) -> float:
            t = max(0.0, min(1.0, t))
            return 1.0 - pow(1.0 - t, 3)

        def _text_width(self, cr, text: str) -> float:
            extents = cr.text_extents(text)
            return float(extents.width if hasattr(extents, "width") else extents[2])

        def ellipsize_text(self, cr, text: str, max_width: float) -> str:
            if self._text_width(cr, text) <= max_width:
                return text

            suffix = "..."
            available = max(1.0, max_width - self._text_width(cr, suffix))
            output = ""

            for ch in text:
                candidate = output + ch
                if self._text_width(cr, candidate) > available:
                    break
                output = candidate

            return output.rstrip() + suffix

        def wrap_text_lines(self, cr, text: str, max_width: float) -> list[str]:
            words = text.split()
            if not words:
                return []

            lines: list[str] = []
            current = ""

            for word in words:
                candidate = word if not current else f"{current} {word}"
                if self._text_width(cr, candidate) <= max_width:
                    current = candidate
                    continue

                if current:
                    lines.append(current)
                    current = word
                else:
                    # Extremely long single tokens still need to move instead of
                    # blowing through the panel edge.
                    chunk = ""
                    for ch in word:
                        candidate_chunk = chunk + ch
                        if self._text_width(cr, candidate_chunk) > max_width and chunk:
                            lines.append(chunk)
                            chunk = ch
                        else:
                            chunk = candidate_chunk
                    current = chunk

            if current:
                lines.append(current)

            return lines

        def draw_chevron(self, cr, cx: float, cy: float, angle: float, a: float) -> None:
            # Starts as a right-facing disclosure arrow and rotates down as the
            # dropdown expands.
            pts = [(-4.0, -5.0), (3.0, 0.0), (-4.0, 5.0)]
            cos_a = math.cos(angle)
            sin_a = math.sin(angle)

            cr.set_source_rgba(*theme_palette()["text"], 0.46 * a)
            cr.set_line_width(1.7)

            for idx, (px, py) in enumerate(pts):
                rx = cx + px * cos_a - py * sin_a
                ry = cy + px * sin_a + py * cos_a
                if idx == 0:
                    cr.move_to(rx, ry)
                else:
                    cr.line_to(rx, ry)

            cr.stroke()

        def draw_settings_row(self, cr, y: float, label: str, value: str, key: str, a: float) -> None:
            open_amount = max(0.0, min(1.0, self.dropdown_anim.get(key, 0.0)))

            self.draw_round_rect(cr, 24, y, WINDOW_W - 48, 38, 13)
            cr.set_source_rgba(*theme_palette()["text"], (0.070 + 0.030 * open_amount) * a)
            cr.fill()

            if open_amount > 0.02:
                cr.set_source_rgba(*theme_palette()["accent"], 0.065 * open_amount * a)
                self.draw_round_rect(cr, 24, y, WINDOW_W - 48, 38, 13)
                cr.fill()

            cr.set_source_rgba(*theme_palette()["text"], 0.92 * a)
            cr.set_font_size(12.4)
            cr.move_to(39, y + 16)
            cr.show_text(label)

            cr.set_source_rgba(*theme_palette()["text"], (0.70 if theme_palette()["light"] else 0.58) * a)
            cr.set_font_size(11.0)
            shown = self.ellipsize_text(cr, value, WINDOW_W - 104)
            cr.move_to(39, y + 31)
            cr.show_text(shown)

            self.draw_chevron(cr, WINDOW_W - 42, y + 19, open_amount * (math.pi / 2.0), a)

        def draw_settings_option(self, cr, y: float, label: str, selected: bool, a: float) -> None:
            self.draw_round_rect(cr, 34, y, WINDOW_W - 68, 26, 10)
            cr.set_source_rgba(*theme_palette()["text"], (0.060 if not selected else 0.105) * a)
            cr.fill()

            if selected:
                cr.set_source_rgba(*theme_palette()["accent"], 0.20 * a)
                cr.arc(47, y + 13, 3.2, 0, 2 * math.pi)
                cr.fill()

            cr.set_source_rgba(*theme_palette()["text"], (0.62 if not selected else 0.91) * a)
            cr.set_font_size(10.8)
            cr.move_to(58, y + 17)
            cr.show_text(self.ellipsize_text(cr, label, WINDOW_W - 108))

        def draw_live_preview(self, cr, width: int, a: float, base: tuple[float, float, float]) -> None:
            panel_y = 112
            panel_h = LISTENING_PREVIEW_EXTRA_H - 12
            viewport_x = 32
            viewport_y = panel_y + 31
            viewport_w = width - 64
            viewport_h = panel_h - 47
            line_h = 22.0

            cr.save()
            cr.rectangle(14, 102, width - 28, panel_h + 18)
            cr.clip()

            cr.set_source_rgba(*theme_palette()["text"], 0.066 * a)
            self.draw_round_rect(cr, 20, panel_y + 18, width - 40, panel_h - 20, 18)
            cr.fill()

            cr.set_source_rgba(*theme_palette()["text"], 0.118 * a)
            self.draw_round_rect(cr, 20.5, panel_y + 18.5, width - 41, panel_h - 21, 18)
            cr.set_line_width(1)
            cr.stroke()

            cr.set_font_size(15.0)
            visible_chars = max(0, min(len(self.realtime_preview), int(self.preview_draw_chars)))
            text = self.realtime_preview[:visible_chars].strip()

            if not text:
                lines = ["Listening..."]
            else:
                lines = self.wrap_text_lines(cr, text, viewport_w)

            total_h = max(viewport_h, len(lines) * line_h)
            max_scroll = max(0.0, total_h - viewport_h)
            visible_line_count = max(1, int(viewport_h // line_h))
            max_user_lines = max(0, len(lines) - visible_line_count)
            self.preview_user_scroll_lines = min(self.preview_user_scroll_lines, max_user_lines)

            if self.preview_user_scroll_lines > 0:
                self.preview_scroll_target = max(0.0, max_scroll - self.preview_user_scroll_lines * line_h)
            else:
                self.preview_scroll_target = max_scroll

            cr.save()
            cr.rectangle(viewport_x, viewport_y - 2, viewport_w, viewport_h + 5)
            cr.clip()

            for idx, line in enumerate(lines):
                yy = viewport_y + idx * line_h - self.preview_scroll

                if yy < viewport_y - line_h or yy > viewport_y + viewport_h + line_h:
                    continue

                top_fade = max(0.35, min(1.0, (yy - viewport_y + 18.0) / 24.0))
                bottom_fade = max(0.35, min(1.0, (viewport_y + viewport_h - yy + 4.0) / 26.0))
                newest = 1.0 if idx >= len(lines) - 1 else 0.76
                line_alpha = min(top_fade, bottom_fade) * newest * a

                cr.set_source_rgba(*theme_palette()["text"], line_alpha)
                cr.move_to(viewport_x, yy + 15)
                cr.show_text(line)

            if realtime_transcription_enabled() and self.engine.recording:
                caret_on = math.sin(time.time() * 7.5) > -0.15
                if caret_on and lines:
                    last_line = lines[-1]
                    last_line_y = viewport_y + (len(lines) - 1) * line_h - self.preview_scroll
                    if viewport_y - line_h < last_line_y < viewport_y + viewport_h + line_h:
                        caret_x = viewport_x + min(viewport_w - 6, self._text_width(cr, last_line) + 4)
                        cr.set_source_rgba(*theme_palette()["text"], 0.82 * a)
                        cr.set_line_width(2)
                        cr.move_to(caret_x, last_line_y + 1)
                        cr.line_to(caret_x, last_line_y + 18)
                        cr.stroke()

            cr.restore()
            cr.restore()

        def draw(self, area, cr, width, height):
            # Outside the card stays transparent; the card itself is a clean solid material.
            a = max(0.0, min(1.0, self.fade_alpha))
            settings_alpha = a * max(0.0, min(1.0, self.settings_anim))
            preview_alpha = a * max(0.0, min(1.0, self.preview_anim))
            settings_extra = self.settings_extra * max(0.0, min(1.0, self.settings_anim))
            preview_extra = LISTENING_PREVIEW_EXTRA_H * max(0.0, min(1.0, self.preview_anim))
            panel_extra = max(settings_extra, preview_extra)
            effective_h = WINDOW_H + panel_extra

            presence = self.ease_out_cubic(self.open_anim)
            scale = 0.965 + 0.035 * presence
            drift = (1.0 - presence) * (14.0 if self.open_target > 0.0 else -10.0)

            cr.save()
            cr.set_operator(cairo.OPERATOR_OVER)  # normal alpha compositing; do not punch transparent holes
            cr.translate(width / 2.0, 7 + effective_h / 2.0 + drift)
            cr.scale(scale, scale)
            cr.translate(-width / 2.0, -(7 + effective_h / 2.0))

            # Soft shadow only outside the card.
            cr.set_source_rgba(0, 0, 0, 0.28 * a)
            self.draw_round_rect(cr, 10, 14, width - 20, effective_h - 20, 24)
            cr.fill()

            palette = theme_palette()
            glass = palette["glass"]
            self.draw_round_rect(cr, 8, 7, width - 16, effective_h - 17, 24)
            cr.save()
            cr.clip()
            has_backdrop = glass and self.glass.surface is not None and not self.glass.native
            if has_backdrop:
                cr.set_source_surface(self.glass.surface, 0, 0)
                cr.paint_with_alpha(a)
            # Native blur uses live translucency; fallback contains actually blurred
            # background pixels, frozen for the short life of this card.
            tint_alpha = 0.78 if glass and (self.glass.native or has_backdrop) else 0.995
            cr.set_source_rgba(*palette["surface"], tint_alpha * a)
            cr.paint()
            if glass:
                sheen = cairo.LinearGradient(8, 7, width, effective_h)
                sheen.add_color_stop_rgba(0, 1, 1, 1, (0.24 if palette["light"] else 0.11) * a)
                sheen.add_color_stop_rgba(0.48, 1, 1, 1, 0.018 * a)
                sheen.add_color_stop_rgba(1, *palette["accent"], 0.065 * a)
                cr.set_source(sheen)
                cr.paint()
            cr.restore()

            # Subtle border/highlight.
            cr.set_source_rgba(*theme_palette()["text"], 0.105 * a)
            self.draw_round_rect(cr, 8.5, 7.5, width - 17, effective_h - 18, 24)
            cr.set_line_width(1)
            cr.stroke()

            # Settings gear normally; back arrow while already inside settings.
            control_x = width - 58
            cr.set_source_rgba(*theme_palette()["text"], 0.105 * a)
            cr.arc(control_x, 27, 13, 0, 2 * math.pi)
            cr.fill()

            cr.set_source_rgba(*theme_palette()["text"], 0.76 * a)
            cr.set_line_width(1.8)

            if self.settings_open:
                cr.set_line_width(1.9)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)

                cr.move_to(control_x + 8.0, 27.0)
                cr.line_to(control_x - 5.2, 27.0)

                cr.move_to(control_x - 0.8, 22.4)
                cr.line_to(control_x - 5.6, 27.0)
                cr.line_to(control_x - 0.8, 31.6)

                cr.stroke()
                cr.set_line_cap(cairo.LINE_CAP_BUTT)
            else:
                # Gear icon.
                for idx in range(8):
                    ang = idx * math.pi / 4.0
                    cr.move_to(control_x + math.cos(ang) * 5.8, 27 + math.sin(ang) * 5.8)
                    cr.line_to(control_x + math.cos(ang) * 8.7, 27 + math.sin(ang) * 8.7)

                cr.stroke()
                cr.arc(control_x, 27, 4.7, 0, 2 * math.pi)
                cr.stroke()

            # Close button in the far right corner.
            close_x = width - 27
            cr.set_source_rgba(*theme_palette()["text"], 0.105 * a)
            cr.arc(close_x, 27, 13, 0, 2 * math.pi)
            cr.fill()

            cr.set_source_rgba(*theme_palette()["text"], 0.78 * a)
            cr.set_line_width(2)
            cr.move_to(close_x - 5, 22)
            cr.line_to(close_x + 5, 32)
            cr.move_to(close_x + 5, 22)
            cr.line_to(close_x - 5, 32)
            cr.stroke()

            # Reactive microphone glow.
            cx, cy = 58, 58
            lev = max(0.02, min(1.0, self.level_smooth))
            if self.mode == "transcribing":
                base = (1.00, 0.78, 0.30)
                lev = 0.42 + 0.12 * math.sin(time.time() * 8)
            elif self.mode == "typing":
                base = (0.45, 0.72, 1.00)
                lev = 0.45
            elif self.mode == "error":
                base = (1.00, 0.25, 0.36)
                lev = 0.55
            elif self.mode == "settings":
                base = (0.72, 0.80, 1.00)
                lev = 0.35
            else:
                base = (0.55, 0.86, 1.00) if lev > 0.13 else (1.00, 0.29, 0.43)

            # Soft glow directly on the solid card.
            for idx in range(3, 0, -1):
                radius = 18 + lev * 22 + idx * 7
                alpha = (0.040 + lev * 0.070) * idx / 3 * a
                cr.set_source_rgba(base[0], base[1], base[2], alpha)
                cr.arc(cx, cy, radius, 0, 2 * math.pi)
                cr.fill()

            # Solid mic circle.
            cr.set_source_rgba(base[0], base[1], base[2], 0.98 * a)
            cr.arc(cx, cy, 24 + lev * 4, 0, 2 * math.pi)
            cr.fill()

            # Mic glyph.
            cr.set_source_rgba(0.035, 0.040, 0.052, 0.98 * a)
            cr.set_line_width(3.2)

            # Capsule/body.
            self.draw_round_rect(cr, cx - 8, cy - 17, 16, 25, 8)
            cr.stroke()

            # U-shaped support.
            cr.move_to(cx - 14, cy - 3)
            cr.curve_to(cx - 14, cy + 12, cx + 14, cy + 12, cx + 14, cy - 3)
            cr.stroke()

            # Stem + foot/base, lifted slightly more so it visually connects.
            cr.move_to(cx, cy + 9.6)
            cr.line_to(cx, cy + 16.4)
            cr.move_to(cx - 8.3, cy + 16.4)
            cr.line_to(cx + 8.3, cy + 16.4)
            cr.stroke()

            # Text.
            cr.select_font_face("Inter, Cantarell, Sans", 0, 0)
            cr.set_source_rgba(*theme_palette()["text"], 0.95 * a)
            cr.set_font_size(18)
            cr.move_to(96, 47)
            cr.show_text(self.title)

            if self.subtitle:
                cr.set_source_rgba(*theme_palette()["text"], 0.69 * a)
                cr.set_font_size(12.8)
                cr.move_to(96, 70)
                cr.show_text(self.ellipsize_text(cr, self.subtitle, width - 164))

            # Tiny level meter, directly on the solid card.
            if self.mode == "listening":
                meter_label = "Transcribing as you speak."
                x0, y0 = 96, 88
                x1 = x0 + self._text_width(cr, meter_label)
                cr.set_line_width(4)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)

                cr.set_source_rgba(*theme_palette()["text"], 0.17 * a)
                cr.move_to(x0, y0)
                cr.line_to(x1, y0)
                cr.stroke()

                cr.set_source_rgba(base[0], base[1], base[2], 0.95 * a)
                cr.move_to(x0, y0)
                cr.line_to(x0 + (x1 - x0) * min(1.0, lev), y0)
                cr.stroke()

                cr.set_line_cap(cairo.LINE_CAP_BUTT)

            if preview_alpha > 0.02:
                self.draw_live_preview(cr, width, preview_alpha, base)

            if settings_alpha > 0.02:
                cr.save()
                cr.rectangle(14, 104, width - 28, self.settings_extra - 4)
                cr.clip()

                selected_values = {
                    "mic": self.selected_setting_value("mic"),
                    "quality": self.selected_setting_value("quality"),
                    "realtime": self.selected_setting_value("realtime"),
                    "theme": self.selected_setting_value("theme"),
                }

                for item in self.settings_layout()[0]:
                    if item["kind"] == "row":
                        self.draw_settings_row(
                            cr,
                            item["y"],
                            item["label"],
                            self.setting_row_value(item["key"]),
                            item["key"],
                            settings_alpha,
                        )
                    elif item["kind"] == "option":
                        key = item["key"]
                        open_amount = max(0.0, min(1.0, self.dropdown_anim.get(key, 0.0)))
                        if open_amount <= 0.015:
                            continue

                        reveal = float(item.get("reveal", self.ease_out_cubic(open_amount)))
                        slide = (1.0 - reveal) * -7.0
                        self.draw_settings_option(
                            cr,
                            item["y"] + slide,
                            item["label"],
                            item["value"] == selected_values.get(key),
                            settings_alpha * reveal,
                        )

                cr.restore()

            cr.restore()

    overlay_ref: dict[str, object] = {
        "overlay": None,
        "creating": False,
        "last_error": "",
    }
    overlay_lock = threading.Lock()

    def create_overlay_if_needed(reason: str) -> None:
        with overlay_lock:
            if overlay_ref.get("overlay") is not None or overlay_ref.get("creating"):
                return
            overlay_ref["creating"] = True

        try:
            repair_gui_environment_for_user_service()
            overlay_ref["overlay"] = Overlay(app)
            overlay_ref["last_error"] = ""
            log(f"Daemon overlay initialized reason={reason!r}")
        except Exception as exc:
            overlay_ref["last_error"] = repr(exc)
            log(f"Daemon overlay initialization failed reason={reason!r}: {exc!r}\n{traceback.format_exc()}")
        finally:
            with overlay_lock:
                overlay_ref["creating"] = False

    def request_overlay_creation(reason: str) -> None:
        GLib.idle_add(lambda: (create_overlay_if_needed(reason), False)[1])

    def on_activate(_app):
        create_overlay_if_needed("activate")

    app.connect("activate", on_activate)

    def daemon_hot_start_tasks():
        if PRECREATE_OVERLAY:
            create_overlay_if_needed("daemon-hot-start")

        if KEEP_MODEL_WARM:
            ModelManager.warm_async(
                reason="daemon-hot-start",
                delay=MODEL_WARMUP_DELAY_SECONDS,
            )

        return False

    GLib.timeout_add(250, daemon_hot_start_tasks)
  
    def wait_for_overlay() -> Overlay | None:
        request_overlay_creation("socket-command")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            overlay = overlay_ref.get("overlay")
            if overlay is not None:
                return overlay  # type: ignore[return-value]
            time.sleep(0.05)

        last_error = str(overlay_ref.get("last_error") or "")
        if last_error:
            log(f"Overlay still not ready after socket command: {last_error}")

        return None

    def handle_socket(msg: str) -> str:
        if msg == "status":
            return "running"
        if msg == "toggle" or msg == "start":
            overlay = wait_for_overlay()
            if overlay is None:
                return "not-ready"
            def start_it():
                overlay.show_and_record()
                return False
            GLib.idle_add(start_it)
            return "ok"
        if msg == "cancel":
            overlay = wait_for_overlay()
            if overlay is None:
                return "not-ready"
            def cancel_it():
                overlay.invoke_cancel("socket cancel")
                return False
            GLib.idle_add(cancel_it)
            return "ok"
        if msg == "warmup":
            ModelManager.warm_async()
            return "warming"
        if msg == "doctor":
            report = doctor_text()
            overlay = overlay_ref.get("overlay")
            if overlay:
                report += "\nGlass backend: " + overlay.glass.status
            return report
        if msg == "quit":
            def quit_it():
                app.quit()
                return False
            GLib.idle_add(quit_it)
            return "bye"
        return "unknown"

    SocketServer(handle_socket).start()
    return app.run([sys.argv[0]])


def gpu_status_text() -> str:
    lines: list[str] = []
    lines.append(f"Backend: {BACKEND}")
    lines.append(f"Model: {MODEL_NAME}")

    try:
        lines.append(f"Daemon status: {'running' if daemon_is_running() else 'not running'}")
    except Exception:
        lines.append("Daemon status: unknown")

    if command_exists("nvidia-smi"):
        lines.append("")
        lines.append("NVIDIA VRAM / compute processes:")
        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,process_name,used_gpu_memory",
                    "--format=csv,noheader,nounits",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=4,
                check=False,
            )
            out = proc.stdout.strip()
            if out:
                lines.extend("  " + line + " MiB" for line in out.splitlines())
            else:
                lines.append("  no active NVIDIA compute process listed")
        except Exception as exc:
            lines.append(f"  nvidia-smi failed: {exc}")

    if command_exists("rocm-smi"):
        lines.append("")
        lines.append("AMD ROCm VRAM:")
        try:
            proc = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=6,
                check=False,
            )
            out = proc.stdout.strip()
            if out:
                lines.extend("  " + line for line in out.splitlines())
            else:
                lines.append("  rocm-smi returned no VRAM output")
        except Exception as exc:
            lines.append(f"  rocm-smi failed: {exc}")

    if command_exists("radeontop"):
        lines.append("")
        lines.append("AMD live monitor available: radeontop")

    if command_exists("nvtop"):
        lines.append("")
        lines.append("Interactive GPU monitor available: nvtop")

    if not command_exists("nvidia-smi") and not command_exists("rocm-smi"):
        lines.append("")
        lines.append("No vendor VRAM CLI found.")
        lines.append("For debugging, install one of:")
        lines.append("  NVIDIA: nvidia-smi is included with NVIDIA drivers")
        lines.append("  AMD ROCm: rocm-smi")
        lines.append("  Generic live monitor: nvtop")
        lines.append("  AMD live monitor: radeontop")

    return "\n".join(lines)

def doctor_text() -> str:
    lines: list[str] = []
    lines.append(f"KDictate app dir: {APP_DIR}")
    lines.append(f"Session: XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '')} XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '')}")
    lines.append(f"Backend: {BACKEND}")
    lines.append(f"Model: {MODEL_NAME} device={DEVICE} compute_type={COMPUTE_TYPE}")
    if BACKEND == "whisper.cpp":
        lines.append(f"whisper.cpp bin: {WHISPER_CPP_BIN}")
        lines.append(f"whisper.cpp model: {WHISPER_CPP_MODEL}")
    for cmd in ["wl-copy", "wl-paste", "xclip", "xsel", "xdotool", "pactl", "easyeffects", "flatpak", "nvidia-smi"]:
        lines.append(f"{cmd}: {'ok' if command_exists(cmd) else 'missing'}")
    try:
        import evdev  # noqa: F401
        lines.append("python-evdev: ok")
    except Exception as exc:
        lines.append(f"python-evdev: missing ({exc})")
    try:
        from evdev import UInput, ecodes
        ui = UInput({ecodes.EV_KEY: [ecodes.KEY_LEFTCTRL, ecodes.KEY_V]}, name="KDictate Doctor Keyboard")
        ui.close()
        lines.append("/dev/uinput: ok")
    except Exception as exc:
        lines.append(f"/dev/uinput: not writable ({exc})")
    backends = clipboard_backends()
    lines.append(f"Clipboard backends: {', '.join(backends) or 'none'}")
    lines.append(f"Theme: {active_theme()}")
    lines.append(f"Native blur bridge: {'installed' if (APP_DIR / 'native/libverbatim-blur.so').exists() else 'not installed'}")
    truth = WiVRnTruth().read()
    lines.append(f"WiVRn headset connection: {truth if truth is not None else 'unknown'}")
    if AUDIO_COORDINATOR:
        lines.append(f"Audio confirmed VR: {AUDIO_COORDINATOR.machine.stable}")
        lines.append(f"Discord: {AUDIO_COORDINATOR.preferences.status}")
    try:
        subprocess.run(["nvidia-smi"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
    except Exception:
        pass
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        lines.append(f"microphone: ok ({dev.get('name', 'default')})")
    except Exception as exc:
        lines.append(f"microphone: failed ({exc})")

    try:
        source, sink = _find_wivrn_audio()
        if source and sink:
            lines.append(
                f"WiVRn audio: detected input={source.get('label', source['name'])} "
                f"output={sink.get('label', sink['name'])}"
            )
        else:
            lines.append("WiVRn audio endpoints: absent")
    except Exception as exc:
        lines.append(f"WiVRn audio: check failed ({exc})")

    try:
        _prefix, kind = _easyeffects_command_prefix()
        if kind:
            lines.append(f"EasyEffects VR pause: available ({kind}), running={'yes' if _easyeffects_is_running() else 'no'}")
        else:
            lines.append("EasyEffects VR pause: not installed")
    except Exception as exc:
        lines.append(f"EasyEffects VR pause: check failed ({exc})")

    return "\n".join(lines)


def warmup_foreground() -> int:
    print(f"Loading Whisper {MODEL_NAME} on {DEVICE}/{COMPUTE_TYPE}...")
    try:
        ModelManager.load()
    except Exception as exc:
        print(f"Warmup failed: {exc}", file=sys.stderr)
        print(f"Log: {LOG_FILE}", file=sys.stderr)
        return 1
    print("Warmup complete.")
    return 0


def cli_main(argv: list[str]) -> int:
    _ensure_dirs()
    cmd = argv[1] if len(argv) > 1 else "toggle"

    if cmd == "_inference-worker":
        return inference_worker_main()

    if cmd == "last-transcript":
        try:
            print(json.loads((APP_DIR / "last-transcript.json").read_text())["text"])
            return 0
        except (OSError, ValueError, KeyError):
            print("No completed transcript available.", file=sys.stderr)
            return 1

    if cmd == "daemon":
        return daemon_main()

    if cmd in {"toggle", "start", "cancel", "warmup"}:
        if not start_daemon_if_needed():
            print(f"KDictate daemon did not start. Log: {LOG_FILE}", file=sys.stderr)
            return 1
        print(send_socket(cmd))
        return 0

    if cmd == "warmup-foreground":
        return warmup_foreground()

    if cmd == "status":
        if daemon_is_running():
            print("running")
            return 0
        print("not running")
        return 1

    if cmd == "doctor":
        print(send_socket("doctor") if daemon_is_running() else doctor_text())
        return 0

    if cmd == "gpu-status":
        print(gpu_status_text())
        return 0

    if cmd == "quit":
        with contextlib.suppress(Exception):
            print(send_socket("quit"))
        return 0

    print("Usage: kdictate [toggle|start|cancel|daemon|status|doctor|gpu-status|last-transcript|warmup|warmup-foreground|quit]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv))
