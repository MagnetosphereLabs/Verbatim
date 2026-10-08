#!/usr/bin/env python3
from __future__ import annotations

import ast
import os
import shlex
import shutil
import tempfile
import subprocess
import sys
from pathlib import Path

NAME = "Verbatim - Voice Dictation"
OLD_NAME = "KDictate - Whisper Dictation"
DEFAULT_COMMAND = shlex.join([str(Path.home() / ".local/bin/kdictate"), "toggle"])
BINDING = "<Super>v"


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
        timeout=3.0,
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def parse_gvariant_list(value: str) -> list[str]:
    value = value.strip()
    if value in {"@as []", "[]", ""}:
        return []
    try:
        parsed = ast.literal_eval(value)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return []


def gsettings_get(schema: str, key: str) -> str:
    proc = run(["gsettings", "get", schema, key])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def gsettings_set(schema: str, key: str, value: str) -> bool:
    proc = run(["gsettings", "set", schema, key, value])
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return False
    return True


def gsettings_reloc_get(schema: str, path: str, key: str) -> str:
    proc = run(["gsettings", "get", f"{schema}:{path}", key])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def gsettings_reloc_set(schema: str, path: str, key: str, value: str) -> bool:
    proc = run(["gsettings", "set", f"{schema}:{path}", key, value])
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return False
    return True


def gvariant_string(value: str) -> str:
    return repr(value)


def gvariant_strv(values: list[str]) -> str:
    return "[" + ", ".join(repr(v) for v in values) + "]"


def owns_super_v(value: str) -> bool:
    values = parse_gvariant_list(value)
    if not values:
        values = [value.strip().strip("'\"")]
    return any(item.lower().replace(" ", "").replace("<mod4>", "<super>") == "<super>v"
               for item in values)


def builtin_super_v_conflict(namespace: str) -> str:
    # Inspect installed schemas instead of assuming this desktop version uses
    # particular keys. GNOME commonly reserves Super+V for notifications.
    schemas = run(["gsettings", "list-schemas"])
    if schemas.returncode:
        return ""
    for schema in schemas.stdout.splitlines():
        if not schema.startswith(namespace) or not any(token in schema for token in ("keybindings", "media-keys")):
            continue
        values = run(["gsettings", "list-recursively", schema])
        if values.returncode:
            continue
        for line in values.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and owns_super_v(parts[2]):
                return f"{parts[0]} {parts[1]}"
    return ""


def desktop_tokens() -> set[str]:
    raw = " ".join(
        [
            os.environ.get("XDG_CURRENT_DESKTOP", ""),
            os.environ.get("DESKTOP_SESSION", ""),
            os.environ.get("XDG_SESSION_DESKTOP", ""),
        ]
    )
    return {p.strip().upper() for p in raw.replace(":", " ").split() if p.strip()}


def register_cosmic(command: str, remove: bool) -> bool:
    script = Path(__file__).with_name("register_cosmic_shortcut.py")
    if not script.exists():
        return False

    if remove:
        proc = run([sys.executable, str(script), "--remove"])
    else:
        proc = run([sys.executable, str(script), command])

    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)

    return proc.returncode == 0


def gnome_slot_matches(path: str) -> bool:
    schema = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
    name = gsettings_reloc_get(schema, path, "name").strip("'")
    command = gsettings_reloc_get(schema, path, "command").strip("'")
    low = f"{name} {command}".lower()
    return "verbatim" in low or "kdictate" in low


def register_gnome(command: str, remove: bool) -> bool:
    if not command_exists("gsettings"):
        return False

    list_schema = "org.gnome.settings-daemon.plugins.media-keys"
    item_schema = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
    list_key = "custom-keybindings"

    current = parse_gvariant_list(gsettings_get(list_schema, list_key))
    changed = False

    matched = [p for p in current if gnome_slot_matches(p)]

    if remove:
        new_list = [p for p in current if p not in matched]
        if new_list != current:
            gsettings_set(list_schema, list_key, gvariant_strv(new_list))
            changed = True
        if changed:
            print("Removed GNOME shortcut entry for Verbatim")
        return True

    for other in current:
        if other not in matched and owns_super_v(gsettings_reloc_get(item_schema, other, "binding")):
            print("Super+V already belongs to another GNOME custom action; choose a free shortcut in Keyboard settings.")
            return False
    conflict = builtin_super_v_conflict("org.gnome.")
    if conflict:
        print(f"Super+V already belongs to {conflict}; choose a free shortcut in Keyboard settings.")
        return False

    if matched:
        path = matched[0]
        new_list = current
    else:
        used = set(current)
        path = ""
        for idx in range(0, 80):
            candidate = f"/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom{idx}/"
            if candidate not in used:
                path = candidate
                break
        if not path:
            print("Could not find a free GNOME custom shortcut slot", file=sys.stderr)
            return False
        new_list = current + [path]

    results = [gsettings_reloc_set(item_schema, path, "name", gvariant_string(NAME)),
        gsettings_reloc_set(item_schema, path, "command", gvariant_string(command)),
        gsettings_reloc_set(item_schema, path, "binding", gvariant_string(BINDING))]
    if not all(results) or not gsettings_set(list_schema, list_key, gvariant_strv(new_list)):
        return False

    print(f"Registered GNOME shortcut: Super+V -> {command}")
    return True


def dconf_read(path: str) -> str:
    proc = run(["dconf", "read", path])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def dconf_write(path: str, value: str) -> bool:
    proc = run(["dconf", "write", path, value])
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return False
    return True


def dconf_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return "'" + escaped + "'"


def cinnamon_slot_matches(slot: str) -> bool:
    base = f"/org/cinnamon/desktop/keybindings/custom-keybindings/{slot}"
    name = dconf_read(f"{base}/name").strip("'")
    command = dconf_read(f"{base}/command").strip("'")
    low = f"{name} {command}".lower()
    return "verbatim" in low or "kdictate" in low


def register_cinnamon(command: str, remove: bool) -> bool:
    if not command_exists("dconf"):
        return False

    list_path = "/org/cinnamon/desktop/keybindings/custom-list"
    current = parse_gvariant_list(dconf_read(list_path))

    matched = [slot for slot in current if cinnamon_slot_matches(slot)]

    if remove:
        new_list = [slot for slot in current if slot not in matched]
        if new_list != current:
            dconf_write(list_path, gvariant_strv(new_list))
            print("Removed Cinnamon shortcut entry for Verbatim")
        return True

    for other in current:
        if other not in matched and owns_super_v(dconf_read(f"/org/cinnamon/desktop/keybindings/custom-keybindings/{other}/binding")):
            print("Super+V already belongs to another Cinnamon action; choose a free shortcut in Keyboard settings.")
            return False
    if command_exists("gsettings"):
        conflict = builtin_super_v_conflict("org.cinnamon.")
        if conflict:
            print(f"Super+V already belongs to {conflict}; choose a free shortcut in Keyboard settings.")
            return False

    if matched:
        slot = matched[0]
        new_list = current
    else:
        used = set(current)
        slot = ""
        for idx in range(0, 80):
            candidate = f"custom{idx}"
            if candidate not in used:
                slot = candidate
                break
        if not slot:
            print("Could not find a free Cinnamon custom shortcut slot", file=sys.stderr)
            return False
        new_list = current + [slot]

    base = f"/org/cinnamon/desktop/keybindings/custom-keybindings/{slot}"
    if not all([dconf_write(f"{base}/name", dconf_string(NAME)),
        dconf_write(f"{base}/command", dconf_string(command)),
        dconf_write(f"{base}/binding", gvariant_strv([BINDING]))]):
        return False

    # An unchanged custom-list emits no notification. In particular, updating
    # an installed command/binding must remove and re-add our slot so Cinnamon
    # reloads the live grab immediately, without restarting the desktop.
    without_ours = [item for item in new_list if item != slot]
    if slot in current and not dconf_write(list_path, gvariant_strv(without_ours)):
        return False
    if not dconf_write(list_path, gvariant_strv(new_list)):
        dconf_write(list_path, gvariant_strv(current))
        return False

    print(f"Registered Cinnamon shortcut: Super+V -> {command}")
    return True


def register_xfce(command: str, remove: bool) -> bool:
    if not command_exists("xfconf-query"):
        return False
    path = "/commands/custom/<Super>v"
    existing = run(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", path])
    ours = "kdictate" in existing.stdout.lower() or "verbatim" in existing.stdout.lower()
    if remove:
        return not ours or run(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", path, "-r"]).returncode == 0
    if existing.returncode == 0 and existing.stdout.strip() and not ours:
        print("Super+V already belongs to another Xfce action; choose a free shortcut in Keyboard settings.")
        return False
    args = ["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", path, "-s", command]
    if existing.returncode:
        args += ["-n", "-t", "string"]
    return run(args).returncode == 0


def register_kde(command: str, remove: bool) -> bool:
    # Plasma's desktop-file components launch commands without a resident Qt
    # process. Register over D-Bus rather than rewriting kglobalshortcutsrc while
    # its daemon owns it. Includes both Plasma 5 and Plasma 6.
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        def call(method, value, signature=None):
            return bus.call_sync("org.kde.kglobalaccel", "/kglobalaccel",
                "org.kde.KGlobalAccel", method, value,
                GLib.VariantType.new(signature) if signature else None,
                Gio.DBusCallFlags.NONE, 2000, None)
        component = "verbatim-dictation.desktop"
        path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "kglobalaccel" / component
        if remove:
            call("unregister", GLib.Variant("(ss)", (component, "_launch")))
            path.unlink(missing_ok=True)
            return True
        # Qt::META | Qt::Key_V. Do not displace another application's shortcut.
        available = call("isGlobalShortcutAvailable", GLib.Variant("(is)", (0x10000056, component)), "(b)").unpack()[0]
        if not available:
            print("Super+V is already assigned in Plasma. Choose a free shortcut in System Settings.")
            return False
        executable = shlex.split(command)
        if not executable:
            return False
        def desktop_arg(value):
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "[Desktop Entry]\nType=Application\nName=" + NAME + "\nExec=" + " ".join(desktop_arg(x) for x in executable) + "\nX-KDE-GlobalAccel-CommandShortcut=true\nX-KDE-Shortcuts=Meta+V\nStartupNotify=false\nNoDisplay=true\n"
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
        action = [component, "_launch", NAME, NAME]
        call("doRegister", GLib.Variant("(as)", (action,)))
        call("getComponent", GLib.Variant("(s)", (component,)), "(o)")
        print(f"Registered Plasma shortcut: Super+V -> {command}")
        return True
    except Exception as exc:
        print(f"Plasma shortcut registration unavailable: {exc}", file=sys.stderr)
        return False


def main() -> int:
    remove = len(sys.argv) > 1 and sys.argv[1] == "--remove"
    command = " ".join(sys.argv[1:]).strip() if not remove else ""
    command = command or DEFAULT_COMMAND

    tokens = desktop_tokens()
    attempted: list[str] = []
    ok = False

    # Preserve COSMIC behavior exactly when COSMIC is detected.
    if "COSMIC" in tokens:
        attempted.append("COSMIC")
        ok = register_cosmic(command, remove) or ok

    # Ubuntu default GNOME.
    if "GNOME" in tokens or "UBUNTU" in tokens:
        attempted.append("GNOME")
        ok = register_gnome(command, remove) or ok

    # Linux Mint Cinnamon.
    if "CINNAMON" in tokens or "X-CINNAMON" in tokens:
        attempted.append("Cinnamon")
        ok = register_cinnamon(command, remove) or ok

    if "KDE" in tokens or "PLASMA" in tokens:
        attempted.append("Plasma")
        ok = register_kde(command, remove) or ok
    if "XFCE" in tokens:
        attempted.append("Xfce")
        ok = register_xfce(command, remove) or ok

    # If the environment is unclear, inspect installed schemas before writing.
    if not attempted and command_exists("gsettings"):
        schemas = set(run(["gsettings", "list-schemas"]).stdout.splitlines())
        if "org.gnome.settings-daemon.plugins.media-keys" in schemas:
            ok = register_gnome(command, remove)
        elif "org.cinnamon.desktop.keybindings" in schemas:
            ok = register_cinnamon(command, remove)

    if ok:
        return 0

    if remove:
        print("No supported desktop shortcut entry was removed.")
        return 0

    print()
    print("Could not auto-register Super+V on this desktop.")
    print("Create a custom keyboard shortcut manually:")
    print(f"  Name:    {NAME}")
    print(f"  Command: {command}")
    print("  Shortcut: Super+V")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
