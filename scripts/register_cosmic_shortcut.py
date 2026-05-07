#!/usr/bin/env python3
"""Register/remove the COSMIC custom shortcut for KDictate.

COSMIC stores shortcut overrides in a RON-ish map. The installer writes the
same format COSMIC itself writes, without depending on private Rust libraries.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

DESC = "KDictate - Whisper Dictation"
CONFIG = Path.home() / ".config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom"


def escape_ron_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def split_top_level_entries(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]

    entries: list[str] = []
    start = 0
    depth = 0
    in_str = False
    escape = False

    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            piece = text[start:i].strip()
            if piece:
                entries.append(piece)
            start = i + 1

    tail = text[start:].strip()
    if tail:
        entries.append(tail)
    return entries


def is_super_v_key(key_part: str) -> bool:
    key_m = re.search(r'key\s*:\s*"([^"]+)"', key_part, re.IGNORECASE)
    if not key_m or key_m.group(1).lower() != "v":
        return False
    mods_m = re.search(r'modifiers\s*:\s*\[([^\]]*)\]', key_part, re.IGNORECASE | re.DOTALL)
    if not mods_m:
        return False
    mods = [m.strip().strip(",") for m in mods_m.group(1).replace("\n", " ").split()]
    mods = [m for m in mods if m]
    return set(mods) == {"Super"}


def split_key_value(entry: str) -> tuple[str, str]:
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(entry):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        elif ch == ":" and depth == 0:
            return entry[:i].strip(), entry[i + 1 :].strip()
    return entry, ""


def is_kdictate_entry(entry: str) -> bool:
    key, val = split_key_value(entry)
    low = entry.lower()
    return "kdictate" in low or (is_super_v_key(key) and "spawn" in val.lower())


def backup(path: Path) -> None:
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(path.name + f".bak-{stamp}"))


def read_entries() -> list[str]:
    if not CONFIG.exists():
        return []
    return split_top_level_entries(CONFIG.read_text(encoding="utf-8"))


def write_entries(entries: list[str]) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    body = ""
    if entries:
        body = "\n" + "\n".join(f"    {entry}," for entry in entries) + "\n"
    CONFIG.write_text("{" + body + "}\n", encoding="utf-8")


def install(command: str) -> None:
    entries = read_entries()
    new_entries = [e for e in entries if not is_kdictate_entry(e)]
    entry = (
        '( modifiers: [ Super, ], key: "v", description: Some("'
        + escape_ron_string(DESC)
        + '"), ): Spawn("'
        + escape_ron_string(command)
        + '")'
    )
    new_entries.insert(0, entry)
    if entries != new_entries:
        backup(CONFIG)
    write_entries(new_entries)
    print(f"Registered COSMIC shortcut: Super+V -> {command}")


def remove() -> None:
    entries = read_entries()
    new_entries = [e for e in entries if "kdictate" not in e.lower()]
    if entries != new_entries:
        backup(CONFIG)
    write_entries(new_entries)
    print("Removed KDictate COSMIC shortcut entry")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--remove":
        remove()
        return 0
    command = " ".join(sys.argv[1:]).strip() or str(Path.home() / ".local/bin/kdictate toggle")
    install(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
