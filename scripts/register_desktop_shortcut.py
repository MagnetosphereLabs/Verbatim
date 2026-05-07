#!/usr/bin/env python3
from __future__ import annotations

import ast
import os
import shlex
import subprocess
import sys
from pathlib import Path

NAME = "Verbatim - Voice Dictation"
OLD_NAME = "KDictate - Whisper Dictation"
DEFAULT_COMMAND = str(Path.home() / ".local/bin/kdictate toggle")
BINDING = "<Super>v"


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )


def command_exists(name: str) -> bool:
    return subprocess.run(["sh", "-lc", f"command -v {shlex.quote(name)} >/dev/null 2>&1"]).returncode == 0


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

    gsettings_reloc_set(item_schema, path, "name", gvariant_string(NAME))
    gsettings_reloc_set(item_schema, path, "command", gvariant_string(command))
    gsettings_reloc_set(item_schema, path, "binding", gvariant_string(BINDING))
    gsettings_set(list_schema, list_key, gvariant_strv(new_list))

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
    dconf_write(f"{base}/name", dconf_string(NAME))
    dconf_write(f"{base}/command", dconf_string(command))
    dconf_write(f"{base}/binding", gvariant_strv([BINDING]))

    # Force Cinnamon to notice changes by rewriting the list after item values.
    dconf_write(list_path, gvariant_strv([x for x in new_list if x != slot]))
    dconf_write(list_path, gvariant_strv(new_list))

    print(f"Registered Cinnamon shortcut: Super+V -> {command}")
    return True


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

    # If the environment is unclear, try safe known registrars in order.
    if not attempted:
        if register_gnome(command, remove):
            ok = True
            attempted.append("GNOME")
        elif register_cinnamon(command, remove):
            ok = True
            attempted.append("Cinnamon")
        elif register_cosmic(command, remove):
            ok = True
            attempted.append("COSMIC")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
