# Cross-compiling for Raspberry Pi OS armhf (32-bit), or any arm-linux-gnueabihf
# Debian or Ubuntu, from a Debian or Ubuntu host, against the target's own
# libwebsockets from multiarch:
#
#   sudo dpkg --add-architecture armhf && sudo apt update
#   sudo apt install crossbuild-essential-armhf libwebsockets-dev:armhf qemu-user
#   cmake -S . -B build-armhf -DCMAKE_TOOLCHAIN_FILE=cmake/arm-linux-gnueabihf.cmake
#
# With qemu-user installed, ctest runs the test binaries under qemu-arm;
# tests/cross_check.sh armhf does all of it.
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)
set(CMAKE_C_COMPILER arm-linux-gnueabihf-gcc)
set(CMAKE_CXX_COMPILER arm-linux-gnueabihf-g++)
# Multiarch puts the target's files in the host's /usr, under their own triple,
# so there is no CMAKE_FIND_ROOT_PATH: the CMAKE_FIND_ROOT_PATH_MODE_* settings
# only act below one. This architecture, and pkg-config's LIBDIR below, point
# the searches at the target's files instead.
set(CMAKE_LIBRARY_ARCHITECTURE arm-linux-gnueabihf)

# pkg-config reads the target's .pc files, never the host's
set(ENV{PKG_CONFIG_LIBDIR} /usr/lib/arm-linux-gnueabihf/pkgconfig:/usr/share/pkgconfig)
set(ENV{PKG_CONFIG_PATH} "")

find_program(SAA_CROSS_EMULATOR qemu-arm)
if(SAA_CROSS_EMULATOR)
  set(CMAKE_CROSSCOMPILING_EMULATOR ${SAA_CROSS_EMULATOR})
endif()
