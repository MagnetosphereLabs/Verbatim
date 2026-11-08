#!/usr/bin/env python3
"""Preserve bare-Super actions without firing them for chords on older X11 desktops.

Cinnamon/Muffin already distinguishes a modifier tap from a native shortcut.
Older Xfce and MATE menu handlers need an adapter: X RECORD observes presses
without grabbing the keyboard; only an otherwise unused Super release invokes
the original action. No typed text is retained or logged.
"""
from __future__ import annotations

import ast
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

UNIT = "verbatim-super-key.service"
KEYS = {"Super_L", "Super_R"}


def desktop() -> str:
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session == "wayland" or (session != "x11" and os.environ.get("WAYLAND_DISPLAY")) or not os.environ.get("DISPLAY"):
        return ""
    tokens = set(" ".join(os.environ.get(k, "") for k in
        ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "XDG_SESSION_DESKTOP")).upper().replace(":", " ").split())
    if tokens & {"COSMIC", "GNOME", "CINNAMON", "X-CINNAMON", "KDE", "PLASMA"}:
        return ""
    return "XFCE" if "XFCE" in tokens else "MATE" if "MATE" in tokens else ""


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)


def state_dir() -> Path:
    path = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "verbatim/super-key"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


class Settings:
    def get(self, item: dict) -> str | None:
        if item["backend"] == "xfconf":
            args = ["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", item["key"]]
        else:
            args = ["gsettings", "get", item["schema"], item["key"]]
        result = run(args)
        if result.returncode:
            # Removing a shortcut/applet later is a user edit, not a reason to
            # resurrect the old value. Distinguish deletion from an offline bus.
            if item["backend"] == "xfconf":
                listing = run(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-l"])
                if listing.returncode == 0 and item["key"] not in listing.stdout.splitlines():
                    return None
            else:
                listing = run(["gsettings", "list-schemas"])
                if listing.returncode == 0 and item["schema"] not in listing.stdout.splitlines():
                    return None
            raise RuntimeError(result.stderr.strip() or "Could not read menu shortcut")
        return result.stdout.rstrip("\n")

    def set(self, item: dict, value: str) -> None:
        if item["backend"] == "xfconf":
            args = ["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", item["key"], "-s", value]
        else:
            args = ["gsettings", "set", item["schema"], item["key"], value]
        result = run(args)
        if result.returncode or self.get(item) != value:
            raise RuntimeError(result.stderr.strip() or "Could not change menu shortcut")


class Journal:
    def __init__(self, path: Path, settings: Settings):
        self.path, self.settings = path, settings

    def restore(self) -> None:
        pending = []
        for item in read_json(self.path, []):
            try:
                # A later edit in Keyboard settings belongs to the user.
                if self.settings.get(item) == item["applied"]:
                    self.settings.set(item, item["original"])
            except Exception:
                pending.append(item)
        if pending:
            save(self.path, pending)
            raise RuntimeError("Menu shortcut restoration is pending; recovery will retry at next start")
        self.path.unlink(missing_ok=True)

    def apply(self, items: list[dict]) -> None:
        # Write ahead of settings changes, including partial setup and power loss.
        save(self.path, items)
        try:
            for item in items:
                if self.settings.get(item) != item["original"]:
                    raise RuntimeError("Menu shortcut changed during setup")
                self.settings.set(item, item["applied"])
        except Exception:
            self.restore()
            raise


class SuperTap:
    """A Super tap must begin and end with no other keys or buttons in use."""
    def __init__(self, super_codes: set[int], held=(), buttons=()):
        self.super_codes = super_codes
        self.held, self.buttons = set(held), set(buttons)
        self.candidate = None

    def event(self, kind: str, code: int) -> int | None:
        if kind == "key-down":
            if code in self.held:  # Ignore repeated presses.
                return None
            self.candidate = code if code in self.super_codes and not self.held and not self.buttons else None
            self.held.add(code)
        elif kind == "key-up":
            tapped = code if code == self.candidate and self.held == {code} and not self.buttons else None
            self.held.discard(code)
            self.candidate = None
            return tapped
        elif kind == "button-down":
            self.buttons.add(code)
            self.candidate = None
        elif kind == "button-up":
            self.buttons.discard(code)
            self.candidate = None
        return None


def menu_items(kind: str, settings: Settings, spare_key) -> list[dict]:
    items = []
    if kind == "XFCE":
        result = run(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-l"])
        if result.returncode:
            raise RuntimeError("Could not inspect Xfce shortcuts")
        paths = set(result.stdout.splitlines())
        disabled_command = None
        for key in sorted(KEYS):
            path = "/commands/custom/" + key
            if path in paths:
                item = {"backend": "xfconf", "key": path, "super": key}
                value = settings.get(item)
                if value:
                    # Xfce rejects an empty shortcut command with a dialog.
                    # A valid no-op masks defaults; SuperTap runs the saved menu.
                    if disabled_command is None:
                        executable = shutil.which("true", path=os.defpath)
                        if not executable:
                            raise RuntimeError("No silent command available; original menu shortcuts retained")
                        disabled_command = shlex.join([str(Path(executable).resolve())])
                    items.append(dict(item, original=value, applied=disabled_command, command=value))
    elif kind == "MATE":
        result = run(["gsettings", "list-schemas"])
        if result.returncode:
            raise RuntimeError("Could not inspect MATE shortcuts")
        schemas = set(result.stdout.splitlines())
        for schema, key in (("com.linuxmint.mintmenu", "hot-key"),
                            ("org.mate.Marco.global-keybindings", "panel-main-menu")):
            if schema not in schemas:
                continue
            item = {"backend": "gsettings", "schema": schema, "key": key}
            value = settings.get(item)
            bare = ast.literal_eval(value)
            if bare in KEYS:
                binding, codes = spare_key()
                items.append(dict(item, original=value, applied=repr(binding), super=bare, codes=codes))
    return items


def has_dictation_shortcut(kind: str) -> bool:
    from register_desktop_shortcut import owns_super_v
    if kind == "XFCE":
        value = run(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", "/commands/custom/<Super>v"])
        return value.returncode == 0 and any(name in value.stdout.lower() for name in ("kdictate", "verbatim"))
    root = "/org/mate/desktop/keybindings/"
    listed = run(["dconf", "list", root])
    if listed.returncode:
        return False
    for entry in listed.stdout.splitlines():
        if not entry.endswith("/"):
            continue
        schema = "org.mate.control-center.keybinding:" + root + entry
        binding = run(["gsettings", "get", schema, "binding"])
        if binding.returncode == 0 and owns_super_v(binding.stdout):
            action = run(["gsettings", "get", schema, "action"])
            if action.returncode == 0 and any(name in action.stdout.lower() for name in ("kdictate", "verbatim")):
                return True
    return False


def relay_is_claimed(control, keycode: int, mintmenu: bool) -> bool:
    from Xlib import X, error
    root = control.screen().root

    def claimed(window):
        catcher = error.CatchError()
        window.grab_key(keycode, 0, False, X.GrabModeAsync, X.GrabModeAsync, onerror=catcher)
        control.sync()
        window.ungrab_key(keycode, 0, onerror=error.CatchError())
        control.sync()
        return isinstance(catcher.get_error(), error.BadAccess)

    if claimed(root):
        return True
    if mintmenu:
        # mintMenu moves its grab from the root onto its menu window while
        # open (keybinding.GlobalKeyBinding.set_focus_window). Preserve the
        # second Super tap that closes it, without targeting an unrelated app.
        try:
            window = control.get_input_focus().focus
            for _ in range(16):
                if not hasattr(window, "query_tree") or window.id == root.id:
                    break
                classes = " ".join(window.get_wm_class() or ()).lower()
                if "mintmenu" in classes and claimed(window):
                    return True
                window = window.query_tree().parent
        except error.XError:
            pass  # The menu can close while its ancestry is being inspected.
    return False


def notify_ready() -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect("\0" + address[1:] if address.startswith("@") else address)
            sock.sendall(b"READY=1")


def start_ticks(pid: int) -> str:
    # Parse after the final ')' because the process name can contain spaces.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def active_pid(directory: Path) -> int | None:
    info = read_json(directory / "process.json", {})
    try:
        pid = int(info["pid"])
        return pid if pid > 1 and start_ticks(pid) == info["start"] else None
    except (KeyError, ValueError, OSError):
        return None


def worker() -> None:
    kind = desktop()
    if not kind:
        notify_ready()
        return
    directory = state_dir()
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        settings = Settings()
        journal = Journal(directory / "settings.json", settings)
        journal.restore()
        if not has_dictation_shortcut(kind):
            notify_ready()
            return
        info = {"pid": os.getpid(), "start": start_ticks(os.getpid()), "ready": False}
        save(directory / "process.json", info)
        def stop_signal(_signum, _frame):
            # Leave the blocking RECORD read through Python's normal unwinding;
            # do not issue a reentrant Xlib request from a signal handler.
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop_signal)
        signal.signal(signal.SIGINT, stop_signal)
        control = recorder = None
        context = None
        try:
            # Lazy import: COSMIC and every Wayland session never load Xlib.
            from Xlib import X, XK, display, error
            from Xlib.ext import record, xtest
            from Xlib.protocol import rq

            control, recorder = display.Display(), display.Display()
            if not recorder.has_extension("RECORD") or not control.has_extension("XTEST"):
                raise RuntimeError("X11 RECORD and XTEST are required; original menu shortcuts retained")
            code = lambda name: control.keysym_to_keycode(XK.string_to_keysym(name))
            super_codes = {name: code(name) for name in KEYS}
            root = control.screen().root
            used = set()

            def spare_key():
                # Extra function keys have their own keycodes in the usual
                # evdev map. No synthetic modifiers or VT-switch chords.
                for number in range(24, 12, -1):
                    symbol = XK.string_to_keysym(f"F{number}")
                    target = next((key for key, level in control.keysym_to_keycodes(symbol) if level == 0), 0)
                    if not target or target in used:
                        continue
                    # Test the real server grab table, including shortcuts owned
                    # by other apps. Include all lock/modifier combinations.
                    catcher = error.CatchError(error.BadAccess)
                    root.grab_key(target, X.AnyModifier, False, X.GrabModeAsync, X.GrabModeAsync, onerror=catcher)
                    control.sync()
                    root.ungrab_key(target, X.AnyModifier)
                    control.sync()
                    if catcher.get_error() is None:
                        used.add(target)
                        return f"F{number}", [target]
                raise RuntimeError("No free menu relay shortcut; original shortcut retained")

            items = menu_items(kind, settings, spare_key)
            if not items:
                notify_ready()
                return
            if any(not super_codes[item["super"]] for item in items):
                raise RuntimeError("Super key is absent from X11 keyboard map")

            def keys_down():
                bitmap = control.query_keymap()
                return {i for i in range(256) if bitmap[i // 8] & (1 << (i % 8))}

            def buttons_down():
                mask = root.query_pointer().mask
                return {i for i in range(1, 6) if mask & (1 << (7 + i))}

            taps = SuperTap(set(super_codes.values()), keys_down(), buttons_down())

            def activate(keycode):
                if keys_down() or buttons_down():
                    return
                for item in items:
                    if super_codes[item["super"]] != keycode or settings.get(item) != item["applied"]:
                        continue
                    if "command" in item:
                        subprocess.Popen(shlex.split(item["command"]), start_new_session=True,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        # An installed schema does not mean its applet is
                        # running. Never send an unhandled relay to the app
                        # that currently has focus.
                        if not relay_is_claimed(control, item["codes"][-1], item["schema"] == "com.linuxmint.mintmenu"):
                            continue
                        # Invoke the original applet's own toggle, preserving its
                        # menu, placement and theme, without a synthetic Super.
                        for relay_code in item["codes"]:
                            xtest.fake_input(control, X.KeyPress, relay_code)
                        for relay_code in reversed(item["codes"]):
                            xtest.fake_input(control, X.KeyRelease, relay_code)
                        control.flush()

            event_names = {X.KeyPress: "key-down", X.KeyRelease: "key-up",
                           X.ButtonPress: "button-down", X.ButtonRelease: "button-up"}

            def callback(reply):
                if reply.category == record.StartOfData:
                    # The observer must be live before touching menu shortcuts.
                    journal.apply(items)
                    info["ready"] = True
                    save(directory / "process.json", info)
                    notify_ready()
                elif reply.category == record.FromServer and not reply.client_swapped:
                    data = reply.data
                    while data:
                        event, data = rq.EventField(None).parse_binary_value(data, recorder.display, None, None)
                        name = event_names.get(event.type)
                        if name:
                            # Core autorepeat may contain artificial releases.
                            if event.type == X.KeyRelease and event.detail in taps.super_codes and event.detail in keys_down():
                                continue
                            tapped = taps.event(name, event.detail)
                            if tapped is not None:
                                activate(tapped)

            context = recorder.record_create_context(0, [record.AllClients], [{
                "core_requests": (0, 0), "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0), "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0), "device_events": (X.KeyPress, X.ButtonRelease),
                "errors": (0, 0), "client_started": False, "client_died": False,
            }])
            recorder.record_enable_context(context, callback)
        finally:
            try:
                journal.restore()
            finally:
                (directory / "process.json").unlink(missing_ok=True)
                for connection in (recorder, control):
                    if connection is not None:
                        # A signal can interrupt Xlib with its I/O lock held.
                        # Closing the underlying socket avoids a flush deadlock
                        # and releases the RECORD context on the server.
                        connection.display.socket.close()


def restore() -> None:
    directory = state_dir()
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Journal(directory / "settings.json", Settings()).restore()


def unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user" / UNIT


def stop() -> None:
    path = unit_path()
    if path.exists():
        try:
            subprocess.run(["systemctl", "--user", "stop", UNIT], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=15)
        except FileNotFoundError:
            pass
    directory = state_dir()
    pid = active_pid(directory)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if active_pid(directory) != pid:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("Super-key observer did not stop; settings were not overwritten")
    restore()


def unit_arg(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$").replace("\n", "\\n").replace("\r", "\\r") + '"'


def start() -> None:
    if not desktop():
        return
    stop()
    command = [sys.executable, str(Path(__file__).resolve())]
    try:
        has_systemd = run(["systemctl", "--user", "show-environment"]).returncode == 0
    except FileNotFoundError:
        has_systemd = False
    if has_systemd:
        path = unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        executable = " ".join(unit_arg(arg) for arg in command)
        # A lingering user manager may still carry another desktop's tokens.
        # Set/clear these for this service without editing other services' env.
        session_environment = "".join("Environment=" + unit_arg(name + "=" + os.environ.get(name, "")) + "\n"
            for name in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "XDG_SESSION_TYPE", "WAYLAND_DISPLAY"))
        path.write_text("[Unit]\nDescription=Verbatim Super-key compatibility\n"
                        "PartOf=graphical-session.target\nStartLimitIntervalSec=60\nStartLimitBurst=3\n"
                        "[Service]\nType=notify\nNotifyAccess=main\n"
                        + session_environment +
                        f"ExecStart={executable} --run\nExecStopPost={executable} --restore\n"
                        "Restart=on-failure\nRestartSec=2\nTimeoutStartSec=15\nTimeoutStopSec=10\n")
        run(["systemctl", "--user", "daemon-reload"])
        # Inherit this session, including custom state/config locations.
        names = [name for name in ("DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
            "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "DBUS_SESSION_BUS_ADDRESS", "XDG_STATE_HOME", "XDG_CONFIG_HOME") if name in os.environ]
        run(["systemctl", "--user", "import-environment", *names])
        result = subprocess.run(["systemctl", "--user", "restart", UNIT], timeout=20)
        if result.returncode:
            raise RuntimeError("Super-key compatibility failed to start; original menu shortcuts retained")
    else:
        directory = state_dir()
        with (directory / "guard.log").open("a") as log:
            child = subprocess.Popen(command + ["--run"], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True)
        for _ in range(150):
            info = read_json(directory / "process.json", {})
            if info.get("pid") == child.pid and info.get("ready"):
                return
            if child.poll() is not None:
                if child.returncode:
                    raise RuntimeError("Super-key compatibility failed; see " + str(directory / "guard.log"))
                return
            time.sleep(0.1)
        child.terminate()
        raise RuntimeError("Super-key compatibility startup timed out")


def main() -> int:
    os.umask(0o077)
    try:
        mode = sys.argv[1] if len(sys.argv) == 2 else ""
        if mode == "--run":
            worker()
        elif mode == "--restore":
            restore()
        elif mode == "--start":
            start()
        elif mode == "--stop":
            stop()
            path = unit_path()
            if path.exists():
                path.unlink()
                run(["systemctl", "--user", "daemon-reload"])
        else:
            raise ValueError("Expected --start, --stop, --run or --restore")
        return 0
    except Exception as exc:
        print(f"Verbatim Super-key compatibility: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
