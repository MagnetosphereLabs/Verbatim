#!/usr/bin/env bash
set -euo pipefail
# Support the public curl | bash entry point as well as an extracted checkout.
# stdin has no script path: never mistake the caller's working directory for
# the requested update, even when it contains an older complete checkout.
INSTALL_SOURCE="${BASH_SOURCE[0]:-}"
ROOT=''
if [ -n "$INSTALL_SOURCE" ] && [ -f "$INSTALL_SOURCE" ]; then
  ROOT="$(cd -- "$(dirname -- "$INSTALL_SOURCE")" && pwd)"
fi
REQUIRED_FILES=(
  install.sh requirements.txt app/kdictate.py bin/kdictate
  scripts/platform.sh scripts/verbatim-session scripts/verbatim-wayvr
  scripts/register_desktop_shortcut.py scripts/register_cosmic_shortcut.py
  scripts/install_wayvr_integration.sh systemd/kdictate.service
  native/build.sh native/blur.c native/ext-background-effect-v1.xml native/kde-blur.xml
)
complete_bundle() {
  local directory="$1" file
  [ -n "$directory" ] || return 1
  for file in "${REQUIRED_FILES[@]}"; do
    [ -s "$directory/$file" ] || return 1
  done
}
bootstrap_verbatim() (
  local dependency bootstrap_directory
  for dependency in curl tar mktemp; do
    command -v "$dependency" >/dev/null || { echo "Install $dependency, then retry the Verbatim install command." >&2; exit 1; }
  done
  bootstrap_directory="$(mktemp -d "${TMPDIR:-/tmp}/verbatim-install.XXXXXX")"
  trap 'rm -rf -- "$bootstrap_directory"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  echo 'Downloading the complete Verbatim update from GitHub...'
  # A single repository archive keeps every companion file at the same revision.
  # Complete staging happens before the installed daemon or files are touched.
  curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 300 \
    'https://github.com/MagnetosphereLabs/Verbatim/archive/refs/heads/main.tar.gz' \
    --output "$bootstrap_directory/repository.tar.gz"
  tar --extract --gzip --file "$bootstrap_directory/repository.tar.gz" \
    --directory "$bootstrap_directory" --strip-components=1 --no-same-owner --no-same-permissions
  if ! complete_bundle "$bootstrap_directory"; then
    echo 'The downloaded repository is missing required Verbatim files. Your existing installation has not been changed.' >&2
    exit 1
  fi
  # Do not feed the still-arriving outer script into a child command's stdin.
  # Installer prompts explicitly use /dev/tty, so interactive setup still works.
  bash "$bootstrap_directory/install.sh" "$@" </dev/null
)
if ! complete_bundle "$ROOT"; then
  bootstrap_verbatim "$@"
  exit $?
fi

. "$ROOT/scripts/platform.sh"
FAMILY="$(verbatim_family)" || { echo 'Supported package families: Ubuntu/Mint/Pop (APT), Fedora (DNF), Arch/CachyOS (pacman).' >&2; exit 1; }
if [ "${1:-}" = '--print-package-plan' ]; then
  echo "Package family: $FAMILY"
  verbatim_packages "$FAMILY" base
  verbatim_packages "$FAMILY" vulkan
  exit 0
fi
if [ "$(id -u)" -eq 0 ]; then
  echo 'Run this installer as your desktop user, without sudo. It requests sudo for system packages and device access.' >&2
  exit 1
fi
# These variables belong to the caller's Python environment. Changes here are
# confined to this installer process and do not deactivate or modify Conda.
unset PYTHONHOME PYTHONPATH
APP="${KDICTATE_APPDIR:-$HOME/.local/share/kdictate-cosmic}"
BIN="$HOME/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
SERVICE_DIR="$CONFIG_DIR/systemd/user"
mkdir -p "$APP" "$BIN" "$SERVICE_DIR" "$CONFIG_DIR/autostart"
exec > >(tee -a "$APP/install.log") 2>&1
printf 'Installing Verbatim with %s packages into %s\n' "$FAMILY" "$APP"
command -v sudo >/dev/null || { echo 'sudo is required for system dependency setup.' >&2; exit 1; }
sudo -v
packages() {
  case "$FAMILY" in
    apt) sudo apt-get install -y "$@" ;;
    dnf) sudo dnf install -y "$@" ;;
    pacman) sudo pacman -S --needed --noconfirm "$@" ;;
  esac
}
if [ "$FAMILY" = apt ]; then sudo apt-get update; fi
read -r -a BASE_PACKAGES <<< "$(verbatim_packages "$FAMILY" base)"
packages "${BASE_PACKAGES[@]}"
# Keep the existing desktop's portal implementation. Install the matching one
# only if missing; do not replace PipeWire/PulseAudio or the user's audio policy.
DESKTOP="${XDG_CURRENT_DESKTOP:-${DESKTOP_SESSION:-}}"
DESKTOP="${DESKTOP^^}"
case "$DESKTOP" in
  *GNOME*|*UBUNTU*) PORTAL='gnome' ;;
  *KDE*|*PLASMA*) PORTAL='kde' ;;
  *COSMIC*) PORTAL='cosmic' ;;
  *) PORTAL='gtk' ;;
esac
if [ "$PORTAL" != cosmic ]; then
  if ! packages "xdg-desktop-portal-$PORTAL"; then
    echo "Desktop portal package unavailable; retaining the installed portal backend."
  fi
fi
PYTHON="$(verbatim_python)"
printf 'Using Python: %s (Verbatim keeps its own virtual environment)\n' "$PYTHON"
"$PYTHON" -c 'import gi,cairo; gi.require_version("Gtk","4.0"); gi.require_version("Atspi","2.0"); from gi.repository import Gtk,Atspi' || {
  echo 'The selected Python must match the distribution GTK and accessibility packages. VERBATIM_PYTHON, if set, must select that interpreter.' >&2; exit 1;
}
# Preserve model/appearance/mic preferences when updating. Never source config
# as shell code; paths and tokens can contain spaces or shell punctuation.
OLD_PROFILE="$("$PYTHON" - "$APP/config.env" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); data={}
if p.exists():
 for line in p.read_text().splitlines():
  key,sep,value=line.partition('=')
  if sep: data[key.strip()]=value.strip().strip('\"\'')
profile=data.get('KDICTATE_PROFILE','')
print(profile if profile in {'speed','balanced','quality'} else '')
PY
)"
CHOICE="${VERBATIM_MODEL_CHOICE:-$OLD_PROFILE}"
if [ -z "$CHOICE" ]; then
  echo 'Model: 1 Speed (base.en), 2 Balanced (small.en, default), 3 Quality (large-v3).'
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf 'Selection [2]: ' >/dev/tty
    read -r CHOICE </dev/tty || true
  fi
fi
case "${CHOICE:-2}" in
  1|speed) MODEL=base.en; PROFILE=speed ;;
  3|quality) MODEL=large-v3; PROFILE=quality ;;
  2|balanced) MODEL=small.en; PROFILE=balanced ;;
  *) echo 'Invalid model selection.' >&2; exit 1 ;;
esac
CPU_THREADS="$(nproc 2>/dev/null || echo 1)"
PERCENT="${VERBATIM_BUILD_CPU_PERCENT:-50}"
[[ "$PERCENT" =~ ^[0-9]+$ ]] && [ "$PERCENT" -ge 1 ] && [ "$PERCENT" -le 100 ] || { echo 'Build CPU percent must be 1–100.' >&2; exit 1; }
BUILD_JOBS=$((CPU_THREADS * PERCENT / 100)); [ "$BUILD_JOBS" -gt 0 ] || BUILD_JOBS=1
export CMAKE_BUILD_PARALLEL_LEVEL="$BUILD_JOBS"
build() { nice -n 10 "$@"; }
# Dependencies and Python are validated before interrupting an existing daemon.
# Keep the restoration journal and lock inode intact for VR session recovery.
if command -v systemctl >/dev/null; then systemctl --user stop kdictate.service || true; fi
if [ -x "$BIN/kdictate" ]; then "$BIN/kdictate" quit >/dev/null 2>&1 || true; fi
# Copy only application-owned trees. Config, models, recovery files, and journal
# remain intact. Installing this bundle never fetches an unpatched app from main.
for tree in app scripts systemd native; do
  mkdir -p "$APP/$tree"
  cp -a "$ROOT/$tree/." "$APP/$tree/"
done
cp "$ROOT/requirements.txt" "$APP/requirements.txt"
install -m 0755 "$ROOT/bin/kdictate" "$BIN/kdictate"
install -m 0755 "$ROOT/scripts/verbatim-wayvr" "$BIN/verbatim-wayvr"
install -m 0755 "$ROOT/scripts/verbatim-session" "$BIN/verbatim-session"
install -m 0644 "$ROOT/systemd/kdictate.service" "$SERVICE_DIR/kdictate.service"
# Small local blur bridge: native COSMIC/GNOME protocol and KDE's blur protocol.
if ! bash "$APP/native/build.sh"; then
  echo 'Native blur bridge failed to build; real local screen blur remains available. See install.log.'
fi
# Layer shell avoids input focus changes on COSMIC/KDE. Mint X11 uses native
# nonfocus hints. GNOME uses a nonfocusable regular window and hides before paste.
if [ "$FAMILY" = apt ]; then
  if apt-cache show libgtk4-layer-shell0 >/dev/null 2>&1; then
    packages libgtk4-layer-shell0 gir1.2-gtk4layershell-1.0 || true
  fi
  if [[ "$DESKTOP" == *COSMIC* || "$DESKTOP" == *KDE* || "$DESKTOP" == *PLASMA* ]] && ! ldconfig -p 2>/dev/null | grep -q 'libgtk4-layer-shell'; then
    read -r -a LAYER_PACKAGES <<< "$(verbatim_packages "$FAMILY" layer)"
    packages "${LAYER_PACKAGES[@]}"
    LAYER_BUILD="$(mktemp -d)"
    if git clone --depth=1 https://github.com/wmww/gtk4-layer-shell.git "$LAYER_BUILD/source" &&
       meson setup "$LAYER_BUILD/build" "$LAYER_BUILD/source" --prefix="$APP/native/layer" --libdir=lib -Dexamples=false -Ddocs=false -Dtests=false &&
       build ninja -C "$LAYER_BUILD/build" -j"$BUILD_JOBS" && ninja -C "$LAYER_BUILD/build" install; then
      echo 'Local GTK4 layer-shell support installed.'
    else
      echo 'Layer-shell build unavailable; continuing with the GTK window backend.'
    fi
    rm -rf -- "$LAYER_BUILD"
  fi
fi
GPU=cpu
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then GPU=nvidia; fi
BACKEND=faster-whisper; DEVICE=cpu; COMPUTE=int8; CPP_BIN=''; CPP_MODEL=''
if [ "$GPU" = nvidia ]; then
  DEVICE=cuda; COMPUTE=float16
else
  read -r -a VULKAN_PACKAGES <<< "$(verbatim_packages "$FAMILY" vulkan)"
  if packages "${VULKAN_PACKAGES[@]}"; then
    # Software Vulkan adapters (llvmpipe/lavapipe) are not hardware acceleration.
    if timeout 10 vulkaninfo --summary 2>/dev/null | sed -n '/deviceType.*DISCRETE_GPU\|deviceType.*INTEGRATED_GPU/p' | head -1 | grep -q .; then GPU=vulkan; fi
  fi
  if [ "$GPU" = vulkan ]; then
    BACKEND=whisper.cpp
    CPP_DIR="$APP/whisper.cpp"
    if [ ! -d "$CPP_DIR/.git" ]; then git clone --depth=1 https://github.com/ggml-org/whisper.cpp.git "$CPP_DIR"; fi
    if cmake -S "$CPP_DIR" -B "$CPP_DIR/build" -G Ninja -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release && build cmake --build "$CPP_DIR/build" -j"$BUILD_JOBS"; then
      CPP_BIN="$CPP_DIR/build/bin/whisper-cli"
      CPP_MODEL="$APP/models/ggml-$MODEL.bin"
      mkdir -p "$APP/models"
      if [ ! -s "$CPP_MODEL" ]; then "$CPP_DIR/models/download-ggml-model.sh" "$MODEL" "$APP/models"; fi
    else
      echo 'Vulkan build failed; using faster-whisper CPU instead.'
      BACKEND=faster-whisper; CPP_BIN=''; CPP_MODEL=''
    fi
  fi
fi
if [ -e "$APP/venv/pyvenv.cfg" ] && ! verbatim_venv_matches "$PYTHON" "$APP/venv"; then
  echo "Rebuilding Verbatim's virtual environment for the selected Python; preferences and models are retained."
  "$PYTHON" -m venv --clear --system-site-packages "$APP/venv"
else
  "$PYTHON" -m venv --system-site-packages "$APP/venv"
fi
"$APP/venv/bin/python" -m pip install --upgrade pip wheel setuptools
CORE_REQUIREMENTS="$(mktemp)"
trap 'rm -f -- "$CORE_REQUIREMENTS"' EXIT
sed '/^faster-whisper/d' "$APP/requirements.txt" >"$CORE_REQUIREMENTS"
"$APP/venv/bin/python" -m pip install -r "$CORE_REQUIREMENTS"
if ! "$APP/venv/bin/python" -m pip install 'faster-whisper>=1.1.1,<2'; then
  echo 'No compatible faster-whisper runtime; enabling native whisper.cpp CPU fallback.'
  if [ "$BACKEND" != whisper.cpp ]; then
    CPP_DIR="$APP/whisper.cpp"
    if [ ! -d "$CPP_DIR/.git" ]; then git clone --depth=1 https://github.com/ggml-org/whisper.cpp.git "$CPP_DIR"; fi
    cmake -S "$CPP_DIR" -B "$CPP_DIR/build-cpu" -G Ninja -DGGML_VULKAN=OFF -DCMAKE_BUILD_TYPE=Release
    build cmake --build "$CPP_DIR/build-cpu" -j"$BUILD_JOBS"
    BACKEND=whisper.cpp; DEVICE=cpu; COMPUTE=int8
    CPP_BIN="$CPP_DIR/build-cpu/bin/whisper-cli"; CPP_MODEL="$APP/models/ggml-$MODEL.bin"
    mkdir -p "$APP/models"
    if [ ! -s "$CPP_MODEL" ]; then "$CPP_DIR/models/download-ggml-model.sh" "$MODEL" "$APP/models"; fi
  fi
fi
if [ "$GPU" = nvidia ] && [ "$BACKEND" = faster-whisper ]; then
  "$APP/venv/bin/python" -m pip install 'nvidia-cublas-cu12>=12.4,<13' 'nvidia-cudnn-cu12>=9,<10'
fi
"$PYTHON" - "$APP/config.env" "$BACKEND" "$MODEL" "$PROFILE" "$DEVICE" "$COMPUTE" "$CPP_BIN" "$CPP_MODEL" <<'PY'
from pathlib import Path
import os,sys,tempfile
path=Path(sys.argv[1]); keys=['KDICTATE_BACKEND','KDICTATE_MODEL','KDICTATE_PROFILE','KDICTATE_DEVICE','KDICTATE_COMPUTE_TYPE','KDICTATE_WHISPER_CPP_BIN','KDICTATE_WHISPER_CPP_MODEL']
updates=dict(zip(keys,sys.argv[2:])); data={}
if path.exists():
 for line in path.read_text().splitlines():
  key,sep,value=line.partition('=')
  if sep and not key.lstrip().startswith('#'): data[key.strip()]=value.strip()
data.update(updates)
for key,value in {'KDICTATE_THEME':'glass-dark','KDICTATE_MIC_DEVICE':'','KDICTATE_WIVRN_AUTO_AUDIO':'1','KDICTATE_WIVRN_PAUSE_EASYEFFECTS':'1','KDICTATE_REALTIME_TRANSCRIPTION':'1','KDICTATE_REALTIME_SILENCE_TO_FINISH_SECONDS':'2.85'}.items(): data.setdefault(key,value)
fd,tmp=tempfile.mkstemp(prefix='config-',dir=path.parent)
with os.fdopen(fd,'w') as f:
 f.write(''.join(f'{k}={v}\n' for k,v in data.items())); f.flush(); os.fsync(f.fileno())
os.replace(tmp,path)
PY
# Seat-scoped access instead of changing permissions on every input device or
# adding the user to a group with unrestricted access to all input hardware.
sudo modprobe uinput
printf 'uinput\n' | sudo tee /etc/modules-load.d/verbatim-uinput.conf >/dev/null
sudo tee /etc/udev/rules.d/70-verbatim-input.rules >/dev/null <<'RULES'
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"
SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_KEYBOARD}=="1", TAG+="uaccess"
RULES
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=misc --sysname-match=uinput
sudo udevadm trigger --subsystem-match=input
sudo setfacl -m "u:$(id -u):rw" /dev/uinput || true
for dev in /dev/input/event*; do
  [ -e "$dev" ] || continue
  if udevadm info --query=property --name="$dev" | grep -qx 'ID_INPUT_KEYBOARD=1'; then sudo setfacl -m "u:$(id -u):r" "$dev" || true; fi
done
# Desktop autostart imports the exact environment at every login. systemd keeps
# the model warm and follows graphical-session lifetime on supporting desktops.
"$PYTHON" - "$CONFIG_DIR/autostart/kdictate-cosmic.desktop" "$BIN/verbatim-session" <<'PY'
from pathlib import Path
import sys
command='"'+sys.argv[2].replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$')+'"'
Path(sys.argv[1]).write_text('[Desktop Entry]\nType=Application\nName=Verbatim\nExec='+command+'\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n')
PY
SHORTCUT_COMMAND="$("$PYTHON" - "$BIN/kdictate" <<'PY'
import shlex,sys
print(shlex.join([sys.argv[1],'toggle']))
PY
)"
SHORTCUT_READY=0
if "$APP/venv/bin/python" "$APP/scripts/register_desktop_shortcut.py" "$SHORTCUT_COMMAND"; then
  SHORTCUT_READY=1
else
  echo 'Shortcut setup needs attention; see the message above.'
fi
if command -v wayvr >/dev/null || command -v wayvrctl >/dev/null || [ -d "$CONFIG_DIR/wayvr" ]; then
  bash "$APP/scripts/install_wayvr_integration.sh" || echo 'WayVR custom UI was not changed; review install.log.'
fi
"$BIN/verbatim-session"
READY=0
for attempt in {1..40}; do
  if "$BIN/kdictate" status >/dev/null 2>&1; then READY=1; break; fi
  sleep 0.5
done
if [ "$READY" != 1 ]; then
  echo "Daemon startup failed. See $APP/kdictate.log and journalctl --user -u kdictate.service." >&2
  exit 1
fi
"$BIN/kdictate" doctor
"$BIN/kdictate" warmup
if [ "$SHORTCUT_READY" = 1 ]; then
  printf '\nInstalled. Press Super+V in a text field, speak, then pause.\n'
else
  printf '\nInstalled. Assign a free keyboard shortcut to: %s\n' "$SHORTCUT_COMMAND"
fi
printf 'Theme defaults to Glass dark; Appearance offers four themes.\nRecovery command: kdictate last-transcript\n'
printf 'Verbatim has been restarted with this update. No reboot or logout is required.\n'
