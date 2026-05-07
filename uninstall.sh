#!/usr/bin/env bash
set -euo pipefail

APP="$HOME/.local/share/kdictate-cosmic"
BIN="$HOME/.local/bin/kdictate"
SERVICE_FILE="$HOME/.config/systemd/user/kdictate.service"
DESKTOP_FILE="$HOME/.config/autostart/kdictate-cosmic.desktop"

if [ -x "$BIN" ]; then
  "$BIN" quit >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now kdictate.service >/dev/null 2>&1 || true
fi

if [ -x "$APP/venv/bin/python" ] && [ -f "$APP/scripts/register_cosmic_shortcut.py" ]; then
  "$APP/venv/bin/python" "$APP/scripts/register_cosmic_shortcut.py" --remove || true
fi

rm -f "$SERVICE_FILE" "$DESKTOP_FILE" "$BIN"
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

rm -rf "$APP"

echo "KDictate Cosmic removed."
echo "The input group membership and /etc udev/module files are left in place because they are harmless and may be used by other Wayland automation tools."
echo "To remove those too: sudo rm -f /etc/udev/rules.d/80-kdictate-uinput.rules /etc/modules-load.d/kdictate-uinput.conf"
