#!/usr/bin/env bash
set -euo pipefail

PURGE_SYSTEM=0

for arg in "${@:-}"; do
  case "$arg" in
    --purge-system)
      PURGE_SYSTEM=1
      ;;
    -h|--help)
      echo "Usage: uninstall.sh [--purge-system]"
      echo
      echo "Removes Verbatim/KDictate user files, services, autostart entries, and shortcuts."
      echo "--purge-system also removes the uinput udev/module files created by the installer."
      exit 0
      ;;
  esac
done

if [ "$(id -u)" -eq 0 ]; then
  echo "Do not run this uninstall script with sudo."
  echo
  echo "Use:"
  echo "  curl -fsSL https://raw.githubusercontent.com/MagnetosphereLabs/Verbatim/main/uninstall.sh | bash"
  echo
  echo "Reason: Verbatim is installed as a per-user app with a user service and user shortcut."
  echo "Use --purge-system if you also want to remove the optional system udev/module files."
  exit 1
fi

echo "Verbatim uninstaller"
echo

APP_DIRS=(
  "$HOME/.local/share/verbatim"
  "$HOME/.local/share/kdictate-cosmic"
)

BIN_FILES=(
  "$HOME/.local/bin/verbatim"
  "$HOME/.local/bin/kdictate"
  "$HOME/.local/bin/verbatim-wayvr"
)

SERVICE_NAMES=(
  "verbatim.service"
  "kdictate.service"
)

SERVICE_FILES=(
  "$HOME/.config/systemd/user/verbatim.service"
  "$HOME/.config/systemd/user/kdictate.service"
)

AUTOSTART_FILES=(
  "$HOME/.config/autostart/verbatim.desktop"
  "$HOME/.config/autostart/kdictate-cosmic.desktop"
)

echo "Stopping running daemons..."

for bin in "${BIN_FILES[@]}"; do
  if [ -x "$bin" ]; then
    "$bin" quit >/dev/null 2>&1 || true
  fi
done

if command -v systemctl >/dev/null 2>&1; then
  for svc in "${SERVICE_NAMES[@]}"; do
    systemctl --user disable --now "$svc" >/dev/null 2>&1 || true
  done
fi

pkill -f '/kdictate.py daemon' >/dev/null 2>&1 || true
pkill -f '/verbatim.py daemon' >/dev/null 2>&1 || true
pkill -f 'kdictate-cosmic/app/kdictate.py daemon' >/dev/null 2>&1 || true
pkill -f 'verbatim/app/kdictate.py daemon' >/dev/null 2>&1 || true

echo "Removing desktop shortcuts..."

for app in "${APP_DIRS[@]}"; do
  if [ -x "$app/venv/bin/python" ] && [ -f "$app/scripts/register_desktop_shortcut.py" ]; then
    "$app/venv/bin/python" "$app/scripts/register_desktop_shortcut.py" --remove >/dev/null 2>&1 || true
  elif [ -x "$app/venv/bin/python" ] && [ -f "$app/scripts/register_cosmic_shortcut.py" ]; then
    "$app/venv/bin/python" "$app/scripts/register_cosmic_shortcut.py" --remove >/dev/null 2>&1 || true
  fi
done

echo "Removing WayVR integration..."

WAYVR_WATCH="$HOME/.config/wayvr/theme/gui/watch.xml"
WAYVR_KEYBOARD="$HOME/.config/wayvr/theme/gui/keyboard.xml"
WAYVR_MIC_ICON="$HOME/.config/wayvr/theme/verbatim-mic.svg"
WAYVR_BACKUP="$HOME/.config/wayvr/verbatim-backups"

mkdir -p "$WAYVR_BACKUP" 2>/dev/null || true

python3 - "$WAYVR_WATCH" "$WAYVR_KEYBOARD" "$WAYVR_BACKUP" <<'PY' || true
from pathlib import Path
import re
import shutil
import sys
import time

watch = Path(sys.argv[1])
keyboard = Path(sys.argv[2])
backup = Path(sys.argv[3])
stamp = time.strftime("%Y%m%d-%H%M%S")

default_watch_grid = '''          <!-- Four buttons -->
          <div flex_direction="column" gap="8">
            <div gap="8">
              <Button id="btn_keyboard" macro="button_style" _press="::OverlayToggle kbd" tooltip="EDIT_MODE.KEYBOARD" tooltip_side="left">
                <sprite src_builtin="watch/keyboard.svg" width="40" height="40" />
              </Button>
              <Button id="btn_edit_mode" macro="button_style" _press="::EditToggle" tooltip="WATCH.EDIT_MODE" tooltip_side="left">
                <sprite color="~color_text" width="40" height="40" src="watch/edit.svg" />
              </Button>
            </div>
            <div gap="8">
              <Button macro="button_style" _press="::PlayspaceRecenter" tooltip="WATCH.RECENTER" tooltip_side="left">
                <sprite width="40" height="40" color="~color_text" src="watch/recenter.svg" />
              </Button>
              <Button macro="button_style" _press="::PlayspaceFixFloor" tooltip="WATCH.FIX_FLOOR" tooltip_side="left">
                <sprite width="40" height="40" color="~color_text" src="watch/fix-floor.svg" />
              </Button>
            </div>
          </div>'''

if watch.exists():
    text = watch.read_text(encoding="utf-8")
    if "btn_verbatim_dictation" in text:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(watch, backup / f"watch.xml.before-verbatim-remove.{stamp}")

        text = re.sub(
            r'''          <!-- Four buttons -->\s*
          <div flex_direction="column" gap="8">[\s\S]*?          </div>\s*
        </rectangle>''',
            default_watch_grid + "\n        </rectangle>",
            text,
            count=1,
        )

        watch.write_text(text, encoding="utf-8")

if keyboard.exists():
    text = keyboard.read_text(encoding="utf-8")
    if "btn_verbatim_dictation" in text:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(keyboard, backup / f"keyboard.xml.before-verbatim-remove.{stamp}")

        text = re.sub(
            r'\s*<Button[^>]*id="btn_verbatim_dictation"[\s\S]*?</Button>\s*<VerticalSeparator\s*/>\s*',
            "\n",
            text,
            count=1,
        )

        text = re.sub(
            r'\s*<Button[^>]*id="btn_verbatim_dictation"[\s\S]*?</Button>\s*',
            "\n",
            text,
            count=1,
        )

        keyboard.write_text(text, encoding="utf-8")
PY

rm -f "$WAYVR_MIC_ICON"

echo "Removing user services and autostart entries..."

for file in "${SERVICE_FILES[@]}"; do
  rm -f "$file"
done

for file in "${AUTOSTART_FILES[@]}"; do
  rm -f "$file"
done

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  systemctl --user reset-failed >/dev/null 2>&1 || true
fi

echo "Removing commands..."

for bin in "${BIN_FILES[@]}"; do
  rm -f "$bin"
done

echo "Removing application files..."

for app in "${APP_DIRS[@]}"; do
  rm -rf "$app"
done

rm -f "$HOME/scan-whisper-dictation.sh"

if [ "$PURGE_SYSTEM" -eq 1 ]; then
  echo "Removing optional system uinput configuration..."
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required for --purge-system, but sudo was not found." >&2
  else
    sudo rm -f \
      /etc/udev/rules.d/80-verbatim-uinput.rules \
      /etc/udev/rules.d/80-kdictate-uinput.rules \
      /etc/modules-load.d/verbatim-uinput.conf \
      /etc/modules-load.d/kdictate-uinput.conf

    sudo udevadm control --reload-rules >/dev/null 2>&1 || true
    sudo udevadm trigger >/dev/null 2>&1 || true
  fi
else
  echo
  echo "Left optional system uinput files in place, if present."
  echo "They are harmless and may be used by other Wayland automation tools."
  echo
  echo "To remove them too, run:"
  echo "  curl -fsSL https://raw.githubusercontent.com/MagnetosphereLabs/Verbatim/main/uninstall.sh | bash -s -- --purge-system"
fi

echo
echo "Verbatim removed."
echo "Legacy KDictate Cosmic files were also removed if present."
