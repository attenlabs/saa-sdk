# Cross-compiling for Raspberry Pi OS arm64, or any aarch64-linux-gnu
# Debian or Ubuntu, from a Debian or Ubuntu host, against the target's own
# libwebsockets from multiarch:
#
#   sudo dpkg --add-architecture arm64 && sudo apt update
#   sudo apt install crossbuild-essential-arm64 libwebsockets-dev:arm64 qemu-user
#   cmake -S . -B build-arm64 -DCMAKE_TOOLCHAIN_FILE=cmake/aarch64-linux-gnu.cmake
#
# With qemu-user installed, ctest runs the test binaries under qemu-aarch64;
# tests/cross_check.sh arm64 does all of it.
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_C_COMPILER aarch64-linux-gnu-gcc)
set(CMAKE_CXX_COMPILER aarch64-linux-gnu-g++)
set(CMAKE_LIBRARY_ARCHITECTURE aarch64-linux-gnu)

# pkg-config reads the target's .pc files, never the host's
set(ENV{PKG_CONFIG_LIBDIR} /usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig)
set(ENV{PKG_CONFIG_PATH} "")

find_program(SAA_CROSS_EMULATOR qemu-aarch64)
if(SAA_CROSS_EMULATOR)
  set(CMAKE_CROSSCOMPILING_EMULATOR ${SAA_CROSS_EMULATOR})
endif()
