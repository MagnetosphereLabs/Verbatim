"""Choose a VRChat or desktop paste destination from actual user activity."""
from __future__ import annotations

import atexit
import contextlib
import os
import select
import threading
import time

from focus_backend import WaylandFocus, Window, X11Focus


def idle_seconds():
    try:
        return max(0.5, min(30.0, float(os.environ.get("KDICTATE_VR_IDLE_SECONDS", "3"))))
    except ValueError:
        return 3.0


class DesktopActivity:
    """Read activity timestamps only. Never record keys, buttons, or positions."""
    def __init__(self, log=lambda _message: None, clock=time.monotonic):
        self.log, self.clock = log, clock
        self.lock = threading.RLock()
        self.last_input = 0.0
        self.pointer_available = False
        self.status = "starting"
        self.stop = threading.Event()
        self.thread = None

    def timestamp(self):
        with self.lock:
            return self.last_input

    def mark(self):
        with self.lock:
            self.last_input = self.clock()

    @staticmethod
    def own_device(name):
        return name.lower().startswith(("kdictate virtual", "kdictate doctor", "verbatim virtual"))

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="VerbatimDesktopActivity", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()

    def _run(self):
        devices = {}
        try:
            from evdev import InputDevice, ecodes, list_devices
            scanned = -10.0
            while not self.stop.is_set():
                if self.clock() - scanned >= 3.0:
                    scanned = self.clock()
                    paths = set(list_devices())
                    for path in set(devices) - paths:
                        devices.pop(path)[0].close()
                    for path in paths - set(devices):
                        try:
                            dev = InputDevice(path)
                            caps = dev.capabilities(absinfo=False)
                            keys = set(caps.get(ecodes.EV_KEY, []))
                            pointer = ecodes.BTN_LEFT in keys or ecodes.BTN_TOOL_FINGER in keys
                            keyboard = ecodes.KEY_SPACE in keys or ecodes.KEY_A in keys
                            if self.own_device(dev.name or "") or not (pointer or keyboard):
                                dev.close()
                                continue
                            devices[path] = dev, pointer
                        except OSError:
                            continue
                    self.pointer_available = any(pointer for _, pointer in devices.values())
                    self.status = "pointer/keyboard activity" if self.pointer_available else "pointer device access unavailable"
                readers = [dev for dev, _ in devices.values()]
                if not readers:
                    self.stop.wait(0.2)
                    continue
                readable, _, _ = select.select(readers, [], [], 0.2)
                for dev in readable:
                    try:
                        pointer = devices[dev.path][1]
                        for event in dev.read():
                            movement = pointer and (event.type == ecodes.EV_ABS or
                                                    event.type == ecodes.EV_REL and event.value != 0)
                            press = event.type == ecodes.EV_KEY and event.value == 1
                            if movement or press:
                                self.mark()
                    except OSError:
                        devices.pop(dev.path, None)
                        dev.close()
                        self.pointer_available = any(pointer for _, pointer in devices.values())
        except Exception as exc:
            self.status = f"activity monitor unavailable: {exc}"
            self.log(self.status)
        finally:
            self.pointer_available = False
            for dev, _ in devices.values():
                with contextlib.suppress(OSError):
                    dev.close()


class DeliveryTarget:
    def __init__(self, router, window=None, previous=None, error="", game=False):
        self.router, self.window, self.previous = router, window, previous
        self.error, self.game = error, game
        self.generation = router.generation
        self.prepared = router.clock()
        self.activated = False
        self.focus_stamp = router.focus_activity

    @property
    def terminal(self):
        return bool(self.window and self.window.terminal)

    def ready(self):
        if self.error or self.generation != self.router.generation or not self.window:
            return False
        self.router.observe()
        current = self.router.backend.snapshot().get(self.window.key)
        if not current or current.identity != self.window.identity or not self.router.backend.is_active(self.window):
            self.error = "The dictation target changed. Your transcript is saved."
            return False
        if self.router.focus_activity > self.focus_stamp:
            self.error = "Another application was selected before paste. Your transcript is saved."
            return False
        if self.activated and self.router.input_time() > self.prepared:
            self.error = "Desktop activity resumed before paste. Your transcript is saved."
            return False
        return True

    def finish(self):
        if not self.activated or not self.window:
            return

        def restore():
            # Keep focus long enough for the game to process Ctrl+V. Do not
            # undo subsequent user interaction or a newer dictation session.
            self.router.observe()
            if (self.generation != self.router.generation or self.router.input_time() > self.prepared
                    or self.router.focus_activity > self.focus_stamp
                    or not self.router.backend.is_active(self.window)):
                return
            if self.previous and self.previous.identity != self.window.identity:
                self.router.activate(self.previous)
            if self.window.minimized:
                self.router.backend.minimize(self.window)

        timer = threading.Timer(0.65, restore)
        timer.daemon = True
        timer.start()


class VrTargetRouter:
    def __init__(self, backend, activity, log=lambda _message: None, clock=time.monotonic):
        self.backend, self.activity, self.log, self.clock = backend, activity, log, clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.source = "desktop"
        self.generation = 0
        self.started = 0.0
        self.recent_at_start = False
        self.focus_activity = 0.0
        self.last_window = None
        self.expected = None
        self.status = "desktop delivery"

    @classmethod
    def for_session(cls, log=lambda _message: None):
        wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland" or (
            os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_SESSION_TYPE") != "x11")
        return cls(WaylandFocus(log) if wayland else X11Focus(log), DesktopActivity(log), log)

    def start(self):
        if self.thread is not None:
            return
        self.backend.start()
        self.activity.start()
        self.thread = threading.Thread(target=self._watch, name="VerbatimTargetSelection", daemon=True)
        self.thread.start()
        atexit.register(self.close)

    def close(self):
        self.stop.set()
        self.backend.close()
        self.activity.close()

    def _watch(self):
        while not self.stop.wait(0.08):
            self.observe()

    def observe(self):
        current = next((w for w in self.backend.snapshot().values() if w.active and w.normal), None)
        if current is None:
            return
        with self.lock:
            if self.last_window and current.identity != self.last_window.identity:
                expected = self.expected
                if not expected or current.identity != expected[0] or self.clock() > expected[1]:
                    self.focus_activity = self.clock()
            self.last_window = current

    def input_time(self):
        return max(self.activity.timestamp(), self.backend.pointer_activity)

    def activity_time(self):
        return max(self.input_time(), self.focus_activity)

    def capture(self, source="desktop"):
        self.observe()
        with self.lock:
            self.generation += 1
            self.source = source
            self.started = self.clock()
            self.recent_at_start = self.activity_time() >= self.started - idle_seconds()
            self.status = "VR activity selection armed" if source == "vr" else "desktop delivery"

    def activate(self, window):
        with self.lock:
            self.expected = window.identity, self.clock() + 1.2
        confirmed = self.backend.activate(window)
        self.observe()
        return confirmed

    def prepare(self):
        """Return None for the unchanged desktop path, otherwise a checked lease."""
        if self.source != "vr" or os.environ.get("KDICTATE_VR_AUTO_TARGET", "1").lower() in {"0", "false", "off"}:
            return None
        self.backend.ready.wait(0.8)
        if hasattr(self.backend, "refresh"):
            self.backend.refresh()
        self.observe()
        desktop_active = self.recent_at_start or self.activity_time() >= self.started
        if not self.backend.supported:
            if desktop_active:
                self.status = "desktop activity; existing delivery backend"
                return None
            self.status = self.backend.status
            return DeliveryTarget(self, error="VR window control is unavailable on this desktop. Your transcript is saved.")

        windows = self.backend.snapshot()
        current = next((w for w in windows.values() if w.active and w.normal), None)
        games = [w for w in windows.values() if w.game and w.normal]
        if desktop_active or not games:
            if not current:
                return DeliveryTarget(self, error="Select the desktop application to receive dictation. Your transcript is saved.")
            self.status = f"desktop activity; target {current.app_id}"
            return DeliveryTarget(self, current)

        game = next((w for w in games if w.active), games[0] if len(games) == 1 else None)
        if game is None:
            return DeliveryTarget(self, error="Several VRChat windows are open; the target is ambiguous. Your transcript is saved.")
        if not game.active and not (self.activity.pointer_available or self.backend.pointer_observed):
            self.status = self.activity.status
            return DeliveryTarget(self, error="Mouse activity tracking is unavailable. Rerun the installer; your transcript is saved.")

        lease = DeliveryTarget(self, game, current, game=True)
        if not game.active:
            lease.activated = True
            if not self.activate(game):
                lease.error = "VRChat could not be focused. Your transcript is saved."
        self.status = f"idle desktop; target {game.app_id}"
        self.log(f"VR dictation: {self.status}")
        return lease

    def diagnostics(self):
        return (f"VR targeting: {self.status}\nWindow control: {self.backend.status}\n"
                f"Desktop activity: {self.activity.status}\nVR desktop idle threshold: {idle_seconds():g}s")
