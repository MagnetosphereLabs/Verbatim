#!/usr/bin/env bash
# Sourceable package map. No installation or environment mutations here.
verbatim_family() {
  local distro_id="${VERBATIM_DISTRO_ID:-}" distro_like="${VERBATIM_DISTRO_LIKE:-}"
  if [ -z "$distro_id" ] && [ -r /etc/os-release ]; then
    local ID="" ID_LIKE=""
    . /etc/os-release
    distro_id="$ID"; distro_like="$ID_LIKE"
  fi
  case " $distro_id $distro_like " in
    *' ubuntu '*|*' debian '*|*' linuxmint '*|*' pop '*) printf '%s\n' apt ;;
    *' fedora '*|*' rhel '*) printf '%s\n' dnf ;;
    *' arch '*|*' cachyos '*) printf '%s\n' pacman ;;
    *) return 1 ;;
  esac
}
verbatim_packages() {
  case "$1:$2" in
    apt:base) echo 'acl dconf-cli build-essential curl git cmake ninja-build pkg-config python3-dev python3-pip python3-venv python3-gi python3-gi-cairo python3-cairo at-spi2-core gir1.2-atspi-2.0 gir1.2-gtk-4.0 libgtk-4-dev libwayland-dev wayland-protocols libportaudio2 portaudio19-dev libsndfile1 pulseaudio-utils libasound2-plugins wl-clipboard xclip xsel xdotool x11-utils gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-pipewire xdg-desktop-portal' ;;
    dnf:base) echo 'acl dconf gcc gcc-c++ make curl git cmake ninja-build pkgconf-pkg-config python3-devel python3-pip python3-gobject python3-cairo at-spi2-core gtk4 gtk4-devel wayland-devel wayland-protocols-devel portaudio portaudio-devel libsndfile pulseaudio-utils alsa-plugins-pulseaudio wl-clipboard xclip xsel xdotool xprop gstreamer1 gstreamer1-plugins-base pipewire-gstreamer xdg-desktop-portal gtk4-layer-shell' ;;
    pacman:base) echo 'acl dconf base-devel curl git cmake ninja pkgconf python python-pip python-gobject python-cairo at-spi2-core gtk4 gtk4-layer-shell wayland wayland-protocols portaudio libsndfile libpulse alsa-plugins wl-clipboard xclip xsel xdotool xorg-xprop gstreamer gst-plugins-base gst-plugin-pipewire xdg-desktop-portal' ;;
    apt:vulkan) echo 'vulkan-tools libvulkan-dev glslc spirv-headers spirv-tools' ;;
    dnf:vulkan) echo 'vulkan-tools vulkan-loader-devel vulkan-headers glslc spirv-headers spirv-tools' ;;
    pacman:vulkan) echo 'vulkan-tools vulkan-headers vulkan-icd-loader shaderc spirv-headers spirv-tools' ;;
    apt:layer) echo 'meson ninja-build gobject-introspection libgirepository1.0-dev valac' ;;
    dnf:layer) echo 'gtk4-layer-shell' ;;
    pacman:layer) echo 'gtk4-layer-shell' ;;
    *) return 1 ;;
  esac
}

# GTK/GI are installed for the distro interpreter, not whichever Conda/pyenv
# Python happens to lead PATH. An explicit override remains supported.
verbatim_python() {
  if [ -n "${VERBATIM_PYTHON:-}" ]; then
    printf '%s\n' "$VERBATIM_PYTHON"
  elif [ -x /usr/bin/python3 ]; then
    printf '%s\n' /usr/bin/python3
  else
    command -v python3
  fi
}
verbatim_venv_matches() {
  local selected_base existing_base
  [ -x "$2/bin/python" ] || return 1
  local identity='import json,os,sys; print(json.dumps([os.path.realpath(getattr(sys,"_base_executable",sys.executable)),os.path.realpath(sys.base_prefix),list(sys.version_info[:2])]))'
  selected_base="$("$1" -c "$identity")" || return 1
  existing_base="$("$2/bin/python" -c "$identity")" || return 1
  [ "$selected_base" = "$existing_base" ]
}
