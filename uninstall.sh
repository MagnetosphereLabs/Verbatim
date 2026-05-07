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

echo "Removing COSMIC shortcuts..."

for app in "${APP_DIRS[@]}"; do
  if [ -x "$app/venv/bin/python" ] && [ -f "$app/scripts/register_cosmic_shortcut.py" ]; then
    "$app/venv/bin/python" "$app/scripts/register_cosmic_shortcut.py" --remove >/dev/null 2>&1 || true
  fi
done

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
