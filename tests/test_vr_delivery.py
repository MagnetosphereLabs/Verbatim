"""Test the application's actual paste code without GTK/audio dependencies."""
from pathlib import Path
import ast
import os
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from typing import Callable
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.published, self.injected, self.finished = [], [], []
        self.desktop = SimpleNamespace(context_generation=1, prefix_space=False, terminal=False, ready=lambda: True)
        self.target = SimpleNamespace(game=True, terminal=False, error="target changed", ready=lambda: True,
                                      finish=lambda: self.finished.append(True))
        self.ns = {
            "Callable": Callable, "threading": threading, "time": SimpleNamespace(sleep=lambda _: None),
            "VR_TARGET": SimpleNamespace(prepare=lambda: self.target), "DESKTOP_TARGET": self.desktop,
            "clipboard_backends": lambda: ["test"], "clipboard_publish": self.publish,
            "bounded_prefix_space": lambda _text: False, "log": lambda _message: None,
            "FAST_PASTE_RESTORE_CLIPBOARD": False, "FAST_PASTE_CONTEXT_PROBE": False,
            "FAST_PASTE_PRE_PASTE_DELAY": 0, "FAST_PASTE_OLD_CLIPBOARD_TIMEOUT": 0,
        }
        tree = ast.parse((ROOT / "app/kdictate.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ClipboardPaster")
        future = next(n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__")
        exec(compile(ast.Module(body=[future, node], type_ignores=[]), "kdictate.py", "exec"), self.ns)
        injector = SimpleNamespace(paste_shortcut=lambda **kwargs: self.injected.append(kwargs) or True)
        self.paster = self.ns["ClipboardPaster"](injector, lambda _seconds: None)

    def publish(self, backend, text):
        self.published.append((backend, text))
        return True, None, "published"

    def test_game_delivery_uses_clipboard_and_ctrl_v(self):
        ok, _ = self.paster.paste_text("search words")
        self.assertTrue(ok)
        self.assertEqual(self.published, [("test", "search words")])
        self.assertEqual(self.injected, [{"terminal": False}])
        self.assertEqual(self.finished, [True])

    def test_game_does_not_use_stale_desktop_spacing(self):
        self.desktop.prefix_space = True
        self.ns["bounded_prefix_space"] = lambda _text: self.fail("game queried desktop text context")
        self.assertTrue(self.paster.paste_text("search words")[0])
        self.assertEqual(self.published[0][1], "search words")

    def test_failed_activation_does_not_publish_or_inject(self):
        self.target.ready = lambda: False
        ok, message = self.paster.paste_text("search words")
        self.assertFalse(ok)
        self.assertEqual(message, "target changed")
        self.assertEqual(self.published, [])
        self.assertEqual(self.injected, [])
        self.assertEqual(self.finished, [True])

    def test_focus_change_between_publication_and_paste_does_not_inject(self):
        calls = iter([True, False])
        self.target.ready = lambda: next(calls)
        self.assertFalse(self.paster.paste_text("search words")[0])
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.injected, [])

    def test_cancel_before_prepare_does_not_focus_or_inject(self):
        self.ns["VR_TARGET"].prepare = lambda: self.fail("cancelled delivery prepared a target")
        self.assertFalse(self.paster.paste_text("text", valid=lambda: False)[0])
        self.assertEqual(self.injected, [])

    def test_cancel_during_activation_does_not_inject(self):
        state = {"valid": True}

        def prepare():
            state["valid"] = False
            return self.target

        self.ns["VR_TARGET"].prepare = prepare
        self.assertFalse(self.paster.paste_text("text", valid=lambda: state["valid"])[0])
        self.assertEqual(self.published, [])
        self.assertEqual(self.finished, [True])

    def test_new_session_after_clipboard_publication_does_not_inject(self):
        def publish(backend, text):
            self.desktop.context_generation += 1
            return True, None, "published"

        self.ns["clipboard_publish"] = publish
        self.assertFalse(self.paster.paste_text("text")[0])
        self.assertEqual(self.injected, [])

    def test_desktop_hotkey_uses_original_target(self):
        self.ns["VR_TARGET"].prepare = lambda: None
        self.desktop.terminal = True
        self.assertTrue(self.paster.paste_text("text")[0])
        self.assertEqual(self.injected, [{"terminal": True}])
        self.assertEqual(self.finished, [])

    def test_vr_desktop_target_keeps_context_and_terminal_shortcut(self):
        self.target.game = False
        self.target.terminal = True
        self.ns["bounded_prefix_space"] = lambda _text: True
        self.assertTrue(self.paster.paste_text("text")[0])
        self.assertEqual(self.published, [("test", " text")])
        self.assertEqual(self.injected, [{"terminal": True}])

    def test_backend_error_is_reported_and_transcript_retained(self):
        def prepare():
            raise ConnectionError("compositor disconnected")

        self.ns["VR_TARGET"].prepare = prepare
        ok, message = self.paster.paste_text("text")
        self.assertFalse(ok)
        self.assertIn("last-transcript", message)
        self.assertEqual(self.injected, [])

    def test_watch_helper_marks_only_start_commands_as_vr(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            folder = Path(temporary)
            executable = folder / "fake kdictate"
            trace = folder / "calls"
            executable.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$VERBATIM_TEST_TRACE"\n')
            executable.chmod(0o755)
            env = {**os.environ, "KDICTATE_BIN": str(executable), "VERBATIM_TEST_TRACE": str(trace)}
            for command in ("toggle", "start", "cancel", "status"):
                result = subprocess.run(["bash", str(ROOT / "scripts/verbatim-wayvr"), command],
                                        env=env, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(trace.read_text().splitlines(), ["vr-toggle", "vr-start", "cancel", "status"])


if __name__ == "__main__":
    unittest.main()
