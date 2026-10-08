#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
wayland-scanner client-header ext-background-effect-v1.xml background-effect-client.h
wayland-scanner private-code ext-background-effect-v1.xml background-effect-protocol.c
wayland-scanner client-header kde-blur.xml kde-blur-client.h
wayland-scanner private-code kde-blur.xml kde-blur-protocol.c
# pkg-config produces compiler arguments, intentionally split as shell words.
read -r -a flags <<< "$(pkg-config --cflags --libs gtk4 wayland-client)"
cc -O2 -fPIC -shared -Wall -Wextra blur.c background-effect-protocol.c kde-blur-protocol.c -o libverbatim-blur.so "${flags[@]}" -lm
