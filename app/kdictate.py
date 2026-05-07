#!/usr/bin/env python3
"""
KDictate Cosmic: local Whisper dictation overlay for COSMIC/Wayland.

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
MODEL_NAME = os.environ.get("KDICTATE_MODEL", "small.en")
BACKEND = os.environ.get("KDICTATE_BACKEND", "faster-whisper").strip().lower()
DEVICE = os.environ.get("KDICTATE_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("KDICTATE_COMPUTE_TYPE", "float16")
LANGUAGE = os.environ.get("KDICTATE_LANGUAGE", "en")
WHISPER_CPP_BIN = os.environ.get("KDICTATE_WHISPER_CPP_BIN", str(APP_DIR / "whisper.cpp/build/bin/whisper-cli"))
WHISPER_CPP_MODEL = os.environ.get("KDICTATE_WHISPER_CPP_MODEL", str(APP_DIR / f"models/ggml-{MODEL_NAME}.bin"))
MAX_RECORD_SECONDS = float(os.environ.get("KDICTATE_MAX_RECORD_SECONDS", "90"))
SILENCE_TO_FINISH_SECONDS = float(os.environ.get("KDICTATE_SILENCE_TO_FINISH_SECONDS", "1.55"))

WINDOW_W = 330
WINDOW_H = 118


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


class InputInjector:
    """Persistent virtual keyboard used to send Ctrl+V on Wayland."""

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
                        ecodes.KEY_LEFTCTRL,
                        ecodes.KEY_RIGHTCTRL,
                        ecodes.KEY_V,
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

    def paste_shortcut(self) -> bool:
        if self.ensure() and self._ui is not None:
            try:
                from evdev import ecodes

                delay = self._ready_at - time.time()
                if delay > 0:
                    time.sleep(delay)
                with self._lock:
                    ui = self._ui
                    ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
                    ui.write(ecodes.EV_KEY, ecodes.KEY_V, 1)
                    ui.syn()
                    time.sleep(0.035)
                    ui.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
                    ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
                    ui.syn()
                return True
            except Exception as exc:
                log(f"uinput paste shortcut failed: {exc!r}")

        # Fallback for systems where python-evdev cannot access /dev/uinput.
        if command_exists("ydotool"):
            env = os.environ.copy()
            runtime_socket = RUNTIME_DIR / ".ydotool_socket"
            if runtime_socket.exists():
                env["YDOTOOL_SOCKET"] = str(runtime_socket)
            elif Path("/tmp/.ydotool_socket").exists():
                env["YDOTOOL_SOCKET"] = "/tmp/.ydotool_socket"

            # New ydotool uses raw keycodes. Older Ubuntu builds often use names.
            attempts = [
                ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
                ["ydotool", "key", "ctrl+v"],
            ]
            for cmd in attempts:
                try:
                    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0)
                    if proc.returncode == 0:
                        return True
                    log(f"ydotool attempt failed {cmd}: {proc.stderr.strip()}")
                except Exception as exc:
                    log(f"ydotool attempt exception {cmd}: {exc!r}")
        return False


class ClipboardPaster:
    def __init__(self, injector: InputInjector, pause_monitor: Callable[[float], None]) -> None:
        self.injector = injector
        self.pause_monitor = pause_monitor

    def paste_text(self, text: str) -> tuple[bool, str]:
        if not command_exists("wl-copy"):
            return False, "wl-copy is not installed. The installer should have installed wl-clipboard."

        old_clip: str | None = None
        old_ok = False
        if command_exists("wl-paste"):
            try:
                old = run(["wl-paste", "--no-newline"], timeout=0.8)
                if old.returncode == 0:
                    old_clip = old.stdout
                    old_ok = True
            except Exception:
                old_ok = False

        def set_clipboard(value: str, label: str) -> tuple[bool, str, subprocess.Popen | None]:
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
                    # Some Wayland compositors keep wl-copy alive as the clipboard
                    # owner. That is success, not failure. Do not kill it before
                    # the paste shortcut has consumed the clipboard.
                    log(f"wl-copy {label} is still running as clipboard owner; treating as success")
                    return True, "clipboard owner active", proc
            except Exception as exc:
                return False, f"wl-copy {label} failed: {exc}", None

        ok, msg, owner_proc = set_clipboard(text, "dictation")
        if not ok:
            return False, msg

        self.pause_monitor(1.5)
        time.sleep(0.18)

        if not self.injector.paste_shortcut():
            return False, "Could not inject Ctrl+V through /dev/uinput or ydotool. Run kdictate doctor."

        if old_ok and old_clip is not None:
            def restore() -> None:
                # Give the focused app enough time to consume Ctrl+V before restoring.
                time.sleep(1.4)
                with contextlib.suppress(Exception):
                    set_clipboard(old_clip, "restore")
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
    _lock = threading.Lock()
    _loading = False
    _load_error: str | None = None

    @classmethod
    def is_loading(cls) -> bool:
        return cls._loading

    @classmethod
    def load(cls):
        """Load only the Python faster-whisper model.

        whisper.cpp is an external binary backend and does not need a long-lived
        Python model object.
        """
        if BACKEND == "whisper.cpp":
            return None

        with cls._lock:
            if cls._model is not None:
                return cls._model
            cls._loading = True
            cls._load_error = None
            log(f"Loading faster-whisper model={MODEL_NAME} device={DEVICE} compute_type={COMPUTE_TYPE}")
            try:
                from faster_whisper import WhisperModel
                cls._model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
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
    started_at: float = 0.0
    last_speech_at: float = 0.0
    speech_seen: bool = False
    noise: list[float] = dataclasses.field(default_factory=list)


class DictationEngine:
    def __init__(self, ui) -> None:
        self.ui = ui
        self.state = AudioState(frames=[])
        self.stream = None
        self.lock = threading.Lock()
        self.recording = False
        self.cancelled = False

    def start(self) -> None:
        if self.recording:
            self.cancel("restarted")
        self.state = AudioState(frames=[])
        self.cancelled = False
        self.recording = True
        now = time.time()
        self.state.started_at = now
        self.state.last_speech_at = now

        try:
            import numpy as np
            import sounddevice as sd

            try:
                dev = sd.query_devices(kind="input")
                samplerate = int(dev.get("default_samplerate") or 48000)
            except Exception:
                samplerate = 48000
            self.state.samplerate = samplerate

            def callback(indata, frames, time_info, status):
                if status:
                    log(f"Audio status: {status}")
                mono = indata[:, 0].astype(np.float32).copy()
                rms = float(np.sqrt(np.mean(np.square(mono))) + 1e-9)
                with self.lock:
                    self.state.frames.append(mono)
                    self.state.latest_rms = rms

            self.stream = sd.InputStream(
                samplerate=samplerate,
                channels=1,
                dtype="float32",
                blocksize=0,
                callback=callback,
            )
            self.stream.start()
            log(f"Recording started samplerate={samplerate}")
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
        self.ui.set_level(rms)

        if age < 0.45:
            if rms > 1e-6:
                self.state.noise.append(rms)
            return

        try:
            import numpy as np
            noise_floor = float(np.percentile(self.state.noise, 70)) if self.state.noise else 0.003
        except Exception:
            noise_floor = 0.003
        threshold = max(0.0065, noise_floor * 2.35)

        if rms > threshold:
            self.state.speech_seen = True
            self.state.last_speech_at = now
            self.ui.set_status("listening", "Listening", "Keep talking, or pause to finish.")

        if self.state.speech_seen and (now - self.state.last_speech_at) >= SILENCE_TO_FINISH_SECONDS and age >= 1.1:
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

        if self.cancelled:
            return
        if not frames:
            self.cancel("no audio frames")
            self.ui.close_smoothly()
            return

        self.ui.set_status("transcribing", "Transcribing", f"Whisper {MODEL_NAME} via {BACKEND}")
        threading.Thread(target=self._transcribe_worker, args=(frames, samplerate), daemon=True).start()

    def _transcribe_worker(self, frames, samplerate: int) -> None:
        wav_path = None
        try:
            import numpy as np
            import soundfile as sf

            audio = np.concatenate(frames).astype(np.float32)
            if audio.size < samplerate * 0.30:
                self.ui.invoke_cancel("too short")
                return

            fd, wav_path = tempfile.mkstemp(prefix="kdictate-", suffix=".wav")
            os.close(fd)
            sf.write(wav_path, audio, samplerate)
            if BACKEND == "whisper.cpp":
                text = transcribe_with_whisper_cpp(wav_path)
            else:
                model = ModelManager.load()
                segments, info = model.transcribe(
                    wav_path,
                    language=LANGUAGE,
                    task="transcribe",
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 450},
                    condition_on_previous_text=False,
                    temperature=0.0,
                    no_speech_threshold=0.35,
                    compression_ratio_threshold=2.4,
                )
                text = " ".join(seg.text.strip() for seg in segments).strip()
            if not text:
                self.ui.invoke_cancel("no speech recognized")
                return
            log(f"Transcribed {len(text)} chars: {text[:240]!r}")
            self.ui.invoke_transcribed(text)
        except Exception as exc:
            log(f"Transcription failed: {exc!r}\n{traceback.format_exc()}")
            self.ui.invoke_error(str(exc))
        finally:
            if wav_path:
                with contextlib.suppress(Exception):
                    os.unlink(wav_path)


class SocketServer:
    def __init__(self, handler: Callable[[str], str]) -> None:
        self.handler = handler
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        _ensure_dirs()
        with contextlib.suppress(FileNotFoundError):
            SOCKET_PATH.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCKET_PATH))
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

    app = Gtk.Application(application_id=APP_ID)

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
            self.last_frame = time.time()
            self.engine = DictationEngine(self)
            self.monitor = KeyboardMonitor(lambda: GLib.idle_add(self.invoke_cancel, "manual typing"))
            self.monitor.start()
            self.paster = ClipboardPaster(injector, self.monitor.pause_for)
            self.layer_enabled = False

            self.area = Gtk.DrawingArea()
            self.area.set_content_width(WINDOW_W)
            self.area.set_content_height(WINDOW_H)
            self.area.set_draw_func(self.draw)
            self.window.set_child(self.area)

            click = Gtk.GestureClick.new()
            click.connect("pressed", self.on_click)
            self.area.add_controller(click)

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

            GLib.timeout_add(33, self.animate)
            GLib.timeout_add(55, self.tick)

        def on_close_request(self, *args):
            self.invoke_cancel("closed")
            return True

        def on_click(self, gesture, n_press, x, y):
            if x >= WINDOW_W - 42 and y <= 42:
                self.invoke_cancel("clicked close")

        def tick(self):
            self.engine.tick()
            return True

        def animate(self):
            now = time.time()
            dt = max(0.001, min(0.09, now - self.last_frame))
            self.last_frame = now
            self.level_smooth += (self.level - self.level_smooth) * min(1.0, dt * 12.0)
            self.fade_alpha += (self.fade_target - self.fade_alpha) * min(1.0, dt * 16.0)
            if self.fade_target == 0.0 and self.fade_alpha < 0.03 and self.visible:
                self.window.hide()
                self.visible = False
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

            if caret:
                cx, cy = caret
                x = max(16, min(cx, sw - WINDOW_W - 16))
                y = max(16, min(cy, sh - WINDOW_H - 16))
            else:
                x = ox + max(16, int((sw - WINDOW_W) / 2))
                y = oy + max(16, int(sh * 0.18))

            if self.layer_enabled and LayerShell is not None:
                with contextlib.suppress(Exception):
                    LayerShell.set_margin(self.window, LayerShell.Edge.LEFT, int(x))
                    LayerShell.set_margin(self.window, LayerShell.Edge.TOP, int(y))
            else:
                # Wayland may ignore move(); this fallback is primarily for XWayland/X11.
                with contextlib.suppress(Exception):
                    self.window.move(int(x), int(y))

        def show_and_record(self):
            if self.engine.recording:
                self.invoke_cancel("toggle")
                return
            self.position()
            self.set_status("listening", "Listening", "Speak now. Pause to finish.")
            self.level = 0.0
            self.fade_alpha = 0.0
            self.fade_target = 1.0
            self.visible = True
            self.monitor.arm(ignore_for=0.8)
            self.window.present()
            self.engine.start()

        def close_smoothly(self):
            self.monitor.disarm()
            self.fade_target = 0.0

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

        def invoke_error(self, msg: str):
            GLib.idle_add(self.show_error, msg)

        def invoke_cancel(self, reason: str):
            GLib.idle_add(self._cancel_on_ui, reason)

        def _cancel_on_ui(self, reason: str):
            self.engine.cancel(reason)
            self.close_smoothly()
            return False

        def _on_transcribed(self, text: str):
            self.set_status("typing", "Typing", text[:68] + ("..." if len(text) > 68 else ""))
            ok, msg = self.paster.paste_text(text)
            if not ok:
                self.show_error(msg)
            else:
                self.close_smoothly()
            return False

        def show_error(self, msg: str):
            self.monitor.disarm()
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

        def draw(self, area, cr, width, height):
            # Outside the card stays transparent; the card itself is a clean solid dark material.
            a = max(0.0, min(1.0, self.fade_alpha))
            cr.save()
            cr.set_operator(cairo.OPERATOR_OVER)  # normal alpha compositing; do not punch transparent holes

            # Soft shadow only outside the card.
            cr.set_source_rgba(0, 0, 0, 0.28 * a)
            self.draw_round_rect(cr, 10, 14, width - 20, height - 20, 24)
            cr.fill()

            # One unified solid dark card. No separate transparent-looking islands.
            cr.set_source_rgba(0.045, 0.047, 0.060, 0.995 * a)
            self.draw_round_rect(cr, 8, 7, width - 16, height - 17, 24)
            cr.fill()

            # Subtle border/highlight.
            cr.set_source_rgba(1, 1, 1, 0.105 * a)
            self.draw_round_rect(cr, 8.5, 7.5, width - 17, height - 18, 24)
            cr.set_line_width(1)
            cr.stroke()

            # Close button: simple, visible, on the same card material.
            cr.set_source_rgba(1, 1, 1, 0.105 * a)
            cr.arc(width - 27, 27, 13, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.78 * a)
            cr.set_line_width(2)
            cr.move_to(width - 32, 22)
            cr.line_to(width - 22, 32)
            cr.move_to(width - 22, 22)
            cr.line_to(width - 32, 32)
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
            cr.set_source_rgba(1, 1, 1, 0.69 * a)
            cr.set_font_size(12.8)
            cr.move_to(96, 70)
            cr.show_text(self.subtitle)

            # Tiny level meter, directly on the solid card.
            if self.mode == "listening":
                x0, y0 = 96, 88
                x1 = width - 48
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

            cr.restore()

    overlay_ref: dict[str, Overlay] = {}

    def on_activate(_app):
        overlay_ref["overlay"] = Overlay(_app)
        ModelManager.warm_async()
        log("Daemon activated")

    app.connect("activate", on_activate)

    def wait_for_overlay() -> Overlay | None:
        deadline = time.time() + 3.0
        while time.time() < deadline:
            overlay = overlay_ref.get("overlay")
            if overlay is not None:
                return overlay
            time.sleep(0.05)
        return overlay_ref.get("overlay")

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


def doctor_text() -> str:
    lines: list[str] = []
    lines.append(f"KDictate app dir: {APP_DIR}")
    lines.append(f"Session: XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '')} XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '')}")
    lines.append(f"Backend: {BACKEND}")
    lines.append(f"Model: {MODEL_NAME} device={DEVICE} compute_type={COMPUTE_TYPE}")
    if BACKEND == "whisper.cpp":
        lines.append(f"whisper.cpp bin: {WHISPER_CPP_BIN}")
        lines.append(f"whisper.cpp model: {WHISPER_CPP_MODEL}")
    for cmd in ["wl-copy", "wl-paste", "nvidia-smi"]:
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

    if cmd == "quit":
        with contextlib.suppress(Exception):
            print(send_socket("quit"))
        return 0

    print("Usage: kdictate [toggle|start|cancel|daemon|status|doctor|warmup|warmup-foreground|quit]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv))
