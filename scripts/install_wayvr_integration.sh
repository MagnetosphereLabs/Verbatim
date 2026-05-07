#!/usr/bin/env bash
set -euo pipefail

WAYVR_CONFIG="$HOME/.config/wayvr"
WAYVR_GUI="$WAYVR_CONFIG/theme/gui"
BACKUP_DIR="$WAYVR_CONFIG/verbatim-backups"

WATCH_XML="$WAYVR_GUI/watch.xml"
KEYBOARD_XML="$WAYVR_GUI/keyboard.xml"

VERBATIM_HELPER="$HOME/.local/bin/verbatim-wayvr"
VERBATIM_ACTION="::ShellExec $VERBATIM_HELPER toggle"

WATCH_UPSTREAM="https://raw.githubusercontent.com/wayvr-org/wayvr/main/wayvr/src/assets/gui/watch.xml"
KEYBOARD_UPSTREAM="https://raw.githubusercontent.com/wayvr-org/wayvr/main/wayvr/src/assets/gui/keyboard.xml"

mkdir -p "$WAYVR_GUI" "$BACKUP_DIR"

if [ ! -x "$VERBATIM_HELPER" ]; then
  echo "Verbatim WayVR helper is missing: $VERBATIM_HELPER"
  exit 1
fi

fetch_default_if_missing() {
  local target="$1"
  local url="$2"
  local name="$3"

  if [ ! -f "$target" ]; then
    echo "Installing default WayVR $name XML into $target"
    curl -fsSL "$url" -o "$target"
  fi
}

backup_file() {
  local file="$1"
  local name="$2"

  if [ -f "$file" ]; then
    cp "$file" "$BACKUP_DIR/${name}.bak.$(date +%Y%m%d-%H%M%S)"
  fi
}

fetch_default_if_missing "$WATCH_XML" "$WATCH_UPSTREAM" "watch"
fetch_default_if_missing "$KEYBOARD_XML" "$KEYBOARD_UPSTREAM" "keyboard"

backup_file "$WATCH_XML" "watch.xml"
backup_file "$KEYBOARD_XML" "keyboard.xml"

python3 - "$WATCH_XML" "$KEYBOARD_XML" "$VERBATIM_ACTION" <<'PY'
from pathlib import Path
import re
import sys

watch_path = Path(sys.argv[1])
keyboard_path = Path(sys.argv[2])
action = sys.argv[3]

watch = watch_path.read_text(encoding="utf-8")
keyboard = keyboard_path.read_text(encoding="utf-8")

# ---------------------------------------------------------------------
# WATCH INTEGRATION
#
# Upstream watch.xml has a symmetrical 2x2 block:
#   top-left:    btn_keyboard
#   top-right:   btn_edit_mode
#   bottom-left: PlayspaceRecenter
#   bottom-right: PlayspaceFixFloor
#
# To keep symmetry and place dictation next to the keyboard controls, replace
# the lower right floor fix button. This preserves the 2x2 geometry and avoids
# creating an ugly fifth button.
# ---------------------------------------------------------------------

watch_button = f'''              <Button id="btn_verbatim_dictation" macro="button_style" _press="{action}" tooltip_str="Verbatim Dictation" tooltip_side="left">
                <label text="🎙" color="~color_text" size="34" weight="bold" align="center" />
              </Button>'''

# Remove previous Verbatim button if present.
watch = re.sub(
    r'\s*<Button[^>]*id="btn_verbatim_dictation"[\s\S]*?</Button>\s*',
    "\n",
    watch,
    count=1,
)

floor_button_pattern = re.compile(
    r'''              <Button\s+macro="button_style"\s+_press="::PlayspaceFixFloor"\s+tooltip="WATCH\.FIX_FLOOR"\s+tooltip_side="left">\s*
                <sprite\s+width="40"\s+height="40"\s+color="~color_text"\s+src="watch/fix-floor\.svg"\s*/>\s*
              </Button>''',
    re.MULTILINE,
)

if floor_button_pattern.search(watch):
    watch = floor_button_pattern.sub(watch_button, watch, count=1)
else:
    raise SystemExit("Could not find WayVR watch fix-floor button to replace safely.")

watch_path.write_text(watch, encoding="utf-8")

# ---------------------------------------------------------------------
# KEYBOARD INTEGRATION
#
# Upstream keyboard.xml has a top tray with:
#   btn_dashboard, panels_root, apps_root, tray_root
#
# The safest non invasive spot is inside tray_root before the burger/menu
# button. This adds a mic shortcut to the keyboard without touching
# generated keycaps.
# ---------------------------------------------------------------------

keyboard_button = f'''          <Button id="btn_verbatim_dictation" macro="button_style" _press="{action}" tooltip_str="Verbatim Dictation">
            <label text="🎙" color="~color_text" size="34" weight="bold" align="center" />
          </Button>

          <VerticalSeparator />'''

keyboard = re.sub(
    r'\s*<Button[^>]*id="btn_verbatim_dictation"[\s\S]*?</Button>\s*<VerticalSeparator\s*/>\s*',
    "\n",
    keyboard,
    count=1,
)

burger_needle = '''          <Button macro="button_style" _press="::ContextMenuOpen menu_burger">
            <sprite width="38" height="38" color="~color_text" src_builtin="keyboard/burger.svg" />
          </Button>'''

if burger_needle in keyboard:
    keyboard = keyboard.replace(burger_needle, keyboard_button + "\n" + burger_needle, 1)
else:
    raise SystemExit("Could not find WayVR keyboard burger/menu button insertion point.")

keyboard_path.write_text(keyboard, encoding="utf-8")

print(f"Patched {watch_path}")
print(f"Patched {keyboard_path}")
PY

echo
echo "WayVR Verbatim integration installed."
echo "Installed:"
echo "  $WATCH_XML"
echo "  $KEYBOARD_XML"
echo
echo "Restart WayVR to reload the custom watch/keyboard UI."
echo "The new mic button calls:"
echo "  $VERBATIM_ACTION"
