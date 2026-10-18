"""Run with: python3 -m unittest discover -s tests -v"""
from pathlib import Path
import dataclasses
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from focus_backend import Window, normal_app
from vr_target import DesktopActivity, VrTargetRouter


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds=1.0):
        self.now += seconds


class Activity:
    pointer_available = True
    status = "pointer/keyboard activity"

    def __init__(self):
        self.last = 0.0

    def timestamp(self):
        return self.last


class Backend:
    supported = True
    pointer_activity = 0.0
    pointer_observed = True
    status = "test window control"

    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()
        self.windows = {
            "browser": Window("browser", "firefox", title="VRChat documentation", active=True, pid=10),
            "game": Window("game", "steam_app_438100", pid=20),
        }
        self.requests = []
        self.refuse = False

    def snapshot(self):
        return dict(self.windows)

    def activate(self, window):
        self.requests.append(("activate", window.key))
        if self.refuse:
            return False
        self.select(window.key)
        return True

    def select(self, key):
        self.windows = {k: dataclasses.replace(w, active=k == key, minimized=False if k == key else w.minimized)
                        for k, w in self.windows.items()}

    def is_active(self, window):
        current = self.windows.get(window.key)
        return bool(current and current.identity == window.identity and current.active)

    def minimize(self, window):
        self.requests.append(("minimize", window.key))


class Timer:
    pending = []

    def __init__(self, delay, function):
        self.function = function
        self.pending.append(self)

    def start(self):
        pass


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.activity, self.backend = Clock(), Activity(), Backend()
        self.router = VrTargetRouter(self.backend, self.activity, clock=self.clock)
        self.env = patch.dict("os.environ", {"KDICTATE_VR_AUTO_TARGET": "1", "KDICTATE_VR_IDLE_SECONDS": "3"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.router.observe()  # Initial desktop focus is a baseline, not activity.

    def capture(self, source="vr"):
        self.router.capture(source)
        self.clock.advance()

    def test_desktop_hotkey_never_auto_targets_game(self):
        self.capture("desktop")
        self.assertIsNone(self.router.prepare())
        self.assertEqual(self.backend.requests, [])

    def test_idle_browser_routes_to_vrchat(self):
        self.capture()
        lease = self.router.prepare()
        self.assertTrue(lease.game)
        self.assertEqual(lease.window.key, "game")
        self.assertTrue(lease.ready())
        self.assertEqual(self.router.focus_activity, 0.0)

    def test_recent_pointer_activity_preserves_browser(self):
        self.activity.last = self.clock() - 1
        self.capture()
        lease = self.router.prepare()
        self.assertEqual(lease.window.key, "browser")
        self.assertFalse(lease.activated)

    def test_pointer_activity_while_speaking_preserves_browser(self):
        self.capture()
        self.activity.last = self.clock()
        self.clock.advance(10)
        self.assertEqual(self.router.prepare().window.key, "browser")

    def test_window_switch_while_speaking_preserves_selected_app(self):
        self.backend.select("game")
        self.router.observe()
        self.clock.advance(10)
        self.capture()
        self.backend.select("browser")
        self.assertEqual(self.router.prepare().window.key, "browser")

    def test_recent_window_switch_is_activity_without_mouse(self):
        self.backend.select("game")
        self.router.observe()
        self.clock.advance()
        self.backend.select("browser")
        self.router.observe()
        self.capture()
        self.assertEqual(self.router.prepare().window.key, "browser")

    def test_mouse_activity_after_activation_aborts_paste(self):
        self.capture()
        lease = self.router.prepare()
        self.clock.advance()
        self.activity.last = self.clock()
        self.assertFalse(lease.ready())

    def test_window_switch_after_activation_aborts_paste(self):
        self.capture()
        lease = self.router.prepare()
        self.clock.advance()
        self.backend.select("browser")
        self.assertFalse(lease.ready())

    def test_desktop_mouse_movement_does_not_abort_desktop_delivery(self):
        self.activity.last = self.clock()
        self.capture()
        lease = self.router.prepare()
        self.clock.advance()
        self.activity.last = self.clock()
        self.assertTrue(lease.ready())

    def test_refused_activation_does_not_allow_paste(self):
        self.capture()
        self.backend.refuse = True
        lease = self.router.prepare()
        self.assertFalse(lease.ready())
        self.assertIn("could not be focused", lease.error)

    def test_missing_pointer_access_does_not_guess_idle(self):
        self.capture()
        self.activity.pointer_available = self.backend.pointer_observed = False
        lease = self.router.prepare()
        self.assertFalse(lease.ready())
        self.assertEqual(self.backend.requests, [])

    def test_already_focused_game_needs_no_activation(self):
        self.backend.select("game")
        self.router.last_window = self.backend.windows["game"]
        self.capture()
        self.activity.pointer_available = self.backend.pointer_observed = False
        self.assertTrue(self.router.prepare().ready())
        self.assertEqual(self.backend.requests, [])

    def test_no_game_keeps_desktop_delivery(self):
        del self.backend.windows["game"]
        self.capture()
        self.assertEqual(self.router.prepare().window.key, "browser")

    def test_ambiguous_game_does_not_guess(self):
        self.backend.windows["second"] = Window("second", "VRChat.exe", pid=30)
        self.capture()
        lease = self.router.prepare()
        self.assertFalse(lease.ready())
        self.assertIn("ambiguous", lease.error)

    def test_game_disappearing_aborts(self):
        self.capture()
        lease = self.router.prepare()
        del self.backend.windows["game"]
        self.assertFalse(lease.ready())

    def test_reused_window_identifier_does_not_receive_paste(self):
        self.capture()
        lease = self.router.prepare()
        self.backend.windows["game"] = Window("game", "firefox", active=True, pid=99)
        self.assertFalse(lease.ready())

    def test_new_dictation_invalidates_old_lease(self):
        self.capture()
        lease = self.router.prepare()
        self.router.capture("desktop")
        self.assertFalse(lease.ready())

    def test_transient_own_overlay_does_not_count_as_desktop_intent(self):
        self.backend.windows["own"] = Window("own", "dev.kdictate.Cosmic", normal=False)
        self.capture()
        self.backend.select("own")
        self.router.observe()
        self.assertEqual(self.router.prepare().window.key, "game")

    def test_disabled_auto_target_uses_existing_path(self):
        self.capture()
        with patch.dict("os.environ", {"KDICTATE_VR_AUTO_TARGET": "0"}):
            self.assertIsNone(self.router.prepare())

    def test_unsupported_compositor_does_not_paste_into_idle_browser(self):
        self.capture()
        self.backend.supported = False
        lease = self.router.prepare()
        self.assertFalse(lease.ready())

    def test_unsupported_compositor_keeps_active_desktop_path(self):
        self.activity.last = self.clock()
        self.capture()
        self.backend.supported = False
        self.assertIsNone(self.router.prepare())

    def finish(self, lease):
        Timer.pending = []
        with patch("vr_target.threading.Timer", Timer):
            lease.finish()
        self.assertEqual(len(Timer.pending), 1)
        return Timer.pending[0].function

    def test_restores_previous_focus_and_original_minimized_state(self):
        self.backend.windows["game"] = dataclasses.replace(self.backend.windows["game"], minimized=True)
        self.capture()
        callback = self.finish(self.router.prepare())
        callback()
        self.assertEqual(self.backend.requests, [("activate", "game"), ("activate", "browser"), ("minimize", "game")])

    def test_does_not_minimize_a_previously_visible_game(self):
        self.capture()
        callback = self.finish(self.router.prepare())
        callback()
        self.assertNotIn(("minimize", "game"), self.backend.requests)

    def test_restore_does_not_undo_a_later_selection(self):
        self.capture()
        callback = self.finish(self.router.prepare())
        self.clock.advance()
        self.backend.select("browser")
        callback()
        self.assertEqual(self.backend.requests, [("activate", "game")])

    def test_restore_does_not_interrupt_new_dictation(self):
        self.capture()
        callback = self.finish(self.router.prepare())
        self.router.capture("desktop")
        callback()
        self.assertEqual(self.backend.requests, [("activate", "game")])

    def test_identity_matching_excludes_browser_titles(self):
        self.assertFalse(self.backend.windows["browser"].game)
        for name in ("VRChat.exe", "vrchat VRChat", "com.vrchat.VRChat", "steam_app_438100"):
            self.assertTrue(Window("a", name).game, name)
        for name in ("firefox", "fakevrchat", "steam_app_1234", "vrchat.example.com"):
            self.assertFalse(Window("a", name).game, name)

    def test_shell_and_own_x11_class_pairs_are_not_targets(self):
        self.assertFalse(normal_app("kdictate.py Kdictate.py"))
        self.assertFalse(normal_app("gnome-shell Gnome-shell"))
        self.assertTrue(normal_app("Navigator Firefox"))

    def test_only_own_injection_devices_are_ignored(self):
        self.assertTrue(DesktopActivity.own_device("KDictate Virtual Keyboard"))
        self.assertFalse(DesktopActivity.own_device("WayVR Mouse"))
        self.assertFalse(DesktopActivity.own_device("WayVR Keyboard"))


if __name__ == "__main__":
    unittest.main()
