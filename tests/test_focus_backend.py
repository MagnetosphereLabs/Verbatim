"""Exercise real Wayland client messages against a small local protocol server."""
from pathlib import Path
import dataclasses
import socket
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from focus_backend import WaylandFocus, Window, X11Focus


def string(value):
    data = value.encode() + b"\0"
    return struct.pack("=I", len(data)) + data + b"\0" * (-len(data) % 4)


class ConnectedSocket:
    def __init__(self, sock):
        self.sock = sock

    def connect(self, _path):
        pass

    def __getattr__(self, name):
        return getattr(self.sock, name)


class Compositor:
    browser, game = 0xff000010, 0xff000011

    def __init__(self, profile):
        self.profile = profile
        self.client, self.server = socket.socketpair()
        self.lock = threading.Lock()
        self.requests = []
        self.refuse = False
        self.bound = {}
        self.errors = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def send(self, obj, opcode, data=b""):
        packet = struct.pack("=II", obj, ((len(data) + 8) << 16) | opcode) + data
        # Fragment every event to test stream framing, not just complete reads.
        with self.lock:
            self.server.sendall(packet[:5])
            self.server.sendall(packet[5:])

    def state(self, handle, active, minimized=False):
        values = ([1] if minimized else []) + ([2] if active else [])
        payload = struct.pack("=I", 4 * len(values)) + b"".join(struct.pack("=I", value) for value in values)
        self.send(handle, 8 if self.profile == "cosmic" else 4, payload)
        self.send(handle, 1 if self.profile == "cosmic" else 5)

    def top(self, manager, handle, app_id, active):
        self.send(manager, 0, struct.pack("=I", handle))
        self.send(handle, 2 if self.profile == "cosmic" else 0, string(app_id))
        self.send(handle, 3 if self.profile == "cosmic" else 1, string(app_id))
        self.state(handle, active)

    def event(self, obj, opcode, data):
        self.requests.append((obj, opcode, data))
        if obj == 1 and opcode == 1:
            registry, = struct.unpack("=I", data)
            names = [(1, "wl_seat", 7)]
            if self.profile == "cosmic":
                names += [(2, "zcosmic_toplevel_info_v1", 3), (3, "zcosmic_toplevel_manager_v1", 4)]
            elif self.profile == "wlr":
                names += [(2, "zwlr_foreign_toplevel_manager_v1", 3)]
            for name, interface, version in names:
                self.send(registry, 0, struct.pack("=I", name) + string(interface) + struct.pack("=I", version))
        elif obj == 1 and opcode == 0:
            callback, = struct.unpack("=I", data)
            self.send(callback, 0, struct.pack("=I", 1))
            self.send(1, 1, struct.pack("=I", callback))
        elif obj == 2:
            name, size = struct.unpack_from("=II", data)
            interface = data[8:7 + size].decode()
            version, new_id = struct.unpack_from("=II", data, 8 + ((size + 3) & ~3))
            assert version == 1
            self.bound[interface] = new_id
            if "toplevel_info" in interface or "zwlr_foreign" in interface:
                self.top(new_id, self.browser, "firefox", True)
                self.top(new_id, self.game, "steam_app_438100", False)
            if "zcosmic_toplevel_manager" in interface:
                self.send(new_id, 0, struct.pack("=III", 8, 2, 4))
        else:
            cosmic = self.profile == "cosmic" and obj == self.bound.get("zcosmic_toplevel_manager_v1") and opcode == 2
            wlr = self.profile == "wlr" and obj in (self.browser, self.game) and opcode == 4
            if (cosmic or wlr) and not self.refuse:
                handle = struct.unpack_from("=I", data)[0] if cosmic else obj
                self.state(self.browser, handle == self.browser)
                self.state(self.game, handle == self.game)

    def run(self):
        buffer = b""
        try:
            while True:
                data = self.server.recv(65536)
                if not data:
                    return
                buffer += data
                while len(buffer) >= 8:
                    obj, word = struct.unpack_from("=II", buffer)
                    size, opcode = word >> 16, word & 0xffff
                    if len(buffer) < size:
                        break
                    self.event(obj, opcode, buffer[8:size])
                    buffer = buffer[size:]
        except OSError:
            pass
        except Exception as exc:
            self.errors.append(exc)


class ProtocolTests(unittest.TestCase):
    def connect(self, profile):
        compositor = Compositor(profile)
        backend = WaylandFocus()
        compositor.thread.start()
        with patch("focus_backend.socket.socket", return_value=ConnectedSocket(compositor.client)), \
                patch.dict("os.environ", {"XDG_RUNTIME_DIR": "/not-used", "WAYLAND_DISPLAY": "test-display"}):
            backend.start()
            self.assertTrue(backend.ready.wait(2), backend.status)
        def cleanup():
            backend.close()
            backend.thread.join(1)
            compositor.server.close()
            compositor.thread.join(1)
            self.assertFalse(compositor.errors)

        self.addCleanup(cleanup)
        return backend, compositor

    def test_cosmic_inventory_and_confirmed_activation(self):
        backend, compositor = self.connect("cosmic")
        self.assertTrue(backend.supported, backend.status)
        self.assertEqual(backend.kind, "cosmic")
        game = backend.snapshot()[str(compositor.game)]
        self.assertTrue(game.game)
        self.assertTrue(backend.activate(game))
        self.assertTrue(backend.is_active(game))

    def test_wlr_inventory_and_confirmed_activation(self):
        backend, compositor = self.connect("wlr")
        self.assertTrue(backend.supported, backend.status)
        game = backend.snapshot()[str(compositor.game)]
        self.assertTrue(backend.activate(game))
        self.assertTrue(backend.is_active(game))

    def test_ignored_activation_is_reported_as_failure(self):
        backend, compositor = self.connect("cosmic")
        compositor.refuse = True
        game = backend.snapshot()[str(compositor.game)]
        self.assertFalse(backend.activate(game, timeout=0.08))

    def test_closed_toplevel_is_removed(self):
        backend, compositor = self.connect("wlr")
        compositor.send(compositor.game, 6)
        deadline = time.monotonic() + 1
        while str(compositor.game) in backend.snapshot() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertNotIn(str(compositor.game), backend.snapshot())

    def test_missing_management_protocol_is_a_safe_failure(self):
        backend, _compositor = self.connect("unsupported")
        self.assertFalse(backend.supported)
        self.assertEqual(backend.snapshot(), {})

    def test_unrelated_global_removal_keeps_inventory(self):
        backend, compositor = self.connect("cosmic")
        compositor.send(2, 0, struct.pack("=I", 99) + string("wl_output") + struct.pack("=I", 3))
        compositor.send(2, 1, struct.pack("=I", 99))
        self.assertTrue(backend.activate(backend.snapshot()[str(compositor.game)]))

    def test_x11_rechecks_actual_focus_before_paste(self):
        backend = X11Focus()
        window = Window("42", "vrchat", pid=1)
        backend.windows = {"42": dataclasses.replace(window, active=True)}
        backend.supported = True
        with patch.object(backend, "command", return_value="99"):
            self.assertFalse(backend.is_active(window))

    def test_new_wayland_activation_does_not_leave_two_cached_focus_targets(self):
        backend, compositor = self.connect("cosmic")
        # A compositor may announce the new focus before clearing the old one.
        compositor.state(compositor.game, True)
        deadline = time.monotonic() + 1
        while not backend.snapshot()[str(compositor.game)].active and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(backend.snapshot()[str(compositor.browser)].active)

    def test_x11_can_focus_game_when_desktop_has_no_active_window(self):
        backend = X11Focus()
        game = Window("42", "steam_app_438100", minimized=True)

        def command(*args):
            if args[:3] == ("xprop", "-root", "_NET_CLIENT_LIST"):
                return "_NET_CLIENT_LIST(WINDOW): window id # 0x2a"
            if args[:2] == ("xdotool", "getmouselocation"):
                return "X=-100\nY=20\n"
            return ""

        with patch.object(backend, "command", side_effect=command), patch.object(backend, "_window", return_value=game):
            backend._poll()
        self.assertTrue(backend.supported)
        self.assertTrue(backend.pointer_observed)
        self.assertIn("42", backend.snapshot())

    def test_x11_checks_window_identity_not_just_numeric_id(self):
        backend = X11Focus()
        window = Window("42", "vrchat", pid=1)
        with patch.object(backend, "command", return_value="42"), \
                patch.object(backend, "_window", return_value=Window("42", "firefox", pid=2)):
            self.assertFalse(backend.is_active(window))


if __name__ == "__main__":
    unittest.main()
