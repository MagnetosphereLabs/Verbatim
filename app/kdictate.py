#!/usr/bin/env python3
"""
Core design:
- Wayland-friendly UI using GTK4; uses gtk4-layer-shell when available so the
  overlay does not steal keyboard focus from the text field being dictated into.
- Clipboard paste uses wl-copy plus a persistent /dev/uinput virtual keyboard,
  so full Unicode text can be pasted into Firefox, COSMIC apps, Electron apps,
  terminals, etc. without X11.
- Whisper large-v3 runs locally through faster-whisper/CTranslate2 on CUDA FP16.
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import math
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import re
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
    """Persist user-adjustable settings without disturbing existing installer keys."""
    try:
        _ensure_dirs()

        existing = CONFIG_FILE.read_text(encoding="utf-8").splitlines() if CONFIG_FILE.exists() else []
        seen: set[str] = set()
        output: list[str] = []

        for raw in existing:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                if key in updates:
                    output.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            output.append(raw)

        for key, value in updates.items():
            if key not in seen:
                output.append(f"{key}={value}")

        CONFIG_FILE.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")

        for key, value in updates.items():
            os.environ[key] = value
    except Exception as exc:
        log(f"Could not persist runtime config {updates!r}: {exc!r}")


def realtime_transcription_enabled() -> bool:
    # Default ON unless the user explicitly turns it off.
    return _truthy(os.environ.get("KDICTATE_REALTIME_TRANSCRIPTION", "1"))


def active_silence_to_finish_seconds() -> float:
    if realtime_transcription_enabled():
        return float(os.environ.get("KDICTATE_REALTIME_SILENCE_TO_FINISH_SECONDS", "2.85"))
    return float(os.environ.get("KDICTATE_SILENCE_TO_FINISH_SECONDS", "1.55"))


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
            check=False,
        )
    except Exception as exc:
        log(f"pactl {' '.join(args)} failed: {exc!r}")
        return None


def _pactl_default(kind: str) -> str:
    proc = _pactl(["info"], timeout=3.0)
    if proc is None or proc.returncode != 0:
        return ""

    wanted = "Default Source:" if kind == "source" else "Default Sink:"
    for raw in proc.stdout.splitlines():
        if raw.startswith(wanted):
            return raw.split(":", 1)[1].strip()

    return ""


def _pactl_nodes(kind: str) -> list[dict[str, str]]:
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


def apply_wivrn_audio_if_available(*, force: bool = False) -> dict[str, str] | None:
    with VR_AUDIO_LOCK:
        return _apply_wivrn_audio_if_available_locked(force=force)



def _apply_wivrn_audio_if_available_locked(*, force: bool = False) -> dict[str, str] | None:
    if not wivrn_auto_audio_enabled():
        _restore_vr_audio_defaults()
        return None

    selected = os.environ.get("KDICTATE_MIC_DEVICE", "").strip()

    # Do not override a deliberate non-VR microphone choice. Empty means
    # "System default", where seamless VR auto-switching is allowed.
    if not force and selected not in {"", WIVRN_AUTO_DEVICE_ID} and not selected.startswith(PULSE_SOURCE_PREFIX):
        _restore_vr_audio_defaults()
        return None

    source, sink = _find_wivrn_audio()

    # Only auto-switch when WiVRn has both its input and output present.
    # That is the signal that the headset is connected, not merely that some
    # unrelated app or audio bridge exists.
    if source is None or sink is None:
        if not VR_AUDIO_STATE.get("active"):
            _remember_desktop_audio_defaults()
        _restore_vr_audio_defaults()
        return None

    current_source = _pactl_default("source")
    current_sink = _pactl_default("sink")

    known_sources = _pactl_nodes("sources")
    known_sinks = _pactl_nodes("sinks")

    _remember_desktop_audio_defaults(
        current_source=current_source,
        current_sink=current_sink,
        sources=known_sources,
        sinks=known_sinks,
    )

    if not VR_AUDIO_STATE.get("active"):
        restore_source = (
            current_source
            if _is_non_wivrn_audio_name(current_source, known_sources)
            else str(VR_AUDIO_STATE.get("last_desktop_source") or "")
        )
        restore_sink = (
            current_sink
            if _is_non_wivrn_audio_name(current_sink, known_sinks)
            else str(VR_AUDIO_STATE.get("last_desktop_sink") or "")
        )

        VR_AUDIO_STATE.update({
            "active": True,
            "restore_source": restore_source,
            "restore_sink": restore_sink,
            "restore_deadline": 0.0,
            "restore_attempts": 0,
            "easyeffects_was_running": _easyeffects_is_running(),
        })
        log(
            "WiVRn audio appeared; saving desktop defaults "
            f"source={restore_source!r} sink={restore_sink!r} "
            f"observed_source={current_source!r} observed_sink={current_sink!r} "
            f"easyeffects_was_running={VR_AUDIO_STATE['easyeffects_was_running']!r}"
        )

    changed = False

    if current_source != source["name"]:
        _set_default_audio("source", source["name"])
        changed = True

    if current_sink != sink["name"]:
        _set_default_audio("sink", sink["name"])
        changed = True

    if changed:
        log(
            "WiVRn audio routed "
            f"source={source['name']!r} sink={sink['name']!r} "
            f"previous_source={current_source!r} previous_sink={current_sink!r}"
        )

    _pause_easyeffects_for_vr()

    return {
        "source": source["name"],
        "sink": sink["name"],
        "source_label": source.get("label") or source["name"],
        "sink_label": sink.get("label") or sink["name"],
        "device_id": PULSE_SOURCE_PREFIX + source["name"],
    }

def start_background_vr_audio_monitor() -> None:
    global BACKGROUND_VR_AUDIO_MONITOR_STARTED

    with BACKGROUND_VR_AUDIO_MONITOR_LOCK:
        if BACKGROUND_VR_AUDIO_MONITOR_STARTED:
            return
        BACKGROUND_VR_AUDIO_MONITOR_STARTED = True

    def run_audio_check(reason: str) -> None:
        try:
            active = apply_wivrn_audio_if_available()

            if active is not None and reason != "watchdog":
                log(
                    "Background WiVRn audio check completed "
                    f"reason={reason!r} source={active['source']!r} sink={active['sink']!r}"
                )
        except Exception as exc:
            log(f"Background WiVRn audio check failed reason={reason!r}: {exc!r}")

    def subscribe_worker() -> None:
        log("Background WiVRn audio monitor started with pactl subscribe")
        run_audio_check("startup")

        while True:
            if not command_exists("pactl"):
                log("Background WiVRn audio monitor waiting: pactl is missing")
                time.sleep(10.0)
                continue

            proc: subprocess.Popen[str] | None = None

            try:
                proc = subprocess.Popen(
                    ["pactl", "subscribe"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )

                assert proc.stdout is not None

                for raw in proc.stdout:
                    line = raw.strip()
                    lower = line.lower()

                    if not line:
                        continue

                    if any(token in lower for token in ("sink", "source", "card", "server")):
                        # Let PipeWire/Pulse finish publishing both sides of the
                        # device before scanning sources/sinks.
                        time.sleep(0.35)
                        run_audio_check(f"pactl event: {line}")

                rc = proc.wait()

                err = ""
                if proc.stderr is not None:
                    with contextlib.suppress(Exception):
                        err = proc.stderr.read().strip()

                log(f"Background WiVRn pactl subscribe exited rc={rc} stderr={err!r}")
            except Exception as exc:
                log(f"Background WiVRn pactl subscribe failed: {exc!r}")

                if proc is not None:
                    with contextlib.suppress(Exception):
                        proc.kill()

            time.sleep(2.0)

    def watchdog_worker() -> None:
        # Fallback for missed events, PipeWire restarts, or desktop policy resets.
        while True:
            time.sleep(15.0)
            run_audio_check("watchdog")

    threading.Thread(
        target=subscribe_worker,
        daemon=True,
        name="KDictateWiVRnAudioSubscribe",
    ).start()

    threading.Thread(
        target=watchdog_worker,
        daemon=True,
        name="KDictateWiVRnAudioWatchdog",
    ).start()


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


def resolve_microphone_device(selected_mic: str) -> tuple[int | str | None, str]:
    selected_mic = (selected_mic or "").strip()

    if selected_mic in {"", WIVRN_AUTO_DEVICE_ID}:
        active = apply_wivrn_audio_if_available(force=(selected_mic == WIVRN_AUTO_DEVICE_ID))

        if active is not None:
            bridge_idx = _portaudio_pulse_bridge_index()
            if bridge_idx is not None:
                return bridge_idx, f"WiVRn microphone via Pulse ({bridge_idx})"
            return None, "WiVRn microphone via system default"

    if selected_mic.startswith(PULSE_SOURCE_PREFIX):
        source_name = selected_mic[len(PULSE_SOURCE_PREFIX):]
        source = next((node for node in _pactl_nodes("sources") if node["name"] == source_name), None)

        if source is not None:
            _set_default_audio("source", source_name)

            if _is_wivrn_node(source):
                sink = next((node for node in _pactl_nodes("sinks") if _is_wivrn_node(node)), None)
                if sink is not None:
                    _set_default_audio("sink", sink["name"])

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
MAX_RECORD_SECONDS = float(os.environ.get("KDICTATE_MAX_RECORD_SECONDS", "90"))
SILENCE_TO_FINISH_SECONDS = float(os.environ.get("KDICTATE_SILENCE_TO_FINISH_SECONDS", "1.55"))
REALTIME_SILENCE_TO_FINISH_SECONDS = float(os.environ.get("KDICTATE_REALTIME_SILENCE_TO_FINISH_SECONDS", "2.85"))

AUDIO_RMS_AVERAGE_WINDOW = max(2, int(os.environ.get("KDICTATE_RMS_AVERAGE_WINDOW", "10")))
AUDIO_NOISE_CALIBRATION_SECONDS = float(os.environ.get("KDICTATE_NOISE_CALIBRATION_SECONDS", "0.75"))
AUDIO_MIN_SPEECH_THRESHOLD = float(os.environ.get("KDICTATE_MIN_SPEECH_THRESHOLD", "0.0065"))
AUDIO_SPEECH_THRESHOLD_MULTIPLIER = float(os.environ.get("KDICTATE_SPEECH_THRESHOLD_MULTIPLIER", "2.65"))
AUDIO_LOWEST_FLOOR_HEADROOM = float(os.environ.get("KDICTATE_LOWEST_FLOOR_HEADROOM", "1.12"))


WINDOW_W = 330
WINDOW_H = 118
LISTENING_PREVIEW_EXTRA_H = 144
LISTENING_PREVIEW_WINDOW_H = WINDOW_H + LISTENING_PREVIEW_EXTRA_H
SETTINGS_EXTRA_H = 184
SETTINGS_MAX_EXTRA_H = 360
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
    return subprocess.run(["sh", "-lc", f"command -v {name} >/dev/null 2>&1"], check=False).returncode == 0

def repair_gui_environment_for_user_service() -> None:
    """Repair display environment when launched by systemd --user too early.

    On some desktops, the user service can start before WAYLAND_DISPLAY or
    DBUS_SESSION_BUS_ADDRESS are present in the service environment. The daemon
    can still run, but GTK cannot create the overlay until these are available.
    """
    os.environ.setdefault("XDG_RUNTIME_DIR", str(RUNTIME_DIR))

    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        bus_path = RUNTIME_DIR / "bus"
        if bus_path.exists():
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"

    if not os.environ.get("WAYLAND_DISPLAY"):
        for candidate in sorted(RUNTIME_DIR.glob("wayland-*")):
            if candidate.name.endswith(".lock"):
                continue
            if candidate.is_socket():
                os.environ["WAYLAND_DISPLAY"] = candidate.name
                os.environ.setdefault("XDG_SESSION_TYPE", "wayland")
                log(f"Detected Wayland display for user service: {candidate.name}")
                break

    if not os.environ.get("DISPLAY"):
        for display_num in range(0, 4):
            if Path(f"/tmp/.X11-unix/X{display_num}").exists():
                os.environ["DISPLAY"] = f":{display_num}"
                os.environ.setdefault("XDG_SESSION_TYPE", "x11")
                log(f"Detected X11 display for user service: :{display_num}")
                break

_SENTENCE_BOUNDARY_STARTERS = (
    "This", "That", "It", "There", "These", "Those",
    "Then", "So", "But", "However", "Now", "Next",
    "Also", "Finally", "Basically", "Actually", "Overall",
)


def normalize_transcript_text(text: str, *, final: bool = False) -> str:
    """Lightly clean Whisper dictation without rewriting the user's words."""
    text = " ".join((text or "").split()).strip()
    if not text:
        return ""

    # Normal spacing around punctuation.
    text = re.sub(r"\s+([,.;:!?%)\]\}])", r"\1", text)
    text = re.sub(r"([(\[\{])\s+", r"\1", text)
    text = re.sub(r"([.!?])([A-Za-z])", r"\1 \2", text)

    # Conservative repair for the common realtime boundary glitch:
    # "... talking This next sentence ..." -> "... talking. This next sentence ..."
    starters = "|".join(re.escape(word) for word in _SENTENCE_BOUNDARY_STARTERS)
    text = re.sub(rf"(?<=[a-z0-9])\s+(?=({starters})\b)", ". ", text)

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
            try:
                from evdev import ecodes

                ready_delay = self._ready_at - time.time()
                if ready_delay > 0:
                    time.sleep(ready_delay)

                with self._lock:
                    ui = self._ui
                    for code, value in events:
                        ui.write(ecodes.EV_KEY, code, value)
                        ui.syn()
                        time.sleep(delay)

                return True
            except Exception as exc:
                log(f"uinput key event injection failed: {exc!r}")

        if command_exists("ydotool"):
            try:
                proc = subprocess.run(
                    ["ydotool", "key", *[f"{code}:{value}" for code, value in events]],
                    env=self._ydotool_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                if proc.returncode == 0:
                    return True
                log(f"ydotool key event injection failed: {proc.stderr.strip()}")
            except Exception as exc:
                log(f"ydotool key event injection exception: {exc!r}")

        return False

    def paste_shortcut(self) -> bool:
        return self._emit_raw_key_events([
            (self.KEY_LEFTCTRL, 1),
            (self.KEY_V, 1),
            (self.KEY_V, 0),
            (self.KEY_LEFTCTRL, 0),
        ], delay=0.030)

    def copy_shortcut(self) -> bool:
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

    def _read_clipboard_text(self, timeout: float = 0.8) -> tuple[bool, str | None]:
        if not command_exists("wl-paste"):
            return False, None

        try:
            proc = run(["wl-paste", "--no-newline"], timeout=timeout)
            if proc.returncode == 0:
                return True, proc.stdout
        except Exception:
            pass

        return False, None

    def _set_clipboard_text(self, value: str, label: str) -> tuple[bool, str, subprocess.Popen | None]:
        try:
            proc = subprocess.Popen(
                ["wl-copy", "--type", "text/plain;charset=utf-8"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.stdin is not None
            proc.stdin.write(value)
            proc.stdin.close()

            try:
                rc = proc.wait(timeout=0.7)
                if rc != 0:
                    err = proc.stderr.read().strip() if proc.stderr else ""
                    return False, f"wl-copy {label} failed: {err}", None
                return True, "clipboard set", None
            except subprocess.TimeoutExpired:
                # On Wayland, wl-copy often remains alive as the clipboard owner.
                # That is success. The owner must stay alive until the paste happens.
                log(f"wl-copy {label} is still running as clipboard owner; treating as success")
                return True, "clipboard owner active", proc
        except Exception as exc:
            return False, f"wl-copy {label} failed: {exc}", None

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
        if not command_exists("wl-copy"):
            return False, "wl-copy is not installed. The installer should have installed wl-clipboard."

        old_ok, old_clip = self._read_clipboard_text(timeout=0.8)

        if self._should_prefix_space(text):
            text = " " + text

        ok, msg, owner_proc = self._set_clipboard_text(text, "dictation")
        if not ok:
            if old_ok and old_clip is not None:
                with contextlib.suppress(Exception):
                    self._set_clipboard_text(old_clip, "restore after failed dictation")
            return False, msg

        self.pause_monitor(1.5)
        time.sleep(0.18)

        if not self.injector.paste_shortcut():
            if old_ok and old_clip is not None:
                with contextlib.suppress(Exception):
                    self._set_clipboard_text(old_clip, "restore after failed paste")
            return False, "Could not inject Ctrl+V through /dev/uinput or ydotool. Run kdictate doctor."

        if old_ok and old_clip is not None:
            def restore() -> None:
                # Give the focused app enough time to consume Ctrl+V before restoring.
                time.sleep(1.4)
                with contextlib.suppress(Exception):
                    self._set_clipboard_text(old_clip, "restore")

            threading.Thread(target=restore, daemon=True).start()

        return True, "pasted"

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
    def warm_async(cls) -> None:
        def worker() -> None:
            try:
                cls.load()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

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
    started_at: float = 0.0
    last_speech_at: float = 0.0
    speech_seen: bool = False
    noise: list[float] = dataclasses.field(default_factory=list)
    rms_window: list[float] = dataclasses.field(default_factory=list)


class DictationEngine:
    def __init__(self, ui) -> None:
        self.ui = ui
        self.state = AudioState(frames=[])
        self.stream = None
        self.lock = threading.Lock()
        self.recording = False
        self.cancelled = False
        self.realtime_busy = False
        self.realtime_last_start = 0.0
        self.realtime_last_audio_seconds = 0.0
        self.realtime_preview_audio_seconds = 0.0

    def start(self) -> None:
        if self.recording:
            self.cancel("restarted")
        self.state = AudioState(frames=[])
        self.cancelled = False
        self.recording = True
        self.realtime_busy = False
        self.realtime_last_start = 0.0
        self.realtime_last_audio_seconds = 0.0
        self.realtime_preview_audio_seconds = 0.0
        now = time.time()
        self.state.started_at = now
        self.state.last_speech_at = now

        try:
            import numpy as np
            import sounddevice as sd

            selected_mic = os.environ.get("KDICTATE_MIC_DEVICE", "").strip()
            device_arg, resolved_mic_label = resolve_microphone_device(selected_mic)

            try:
                if device_arg is None:
                    dev = sd.query_devices(kind="input")
                else:
                    dev = sd.query_devices(device=device_arg, kind="input")

                samplerate = int(dev.get("default_samplerate") or 48000)
                mic_label = str(dev.get("name", resolved_mic_label))
            except Exception:
                samplerate = 48000
                mic_label = resolved_mic_label or "default"

            self.state.samplerate = samplerate

            def callback(indata, frames, time_info, status):
                if status:
                    log(f"Audio status: {status}")

                mono = indata[:, 0].astype(np.float32).copy()
                rms = float(np.sqrt(np.mean(np.square(mono))) + 1e-9)

                with self.lock:
                    self.state.frames.append(mono)
                    self.state.latest_rms = rms

                    # Smooth the mic level before VAD decisions.
                    self.state.rms_window.append(rms)
                    if len(self.state.rms_window) > AUDIO_RMS_AVERAGE_WINDOW:
                        self.state.rms_window = self.state.rms_window[-AUDIO_RMS_AVERAGE_WINDOW:]

                    avg_rms = float(np.mean(self.state.rms_window)) if self.state.rms_window else rms
                    self.state.latest_avg_rms = avg_rms

                    # Track the quietest averaged mic level heard after the keybind.
                    if avg_rms > 1e-7:
                        if self.state.lowest_avg_rms <= 0.0:
                            self.state.lowest_avg_rms = avg_rms
                        else:
                            self.state.lowest_avg_rms = min(self.state.lowest_avg_rms, avg_rms)

            self.stream = sd.InputStream(
                samplerate=samplerate,
                channels=1,
                dtype="float32",
                blocksize=0,
                device=device_arg,
                callback=callback,
            )
            self.stream.start()
            log(
                f"Recording started samplerate={samplerate} "
                f"microphone={mic_label!r} realtime={realtime_transcription_enabled()}"
            )
        except Exception as exc:
            self.recording = False
            self.ui.show_error(f"Microphone failed: {exc}")
            log(f"Microphone failed: {exc!r}\n{traceback.format_exc()}")

    def cancel(self, reason: str) -> None:
        self.cancelled = True
        self.recording = False
        with contextlib.suppress(Exception):
            if self.stream:
                self.stream.stop()
                self.stream.close()
        self.stream = None
        log(f"Dictation cancelled: {reason}")

    def tick(self) -> None:
        if not self.recording:
            return
        now = time.time()
        age = now - self.state.started_at
        with self.lock:
            rms = self.state.latest_rms
            avg_rms = self.state.latest_avg_rms or rms
            lowest_avg_rms = self.state.lowest_avg_rms

        self.ui.set_level(avg_rms)

        if age < 0.45:
            if avg_rms > 1e-6:
                self.state.noise.append(avg_rms)
            return

        # Keep learning the audio floor until speech is actually detected.
        if age < AUDIO_NOISE_CALIBRATION_SECONDS and not self.state.speech_seen:
            if avg_rms > 1e-6:
                self.state.noise.append(avg_rms)

        try:
            import numpy as np
            learned_floor = float(np.percentile(self.state.noise, 70)) if self.state.noise else 0.003
        except Exception:
            learned_floor = 0.003

        # lowest averaged volume heard by the mic after the keybind started.
        observed_floor = float(lowest_avg_rms or 0.0)
        noise_floor = max(learned_floor, observed_floor)
        self.state.noise_floor = noise_floor

        threshold = max(
            AUDIO_MIN_SPEECH_THRESHOLD,
            noise_floor * AUDIO_SPEECH_THRESHOLD_MULTIPLIER,
            observed_floor * AUDIO_LOWEST_FLOOR_HEADROOM,
        )

        if avg_rms > threshold:
            self.state.speech_seen = True
            self.state.last_speech_at = now

            if realtime_transcription_enabled():
                self.ui.set_status("listening", "Listening live", "Transcribing as you speak.")
            else:
                self.ui.set_status("listening", "Listening", "Keep talking, or pause to finish.")

        if realtime_transcription_enabled():
            self._maybe_realtime_transcribe(now)

        silence_to_finish = active_silence_to_finish_seconds()

        if self.state.speech_seen and (now - self.state.last_speech_at) >= silence_to_finish and age >= 1.1:
            self.finish()
        elif age >= MAX_RECORD_SECONDS:
            self.finish()
        elif not self.state.speech_seen and age >= 12.0:
            self.cancel("no speech detected")
            self.ui.close_smoothly()

    def finish(self) -> None:
        if not self.recording:
            return
        self.recording = False
        with contextlib.suppress(Exception):
            if self.stream:
                self.stream.stop()
                self.stream.close()
        self.stream = None

        with self.lock:
            frames = list(self.state.frames)
            samplerate = self.state.samplerate
            last_speech_elapsed = max(0.0, self.state.last_speech_at - self.state.started_at)

        if self.cancelled:
            return
        if not frames:
            self.cancel("no audio frames")
            self.ui.close_smoothly()
            return

        preview_text = ""
        if realtime_transcription_enabled():
            preview_text = str(getattr(self.ui, "realtime_preview", "") or "").strip()

        if preview_text:
            preview_audio_seconds = max(0.0, float(self.realtime_preview_audio_seconds or 0.0))
            speech_tail_gap = max(0.0, last_speech_elapsed - preview_audio_seconds)
            allowed_preview_lag = max(1.25, active_silence_to_finish_seconds() + 0.35)

            if speech_tail_gap <= allowed_preview_lag:
                quick_text = normalize_transcript_text(preview_text, final=True)
                if quick_text:
                    log(
                        "Using current realtime preview for final paste "
                        f"preview_audio={preview_audio_seconds:.2f}s "
                        f"last_speech={last_speech_elapsed:.2f}s "
                        f"allowed_lag={allowed_preview_lag:.2f}s"
                    )
                    self.ui.invoke_transcribed(quick_text)
                    return

        self.ui.set_status("transcribing", "Transcribing", f"Whisper {MODEL_NAME} via {BACKEND}")
        threading.Thread(target=self._transcribe_worker, args=(frames, samplerate), daemon=True).start()

    def _transcribe_file(self, wav_path: str, *, realtime: bool = False) -> str:
        if BACKEND == "whisper.cpp":
            return transcribe_with_whisper_cpp(wav_path)

        model = ModelManager.load()
        segments, info = model.transcribe(
            wav_path,
            language=LANGUAGE,
            task="transcribe",
            beam_size=1 if realtime else 5,
            vad_filter=False if realtime else True,
            vad_parameters={"min_silence_duration_ms": 850 if realtime else 450},
            condition_on_previous_text=False if realtime else True,
            temperature=0.0,
            no_speech_threshold=0.35,
            compression_ratio_threshold=2.4,
        )

        return " ".join(seg.text.strip() for seg in segments).strip()

    def _write_wav_and_transcribe(self, frames, samplerate: int, *, realtime: bool) -> str:
        import numpy as np
        import soundfile as sf

        audio = np.concatenate(frames).astype(np.float32)
        min_seconds = 0.85 if realtime else 0.30

        if audio.size < samplerate * min_seconds:
            return ""

        fd, wav_path = tempfile.mkstemp(prefix="kdictate-live-" if realtime else "kdictate-", suffix=".wav")
        os.close(fd)

        try:
            sf.write(wav_path, audio, samplerate)
            return self._transcribe_file(wav_path, realtime=realtime)
        finally:
            with contextlib.suppress(Exception):
                os.unlink(wav_path)

    def _maybe_realtime_transcribe(self, now: float) -> None:
        if self.realtime_busy or self.cancelled:
            return

        if now - self.realtime_last_start < 1.25:
            return

        with self.lock:
            frames = list(self.state.frames)
            samplerate = self.state.samplerate

        if not frames or samplerate <= 0:
            return

        audio_seconds = sum(len(frame) for frame in frames) / float(samplerate)

        if audio_seconds < 1.1 or audio_seconds - self.realtime_last_audio_seconds < 0.65:
            return

        self.realtime_busy = True
        self.realtime_last_start = now
        self.realtime_last_audio_seconds = audio_seconds

        threading.Thread(target=self._realtime_worker, args=(frames, samplerate), daemon=True).start()

    def _realtime_worker(self, frames, samplerate: int) -> None:
        try:
            audio_seconds = sum(len(frame) for frame in frames) / float(samplerate) if samplerate > 0 else 0.0
            text = self._write_wav_and_transcribe(frames, samplerate, realtime=True)
            text = normalize_transcript_text(text, final=False)

            if text and self.recording and not self.cancelled and realtime_transcription_enabled():
                self.ui.invoke_realtime_text(text, audio_seconds)
        except Exception as exc:
            log(f"Realtime transcription preview failed: {exc!r}")
        finally:
            self.realtime_busy = False

    def _transcribe_worker(self, frames, samplerate: int) -> None:
        try:
            text = self._write_wav_and_transcribe(frames, samplerate, realtime=False)

            if not text:
                self.ui.invoke_cancel("no speech recognized")
                return

            text = normalize_transcript_text(text, final=True)
            log(f"Transcribed {len(text)} chars: {text[:240]!r}")
            self.ui.invoke_transcribed(text)
        except Exception as exc:
            log(f"Transcription failed: {exc!r}\n{traceback.format_exc()}")
            self.ui.invoke_error(str(exc))


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
            self.monitor = KeyboardMonitor(lambda: GLib.idle_add(self.invoke_cancel, "manual typing"))
            self.monitor.start()
            self.paster = ClipboardPaster(injector, self.monitor.pause_for)
            self.layer_enabled = False

            self.settings_open = False
            self.settings_anim = 0.0
            self.settings_extra = float(SETTINGS_EXTRA_H)
            self.settings_extra_target = float(SETTINGS_EXTRA_H)
            self.open_dropdown: str | None = None
            self.dropdown_anim = {"mic": 0.0, "quality": 0.0, "realtime": 0.0}
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

            if LayerShell is not None:
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
            self.window_x = int(max(0, x))
            self.window_y = int(max(0, y))

            if self.layer_enabled and LayerShell is not None:
                with contextlib.suppress(Exception):
                    LayerShell.set_margin(self.window, LayerShell.Edge.LEFT, self.window_x)
                    LayerShell.set_margin(self.window, LayerShell.Edge.TOP, self.window_y)
            else:
                with contextlib.suppress(Exception):
                    self.window.move(self.window_x, self.window_y)

        def poll_vr_audio(self):
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
            if key == "mic":
                return os.environ.get("KDICTATE_MIC_DEVICE", "").strip()
            if key == "quality":
                return _normalize_profile(os.environ.get("KDICTATE_PROFILE"), os.environ.get("KDICTATE_MODEL"))
            if key == "realtime":
                return "1" if realtime_transcription_enabled() else "0"
            return ""

        def setting_row_value(self, key: str) -> str:
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
            self.window.present()
            self.area.queue_draw()

        def set_window_height(self, height: int) -> None:
            if self.current_window_h == height:
                return

            self.current_window_h = height
            self.area.set_content_height(height)
            self.window.set_default_size(WINDOW_W, height)

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
            self.window.present()
            self.area.queue_draw()

            # Load GPU model only when the user invokes dictation.
            # This keeps the daemon ready without occupying NVIDIA VRAM 24/7.
            if BACKEND != "whisper.cpp":
                ModelManager.warm_async()

            self.engine.start()

        def close_smoothly(self):
            self.monitor.disarm()
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
                previous_len = len(self.realtime_preview)

                # Whisper can occasionally emit a much shorter interim hypothesis.
                # Ignoring that one frame regression prevents the live transcript
                # from jumping upward and then snapping back down.
                if previous_len > 80 and len(text) < previous_len * 0.72:
                    log(
                        "Ignored short realtime preview regression "
                        f"old_len={previous_len} new_len={len(text)}"
                    )
                    return False

                self.realtime_preview = text
                self.engine.realtime_preview_audio_seconds = max(
                    self.engine.realtime_preview_audio_seconds,
                    float(audio_seconds or 0.0),
                )

                if len(text) < previous_len:
                    self.preview_draw_chars = min(self.preview_draw_chars, float(len(text)))

                # If the user has not intentionally scrolled back, keep the live
                # transcript pinned to the newest line with a smooth upward drift.
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

            def paste_worker() -> None:
                ok, msg = self.paster.paste_text(text)

                if not ok:
                    GLib.idle_add(self.show_error, msg)
                else:
                    GLib.idle_add(self.close_smoothly)

            threading.Thread(target=paste_worker, daemon=True).start()
            return False

        def show_error(self, msg: str):
            self.monitor.disarm()
            self.settings_open = False
            self.set_status("error", "Needs attention", msg[:96])
            self.fade_target = 1.0
            self.visible = True
            self.window.present()
            def later():
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

            cr.set_source_rgba(1, 1, 1, 0.46 * a)
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
            cr.set_source_rgba(1, 1, 1, (0.070 + 0.030 * open_amount) * a)
            cr.fill()

            if open_amount > 0.02:
                cr.set_source_rgba(0.72, 0.80, 1.00, 0.065 * open_amount * a)
                self.draw_round_rect(cr, 24, y, WINDOW_W - 48, 38, 13)
                cr.fill()

            cr.set_source_rgba(1, 1, 1, 0.92 * a)
            cr.set_font_size(12.4)
            cr.move_to(39, y + 16)
            cr.show_text(label)

            cr.set_source_rgba(1, 1, 1, 0.58 * a)
            cr.set_font_size(11.0)
            shown = self.ellipsize_text(cr, value, WINDOW_W - 104)
            cr.move_to(39, y + 31)
            cr.show_text(shown)

            self.draw_chevron(cr, WINDOW_W - 42, y + 19, open_amount * (math.pi / 2.0), a)

        def draw_settings_option(self, cr, y: float, label: str, selected: bool, a: float) -> None:
            self.draw_round_rect(cr, 34, y, WINDOW_W - 68, 26, 10)
            cr.set_source_rgba(1, 1, 1, (0.060 if not selected else 0.105) * a)
            cr.fill()

            if selected:
                cr.set_source_rgba(0.72, 0.80, 1.00, 0.20 * a)
                cr.arc(47, y + 13, 3.2, 0, 2 * math.pi)
                cr.fill()

            cr.set_source_rgba(1, 1, 1, (0.62 if not selected else 0.91) * a)
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

            cr.set_source_rgba(1, 1, 1, 0.066 * a)
            self.draw_round_rect(cr, 20, panel_y + 18, width - 40, panel_h - 20, 18)
            cr.fill()

            cr.set_source_rgba(1, 1, 1, 0.118 * a)
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

                cr.set_source_rgba(1, 1, 1, line_alpha)
                cr.move_to(viewport_x, yy + 15)
                cr.show_text(line)

            if realtime_transcription_enabled() and self.engine.recording:
                caret_on = math.sin(time.time() * 7.5) > -0.15
                if caret_on and lines:
                    last_line = lines[-1]
                    last_line_y = viewport_y + (len(lines) - 1) * line_h - self.preview_scroll
                    if viewport_y - line_h < last_line_y < viewport_y + viewport_h + line_h:
                        caret_x = viewport_x + min(viewport_w - 6, self._text_width(cr, last_line) + 4)
                        cr.set_source_rgba(1, 1, 1, 0.82 * a)
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

            # One unified solid dark card. No separate transparent-looking islands.
            cr.set_source_rgba(0.045, 0.047, 0.060, 0.995 * a)
            self.draw_round_rect(cr, 8, 7, width - 16, effective_h - 17, 24)
            cr.fill()

            # Subtle border/highlight.
            cr.set_source_rgba(1, 1, 1, 0.105 * a)
            self.draw_round_rect(cr, 8.5, 7.5, width - 17, effective_h - 18, 24)
            cr.set_line_width(1)
            cr.stroke()

            # Settings gear normally; back arrow while already inside settings.
            control_x = width - 58
            cr.set_source_rgba(1, 1, 1, 0.105 * a)
            cr.arc(control_x, 27, 13, 0, 2 * math.pi)
            cr.fill()

            cr.set_source_rgba(1, 1, 1, 0.76 * a)
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
            cr.set_source_rgba(1, 1, 1, 0.105 * a)
            cr.arc(close_x, 27, 13, 0, 2 * math.pi)
            cr.fill()

            cr.set_source_rgba(1, 1, 1, 0.78 * a)
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
            cr.set_source_rgba(1, 1, 1, 0.95 * a)
            cr.set_font_size(18)
            cr.move_to(96, 47)
            cr.show_text(self.title)

            if self.subtitle:
                cr.set_source_rgba(1, 1, 1, 0.69 * a)
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

                cr.set_source_rgba(1, 1, 1, 0.17 * a)
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
            return doctor_text()
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
    for cmd in ["wl-copy", "wl-paste", "pactl", "easyeffects", "flatpak", "nvidia-smi"]:
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
    try:
        run(["wl-copy"], input_text="", timeout=1.0)
        lines.append("Wayland clipboard: ok")
    except subprocess.TimeoutExpired:
        # wl-copy may stay alive as the clipboard owner on Wayland.
        # That is normal and usable, not a failure.
        lines.append("Wayland clipboard: ok (wl-copy stayed alive as clipboard owner)")
    except Exception as exc:
        lines.append(f"Wayland clipboard: failed ({exc})")
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
            lines.append("WiVRn audio: not connected")
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
        print(doctor_text())
        return 0

    if cmd == "gpu-status":
        print(gpu_status_text())
        return 0

    if cmd == "quit":
        with contextlib.suppress(Exception):
            print(send_socket("quit"))
        return 0

    print("Usage: kdictate [toggle|start|cancel|daemon|status|doctor|gpu-status|warmup|warmup-foreground|quit]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv))
