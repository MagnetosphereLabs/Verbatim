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

MIC_ICON="$WAYVR_CONFIG/theme/verbatim-mic.svg"

cat > "$MIC_ICON" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <path fill="currentColor" d="M32 6c-7.2 0-13 5.8-13 13v14c0 7.2 5.8 13 13 13s13-5.8 13-13V19C45 11.8 39.2 6 32 6Zm0 6c3.9 0 7 3.1 7 7v14c0 3.9-3.1 7-7 7s-7-3.1-7-7V19c0-3.9 3.1-7 7-7Z"/>
  <path fill="currentColor" d="M14 30a3 3 0 0 1 6 0v3c0 6.6 5.4 12 12 12s12-5.4 12-12v-3a3 3 0 0 1 6 0v3c0 8.8-6.4 16.1-15 17.6V56h8a3 3 0 0 1 0 6H21a3 3 0 0 1 0-6h8v-5.4C20.4 49.1 14 41.8 14 33v-3Z"/>
</svg>
SVG

python3 - "$WATCH_XML" "$KEYBOARD_XML" "$VERBATIM_ACTION" "$MIC_ICON" <<'PY'
from pathlib import Path
import re
import sys

watch_path = Path(sys.argv[1])
keyboard_path = Path(sys.argv[2])
action = sys.argv[3]
mic_icon = sys.argv[4]

watch = watch_path.read_text(encoding="utf-8")
keyboard = keyboard_path.read_text(encoding="utf-8")

# ---------------------------------------------------------------------
# WATCH INTEGRATION
#
# Desired symmetrical 2x2 watch grid:
#   top-left:     Keyboard
#   top-right:    Verbatim Mic
#   bottom-left:  Recenter
#   bottom-right: Edit Mode
#
# We remove Fix Floor from the quick grid because dictation is more useful
# beside the keyboard button, and adding a fifth button would break symmetry.
# ---------------------------------------------------------------------

watch_button_grid = f'''          <!-- Four buttons -->
          <div flex_direction="column" gap="8">
            <div gap="8">
              <Button id="btn_keyboard" macro="button_style" _press="::OverlayToggle kbd" tooltip="EDIT_MODE.KEYBOARD" tooltip_side="left">
                <sprite src_builtin="watch/keyboard.svg" width="40" height="40" />
              </Button>
              <Button id="btn_verbatim_dictation" macro="button_style" _press="{action}" tooltip_str="Verbatim Dictation" tooltip_side="left">
                <sprite width="40" height="40" color="~color_text" src_ext="{mic_icon}" />
              </Button>
            </div>
            <div gap="8">
              <Button id="btn_recenter" macro="button_style" _press="::PlayspaceRecenter" tooltip="WATCH.RECENTER" tooltip_side="left">
                <sprite width="40" height="40" color="~color_text" src="watch/recenter.svg" />
              </Button>
              <Button id="btn_edit_mode" macro="button_style" _press="::EditToggle" tooltip="WATCH.EDIT_MODE" tooltip_side="left">
                <sprite color="~color_text" width="40" height="40" src="watch/edit.svg" />
              </Button>
            </div>
          </div>'''

watch = re.sub(
    r'''          <!-- Four buttons -->\s*
          <div flex_direction="column" gap="8">[\s\S]*?          </div>\s*
        </rectangle>''',
    watch_button_grid + "\n        </rectangle>",
    watch,
    count=1,
)

if "btn_verbatim_dictation" not in watch:
    raise SystemExit("Failed to install Verbatim button into WayVR watch.xml")

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
            <sprite width="38" height="38" color="~color_text" src_ext="{mic_icon}" />
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
