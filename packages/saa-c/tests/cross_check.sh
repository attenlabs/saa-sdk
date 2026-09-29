#!/bin/sh
# cross_check.sh native|arm64|armhf - builds saa-c on a Debian or Ubuntu host,
# natively or cross-compiled against the target's libwebsockets from multiarch,
# and runs the tests. The Arm builds run the unit tests, the header checks, and
# the harness under qemu; conformance's timing is not meant for emulation.
#
#   sudo dpkg --add-architecture armhf && sudo apt update
#   sudo apt install crossbuild-essential-armhf libwebsockets-dev:armhf qemu-user
#   tests/cross_check.sh armhf
#
# SAA_PYTHON names a Python with websockets 13 or newer for the harness; without
# one, the tests that need the mock server are skipped.
set -eu
arch=${1:-}
pkg=$(cd "$(dirname "$0")/.." && pwd)
case $arch in
  native) triple= ;;
  arm64)  triple=aarch64-linux-gnu;   qemu=qemu-aarch64 ;;
  armhf)  triple=arm-linux-gnueabihf; qemu=qemu-arm ;;
  *) echo "usage: cross_check.sh native|arm64|armhf" >&2; exit 2 ;;
esac

build=$pkg/build/cross-$arch
set -- -DSAA_WERROR=ON
if [ -n "${SAA_PYTHON:-}" ]; then
  set -- "$@" -DPython3_EXECUTABLE="$SAA_PYTHON"
fi
if [ -n "$triple" ]; then
  missing=
  command -v "$triple-gcc" >/dev/null || missing="$missing crossbuild-essential-$arch"
  [ -e "/usr/lib/$triple/pkgconfig/libwebsockets.pc" ] || missing="$missing libwebsockets-dev:$arch"
  command -v "$qemu" >/dev/null || missing="$missing qemu-user"
  if [ -n "$missing" ]; then
    echo "missing:$missing" >&2
    echo "  sudo dpkg --add-architecture $arch && sudo apt update && sudo apt install$missing" >&2
    exit 2
  fi
  set -- "$@" -DCMAKE_TOOLCHAIN_FILE="$pkg/cmake/$triple.cmake"
fi

cmake -S "$pkg" -B "$build" "$@"
cmake --build "$build" --parallel
if [ -n "$triple" ]; then
  file "$build/saa_client_demo" 2>/dev/null || true
  # ctest runs test binaries through qemu itself; the Python runners are told here
  SAA_TEST_EMULATOR=$qemu ctest --test-dir "$build" --output-on-failure -E '^(conformance|consumer)$'
else
  ctest --test-dir "$build" --output-on-failure
fi
