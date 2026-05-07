#!/usr/bin/env bash
set -euo pipefail

VERBATIM_REPO_OWNER="${VERBATIM_REPO_OWNER:-MagnetosphereLabs}"
VERBATIM_REPO_NAME="${VERBATIM_REPO_NAME:-Verbatim}"
VERBATIM_BRANCH="${VERBATIM_BRANCH:-main}"

if [ "$(id -u)" -eq 0 ]; then
  echo "Do not run this installer with sudo."
  echo
  echo "Use:"
  echo "  curl -fsSL https://raw.githubusercontent.com/${VERBATIM_REPO_OWNER}/${VERBATIM_REPO_NAME}/${VERBATIM_BRANCH}/install.sh | bash"
  echo
  echo "Reason: Verbatim installs a user service, user shortcut, and files under the desktop user's home directory."
  echo "The installer will ask for sudo internally when system packages or input-device permissions are needed."
  exit 1
fi

SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"

if [ -f "$SCRIPT_SOURCE" ]; then
  ROOT="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
else
  ROOT="$(pwd)"
fi

if [ ! -d "$ROOT/app" ] || [ ! -d "$ROOT/bin" ] || [ ! -d "$ROOT/scripts" ] || [ ! -f "$ROOT/requirements.txt" ]; then
  if [ "${VERBATIM_BOOTSTRAPPED:-0}" != "1" ]; then
    echo "Verbatim bootstrap installer"
    echo "Fetching ${VERBATIM_REPO_OWNER}/${VERBATIM_REPO_NAME}@${VERBATIM_BRANCH}..."

    TMPDIR_INSTALL="$(mktemp -d "${TMPDIR:-/tmp}/verbatim-install.XXXXXXXX")"

    cleanup() {
      rm -rf "$TMPDIR_INSTALL"
    }
    trap cleanup EXIT

    ARCHIVE_URL="https://github.com/${VERBATIM_REPO_OWNER}/${VERBATIM_REPO_NAME}/archive/refs/heads/${VERBATIM_BRANCH}.tar.gz"

    curl -fsSL "$ARCHIVE_URL" -o "$TMPDIR_INSTALL/verbatim.tar.gz"
    tar -xzf "$TMPDIR_INSTALL/verbatim.tar.gz" -C "$TMPDIR_INSTALL"

    FETCHED_ROOT="$(find "$TMPDIR_INSTALL" -mindepth 1 -maxdepth 1 -type d -name "${VERBATIM_REPO_NAME}-*" | head -n1)"

    if [ -z "$FETCHED_ROOT" ] || [ ! -f "$FETCHED_ROOT/install.sh" ]; then
      echo "Could not locate fetched Verbatim installer." >&2
      exit 1
    fi

    export VERBATIM_BOOTSTRAPPED=1
    exec bash "$FETCHED_ROOT/install.sh" "$@"
  fi

  echo "Installer is missing required project files: app/, bin/, scripts/, requirements.txt" >&2
  exit 1
fi

APP="$HOME/.local/share/kdictate-cosmic"
BIN="$HOME/.local/bin"
LOG="$APP/install.log"
SERVICE_DIR="$HOME/.config/systemd/user"
AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/kdictate-cosmic.desktop"
SERVICE_FILE="$SERVICE_DIR/kdictate.service"

mkdir -p "$APP" "$BIN" "$SERVICE_DIR" "$AUTOSTART_DIR"
exec > >(tee -a "$LOG") 2>&1

echo "KDictate Cosmic installer"
echo "Target app dir: $APP"
echo "Install log: $LOG"
echo

rm -f "$HOME/scan-whisper-dictation.sh"

if [ "${XDG_SESSION_TYPE:-}" != "wayland" ]; then
  echo "Notice: XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-unknown}. This package is built for COSMIC on Wayland."
  echo "It can still install, but test it inside your COSMIC Wayland session."
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required for OS packages and input-device access setup." >&2
  exit 1
fi

sudo -v

echo
echo "Detecting GPU backend..."
GPU_BACKEND="cpu"
GPU_NAME="unknown"

if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  GPU_BACKEND="nvidia"
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1 || true)"
elif command -v vulkaninfo >/dev/null 2>&1; then
  GPU_LINE="$(vulkaninfo --summary 2>/dev/null | grep -m1 'deviceName' || true)"
  if [ -n "$GPU_LINE" ]; then
    GPU_BACKEND="vulkan"
    GPU_NAME="$(printf '%s\n' "$GPU_LINE" | sed 's/.*= *//')"
  fi
else
  # vulkan-tools will be installed below; after apt install we check again.
  GPU_BACKEND="maybe-vulkan"
fi

echo "Detected GPU path: $GPU_BACKEND (${GPU_NAME:-unknown})"
echo
echo "Choose Whisper model:"
echo "  1) Speed     ~142 MiB disk, ~388 MB memory"
echo "  2) Balanced  ~466 MiB disk, ~852 MB memory [default]"
echo "  3) Quality   ~2.9 GiB disk, ~3.9 GB memory"
printf "Selection [2]: "
read -r MODEL_CHOICE || true
MODEL_CHOICE="${MODEL_CHOICE:-2}"

case "$MODEL_CHOICE" in
  1)
    KDICTATE_MODEL="base.en"
    KDICTATE_PROFILE="speed"
    ;;
  3)
    KDICTATE_MODEL="large-v3"
    KDICTATE_PROFILE="quality"
    ;;
  *)
    KDICTATE_MODEL="small.en"
    KDICTATE_PROFILE="balanced"
    ;;
esac

echo "Selected: $KDICTATE_PROFILE ($KDICTATE_MODEL)"
echo

echo "Installing OS packages..."
sudo apt-get update
sudo apt-get install -y \
  acl \
  build-essential \
  curl \
  ffmpeg \
  git \
  vulkan-tools \
  libvulkan1 \
  libvulkan-dev \
  gir1.2-atspi-2.0 \
  gir1.2-gtk-4.0 \
  libasound2-dev \
  libgtk-4-1 \
  libgtk-4-dev \
  libportaudio2 \
  libsndfile1 \
  pkg-config \
  valac \
  portaudio19-dev \
  python3-cairo \
  python3-dev \
  python3-gi \
  python3-gi-cairo \
  python3-pip \
  python3-venv \
  wl-clipboard \
  xdg-desktop-portal \
  xdg-desktop-portal-gtk


if [ "$GPU_BACKEND" = "maybe-vulkan" ] && command -v vulkaninfo >/dev/null 2>&1; then
  GPU_LINE="$(vulkaninfo --summary 2>/dev/null | grep -m1 'deviceName' || true)"
  if [ -n "$GPU_LINE" ]; then
    GPU_BACKEND="vulkan"
    GPU_NAME="$(printf '%s\n' "$GPU_LINE" | sed 's/.*= *//')"
  else
    GPU_BACKEND="cpu"
  fi
fi

# gtk4-layer-shell makes the overlay behave like a Wayland shell surface. On
# current Pop/COSMIC builds it may already be present or available as a package;
# if not, build the small upstream library from source.
if ! ldconfig -p 2>/dev/null | grep -q 'libgtk4-layer-shell.so'; then
  echo "Installing gtk4-layer-shell package if available..."
  if apt-cache show libgtk4-layer-shell0 >/dev/null 2>&1; then
    sudo apt-get install -y libgtk4-layer-shell0 gir1.2-gtk4layershell-1.0 || true
  fi
fi

if ! ldconfig -p 2>/dev/null | grep -q 'libgtk4-layer-shell.so'; then
  echo "Building gtk4-layer-shell from source..."
  sudo apt-get install -y meson ninja-build gobject-introspection libgirepository1.0-dev libwayland-dev wayland-protocols
  TMP="$(mktemp -d)"
  git clone --depth=1 https://github.com/wmww/gtk4-layer-shell.git "$TMP/gtk4-layer-shell"
  meson setup "$TMP/gtk4-layer-shell/build" "$TMP/gtk4-layer-shell" --prefix=/usr --buildtype=release
  ninja -C "$TMP/gtk4-layer-shell/build"
  sudo ninja -C "$TMP/gtk4-layer-shell/build" install
  sudo ldconfig
  rm -rf "$TMP"
fi


echo
echo "Configuring transcription backend..."

KDICTATE_BACKEND="faster-whisper"
KDICTATE_DEVICE="cuda"
KDICTATE_COMPUTE_TYPE="float16"
KDICTATE_WHISPER_CPP_BIN=""
KDICTATE_WHISPER_CPP_MODEL=""

if [ "$GPU_BACKEND" = "nvidia" ]; then
  echo "Using NVIDIA faster-whisper CUDA backend."
else
  echo "Using whisper.cpp backend for ${GPU_BACKEND}."
  KDICTATE_BACKEND="whisper.cpp"
  KDICTATE_DEVICE="cpu"
  KDICTATE_COMPUTE_TYPE="int8"

  sudo apt-get install -y cmake ninja-build

  WHISPER_CPP_DIR="$APP/whisper.cpp"
  if [ ! -d "$WHISPER_CPP_DIR/.git" ]; then
    rm -rf "$WHISPER_CPP_DIR"
    git clone --depth=1 https://github.com/ggml-org/whisper.cpp.git "$WHISPER_CPP_DIR"
  else
    git -C "$WHISPER_CPP_DIR" pull --ff-only || true
  fi

  if [ "$GPU_BACKEND" = "vulkan" ]; then
    echo "Building whisper.cpp with Vulkan."
    cmake -S "$WHISPER_CPP_DIR" -B "$WHISPER_CPP_DIR/build" -G Ninja \
      -DGGML_VULKAN=ON \
      -DCMAKE_BUILD_TYPE=Release
  else
    echo "Building whisper.cpp CPU fallback."
    cmake -S "$WHISPER_CPP_DIR" -B "$WHISPER_CPP_DIR/build" -G Ninja \
      -DCMAKE_BUILD_TYPE=Release
  fi
  cmake --build "$WHISPER_CPP_DIR/build" -j"$(nproc)"

  mkdir -p "$APP/models"
  if [ ! -f "$APP/models/ggml-$KDICTATE_MODEL.bin" ]; then
    "$WHISPER_CPP_DIR/models/download-ggml-model.sh" "$KDICTATE_MODEL" "$APP/models"
  fi

  KDICTATE_WHISPER_CPP_BIN="$WHISPER_CPP_DIR/build/bin/whisper-cli"
  KDICTATE_WHISPER_CPP_MODEL="$APP/models/ggml-$KDICTATE_MODEL.bin"
fi

mkdir -p "$APP"
cat > "$APP/config.env" <<CONFIG
KDICTATE_BACKEND=$KDICTATE_BACKEND
KDICTATE_MODEL=$KDICTATE_MODEL
KDICTATE_PROFILE=$KDICTATE_PROFILE
KDICTATE_DEVICE=$KDICTATE_DEVICE
KDICTATE_COMPUTE_TYPE=$KDICTATE_COMPUTE_TYPE
KDICTATE_WHISPER_CPP_BIN=$KDICTATE_WHISPER_CPP_BIN
KDICTATE_WHISPER_CPP_MODEL=$KDICTATE_WHISPER_CPP_MODEL
CONFIG

echo "Backend config written to $APP/config.env"
cat "$APP/config.env"
echo
echo "Copying KDictate files..."
rm -rf "$APP/app" "$APP/scripts" "$APP/systemd" "$APP/docs"
mkdir -p "$APP"
cp -a "$ROOT/app" "$APP/app"
cp -a "$ROOT/scripts" "$APP/scripts"
cp -a "$ROOT/systemd" "$APP/systemd"
cp -a "$ROOT/docs" "$APP/docs"
cp "$ROOT/requirements.txt" "$APP/requirements.txt"
cp "$ROOT/README.md" "$APP/README.md" 2>/dev/null || true
install -m 0755 "$ROOT/bin/kdictate" "$BIN/kdictate"

echo "Creating Python environment..."
python3 -m venv --system-site-packages "$APP/venv"
"$APP/venv/bin/python" -m pip install --upgrade pip wheel setuptools
"$APP/venv/bin/pip" install -r "$APP/requirements.txt"

echo "Configuring local input injection and manual-typing detection..."
sudo modprobe uinput || true
echo uinput | sudo tee /etc/modules-load.d/kdictate-uinput.conf >/dev/null
sudo tee /etc/udev/rules.d/80-kdictate-uinput.rules >/dev/null <<'RULES'
KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
KERNEL=="event*", SUBSYSTEM=="input", GROUP="input", MODE="0640"
RULES
sudo usermod -aG input "$USER" || true
sudo udevadm control --reload-rules || true
sudo udevadm trigger || true
sudo setfacl -m "u:$USER:rw" /dev/uinput 2>/dev/null || true
for dev in /dev/input/event*; do
  [ -e "$dev" ] || continue
  sudo setfacl -m "u:$USER:r" "$dev" 2>/dev/null || true
done

if command -v systemctl >/dev/null 2>&1; then
  echo "Installing user service..."
  install -m 0644 "$ROOT/systemd/kdictate.service" "$SERVICE_FILE"
  systemctl --user daemon-reload || true
  systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR PATH || true
  systemctl --user enable --now kdictate.service || true
fi

cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=KDictate Cosmic
Comment=Local Whisper dictation overlay for COSMIC Wayland
Exec=$HOME/.local/bin/kdictate daemon
X-GNOME-Autostart-enabled=true
NoDisplay=true
Terminal=false
DESKTOP
chmod 0644 "$DESKTOP_FILE"

echo "Registering Super+V in COSMIC..."
"$APP/venv/bin/python" "$APP/scripts/register_cosmic_shortcut.py" "$HOME/.local/bin/kdictate toggle"

echo "Restarting daemon..."
"$BIN/kdictate" quit >/dev/null 2>&1 || true
sleep 0.3
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user restart kdictate.service || true
fi
if ! "$BIN/kdictate" status >/dev/null 2>&1; then
  nohup "$BIN/kdictate" daemon >> "$APP/kdictate.log" 2>&1 &
  sleep 1
fi

echo "Running doctor..."
"$BIN/kdictate" doctor || true

echo
echo "Warming Whisper $KDICTATE_MODEL with backend $KDICTATE_BACKEND."
"$BIN/kdictate" warmup || true

echo
echo "Install complete."
echo "Use: press Super+V in a text field, speak, then pause."
echo "Logs: $APP/kdictate.log"
echo
echo "Important: if doctor reports /dev/uinput or /dev/input permission problems, log out and back in once."
echo "That refreshes the new input group membership."
