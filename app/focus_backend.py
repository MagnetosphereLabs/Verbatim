"""Window inventory and confirmed activation for VR dictation.

Wayland uses only version-1 COSMIC/wlr toplevel requests over a private client
connection. No desktop extensions, Xwayland guesses, or added Python packages
are needed. Wire layouts are defined by the upstream protocols:
https://github.com/pop-os/cosmic-protocols/tree/main/unstable
https://github.com/swaywm/wlr-protocols/tree/master/unstable
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import threading
import time


@dataclasses.dataclass(frozen=True)
class Window:
    key: str
    app_id: str = ""
    title: str = ""
    active: bool = False
    minimized: bool = False
    pid: int = 0
    normal: bool = True

    @property
    def game(self):
        # Match application identity, never a browser tab mentioning VRChat.
        return any(token == "steam_app_438100" or
                   re.fullmatch(r"(?:[\w-]+\.)*vrchat(?:\.exe)?", token)
                   for token in re.findall(r"[\w.-]+", self.app_id.lower()))

    @property
    def terminal(self):
        return any(name in self.app_id.lower() for name in
                   ("terminal", "konsole", "alacritty", "kitty", "xterm", "tilix", "wezterm"))

    @property
    def identity(self):
        return self.key, self.app_id, self.pid


def normal_app(app_id):
    name = app_id.lower()
    return bool(name) and not (
        set(name.split()) & {"cinnamon", "mintmenu", "gnome-shell", "org.gnome.shell", "plasmashell",
                 "org.kde.plasmashell", "xfce4-panel", "cosmic-panel", "cosmic-comp",
                 "cosmic-launcher", "cosmic-app-library", "kdictate", "verbatim",
                 "dev.kdictate.cosmic", "kdictate.py", "wayvr"}
        or "xdg-desktop-portal" in name or name.startswith("org.freedesktop.impl.portal.")
    )


class WaylandFocus:
    """Small, bounded client for the fd-free part of toplevel management."""
    def __init__(self, log=lambda _message: None):
        self.log = log
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.windows = {}
        self.pending = {}
        self.globals = {}
        self.bound_globals = set()
        self.objects = {1: "display", 2: "registry"}
        self.next_id = 3
        self.callbacks = set()
        self.buffer = b""
        self.sock = None
        self.thread = None
        self.kind = ""
        self.manager = self.info = self.seat = 0
        self.capabilities = set()
        self.supported = False
        self.status = "starting"
        self.pointer_activity = 0.0
        self.pointer_observed = False

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="VerbatimWaylandFocus", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        with contextlib.suppress(OSError):
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)

    @staticmethod
    def string(value):
        data = value.encode() + b"\0"
        return struct.pack("=I", len(data)) + data + b"\0" * (-len(data) % 4)

    @staticmethod
    def read_string(data, offset=0):
        size, = struct.unpack_from("=I", data, offset)
        if size < 1 or offset + 4 + size > len(data):
            raise ValueError("invalid Wayland string")
        return data[offset + 4:offset + 3 + size].decode("utf-8", "replace"), offset + 4 + ((size + 3) & ~3)

    def _send(self, obj, opcode, data=b""):
        message = struct.pack("=II", obj, ((8 + len(data)) << 16) | opcode) + data
        with self.send_lock:
            self.sock.sendall(message)

    def _allocate(self, kind):
        obj = self.next_id
        self.next_id += 1
        self.objects[obj] = kind
        return obj

    def _bind(self, interface):
        name, _version = self.globals[interface]
        self.bound_globals.add(name)
        obj = self._allocate(interface)
        self._send(2, 0, struct.pack("=I", name) + self.string(interface) + struct.pack("=II", 1, obj))
        return obj

    def _sync(self):
        callback = self._allocate("callback")
        self._send(1, 0, struct.pack("=I", callback))
        deadline = time.monotonic() + 2.0
        while callback not in self.callbacks:
            if time.monotonic() > deadline:
                raise TimeoutError("Wayland focus initialization timed out")
            self._receive()
        self.callbacks.remove(callback)

    def _receive(self):
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return
        if not data:
            raise ConnectionError("Wayland focus connection closed")
        self.buffer += data
        while len(self.buffer) >= 8:
            obj, word = struct.unpack_from("=II", self.buffer)
            size, opcode = word >> 16, word & 0xffff
            if size < 8 or size % 4 or size > 65532:
                raise ValueError("invalid Wayland event size")
            if len(self.buffer) < size:
                break
            payload, self.buffer = self.buffer[8:size], self.buffer[size:]
            with self.lock:
                self._event(obj, opcode, payload)

    def _event(self, obj, opcode, data):
        kind = self.objects.get(obj)
        if kind == "display":
            if opcode == 0:
                raise RuntimeError("Wayland server rejected focus protocol request")
            if opcode == 1:
                deleted, = struct.unpack_from("=I", data)
                self.objects.pop(deleted, None)
        elif kind == "registry":
            name, = struct.unpack_from("=I", data)
            if opcode == 0:
                interface, offset = self.read_string(data, 4)
                version, = struct.unpack_from("=I", data, offset)
                self.globals[interface] = name, version
            elif opcode == 1 and name in self.bound_globals:
                raise ConnectionError("Wayland globals changed; restart Verbatim to reconnect")
        elif kind == "callback":
            self.callbacks.add(obj)
        elif obj == self.info:
            if opcode == 0:
                handle, = struct.unpack_from("=I", data)
                self.objects[handle] = "toplevel"
                self.pending[handle] = {}
            elif opcode == 1:
                raise ConnectionError("Wayland toplevel inventory stopped")
        elif obj == self.manager and self.kind == "cosmic":
            size, = struct.unpack_from("=I", data)
            self.capabilities = set(struct.unpack(f"={size // 4}I", data[4:4 + size]))
            if self.ready.is_set():
                self.supported = 2 in self.capabilities
                self.status = "cosmic window control" if self.supported else "window activation is not permitted"
        elif kind == "toplevel":
            top = self.pending[obj]
            title_op, app_op, state_op, done_op, closed_op = (
                (2, 3, 8, 1, 0) if self.kind == "cosmic" else (0, 1, 4, 5, 6)
            )
            if opcode in (title_op, app_op):
                value, _ = self.read_string(data)
                top["title" if opcode == title_op else "app_id"] = value
            elif opcode == state_op:
                size, = struct.unpack_from("=I", data)
                states = set(struct.unpack(f"={size // 4}I", data[4:4 + size]))
                top.update(active=2 in states, minimized=1 in states)
            elif opcode == done_op:
                app_id = top.get("app_id", "")
                if top.get("active"):
                    for handle, properties in self.pending.items():
                        if handle != obj:
                            properties["active"] = False
                    self.windows = {key: dataclasses.replace(window, active=False)
                                    for key, window in self.windows.items()}
                self.windows[str(obj)] = Window(str(obj), normal=normal_app(app_id), **top)
            elif opcode == closed_op:
                self.windows.pop(str(obj), None)
                self.pending.pop(obj, None)
                self._send(obj, 0 if self.kind == "cosmic" else 7)
                self.objects.pop(obj, None)

    def _run(self):
        try:
            display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
            path = display if display.startswith("/") else str(Path(os.environ["XDG_RUNTIME_DIR"]) / display)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(0.25)
            self.sock.connect(path)
            self._send(1, 1, struct.pack("=I", 2))  # wl_display.get_registry
            self._sync()
            if "wl_seat" not in self.globals:
                raise RuntimeError("Wayland compositor has no input seat")
            self.seat = self._bind("wl_seat")
            if all(name in self.globals for name in ("zcosmic_toplevel_info_v1", "zcosmic_toplevel_manager_v1")):
                self.kind = "cosmic"
                self.info = self._bind("zcosmic_toplevel_info_v1")
                self.manager = self._bind("zcosmic_toplevel_manager_v1")
            elif "zwlr_foreign_toplevel_manager_v1" in self.globals:
                self.kind = "wlr"
                self.info = self.manager = self._bind("zwlr_foreign_toplevel_manager_v1")
            else:
                raise RuntimeError("compositor does not expose COSMIC/wlr window control")
            self._sync()
            self.supported = self.kind != "cosmic" or 2 in self.capabilities
            self.status = f"{self.kind} window control" if self.supported else "window activation is not permitted"
            self.ready.set()
            while not self.stop.is_set():
                self._receive()
        except Exception as exc:
            self.status = str(exc)
            if not self.stop.is_set():
                self.log(f"VR window targeting unavailable: {exc}")
        finally:
            self.supported = False
            self.ready.set()
            with self.lock:
                self.windows.clear()
            if self.sock:
                self.sock.close()

    def snapshot(self):
        with self.lock:
            return dict(self.windows) if self.supported else {}

    def activate(self, window, timeout=0.8):
        if not self.supported or self.snapshot().get(window.key, Window("")).identity != window.identity:
            return False
        try:
            handle = int(window.key)
            if self.kind == "cosmic":
                if window.minimized and 4 in self.capabilities:
                    self._send(self.manager, 6, struct.pack("=I", handle))
                self._send(self.manager, 2, struct.pack("=II", handle, self.seat))
            else:
                if window.minimized:
                    self._send(handle, 3)
                self._send(handle, 4, struct.pack("=I", self.seat))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = self.snapshot().get(window.key)
                if current and current.identity == window.identity and current.active:
                    return True
                time.sleep(0.015)
        except (OSError, ValueError):
            pass
        return False

    def is_active(self, window):
        current = self.snapshot().get(window.key)
        return bool(current and current.identity == window.identity and current.active)

    def minimize(self, window):
        if not self.supported or (self.kind == "cosmic" and 4 not in self.capabilities):
            return
        with contextlib.suppress(OSError, ValueError):
            if self.snapshot().get(window.key, Window("")).identity == window.identity:
                if self.kind == "cosmic":
                    self._send(self.manager, 5, struct.pack("=I", int(window.key)))
                else:
                    self._send(int(window.key), 2)


class X11Focus:
    def __init__(self, log=lambda _message: None):
        self.log = log
        self.supported = False
        self.status = "starting"
        self.ready = threading.Event()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.windows = {}
        self.pointer_activity = 0.0
        self.pointer_observed = False
        self.pointer = None
        self.metadata = {}
        self.poll_lock = threading.Lock()

    @staticmethod
    def command(*args):
        try:
            result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=0.6, check=False)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _window(self, key, active=False):
        text = self.command("xprop", "-id", key, "WM_CLASS", "_NET_WM_PID", "_NET_WM_WINDOW_TYPE", "_NET_WM_STATE")
        classes = next((line for line in text.splitlines() if line.startswith("WM_CLASS")), "")
        app_id = " ".join(re.findall(r'"([^"\n]*)"', classes))
        pid = re.search(r"_NET_WM_PID[^=]*=\s*(\d+)", text)
        normal = normal_app(app_id) and not any("_NET_WM_WINDOW_TYPE_" + kind in text for kind in
                     ("DESKTOP", "DOCK", "MENU", "DROPDOWN_MENU", "POPUP_MENU", "TOOLTIP", "SPLASH", "NOTIFICATION"))
        return Window(key, app_id, active=active, minimized="_NET_WM_STATE_HIDDEN" in text,
                      pid=int(pid[1]) if pid else 0, normal=normal)

    def _poll(self, force=False):
        with self.poll_lock:
            self._poll_unlocked(force)

    def _poll_unlocked(self, force=False):
        active = self.command("xdotool", "getactivewindow")
        clients = self.command("xprop", "-root", "_NET_CLIENT_LIST")
        keys = [str(int(key, 16)) for key in re.findall(r"0x[0-9a-fA-F]+", clients)]
        if active.isdigit() and active not in keys:
            keys.append(active)
        windows = {}
        for key in keys:
            cached = self.metadata.get(key)
            if force or cached is None or time.monotonic() - cached[0] > 5.0:
                cached = time.monotonic(), self._window(key)
                self.metadata[key] = cached
            windows[key] = dataclasses.replace(cached[1], active=key == active)
        self.metadata = {key: self.metadata[key] for key in keys}
        pointer = self.command("xdotool", "getmouselocation", "--shell")
        xy = re.findall(r"^[XY]=(-?\d+)$", pointer, re.MULTILINE)
        with self.lock:
            self.supported = bool(keys and any(w.normal for w in windows.values()))
            self.status = "X11 window control" if self.supported else "X11 focus unavailable"
            self.windows = windows
            if len(xy) == 2:
                if self.pointer is not None and xy != self.pointer:
                    self.pointer_activity = time.monotonic()
                self.pointer, self.pointer_observed = xy, True

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="VerbatimX11Focus", daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                self._poll()
            except Exception as exc:
                self.supported = False
                self.status = f"X11 focus unavailable: {exc}"
            self.ready.set()
            self.stop.wait(0.35)

    def close(self):
        self.stop.set()

    def snapshot(self):
        with self.lock:
            return dict(self.windows) if self.supported else {}

    def activate(self, window, timeout=0.8):
        if self.snapshot().get(window.key, Window("")).identity != window.identity:
            return False
        self.command("xdotool", "windowactivate", "--sync", window.key)
        # Confirm the actual active window; an accepted request alone is insufficient.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.command("xdotool", "getactivewindow") == window.key:
                self._poll(force=True)
                return True
            time.sleep(0.025)
        return False

    def refresh(self):
        self._poll(force=True)

    def is_active(self, window):
        return (self.command("xdotool", "getactivewindow") == window.key
                and self._window(window.key).identity == window.identity)

    def minimize(self, window):
        if self.snapshot().get(window.key, Window("")).identity == window.identity:
            self.command("xdotool", "windowminimize", window.key)
