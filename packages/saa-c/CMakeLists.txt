# saa-c: SAA cloud streaming client for C/C++ devices.
cmake_minimum_required(VERSION 3.16)

# The version lives in the public header; CMake reads it from there.
file(STRINGS ${CMAKE_CURRENT_SOURCE_DIR}/include/saa/saa_client.h _saa_ver
     REGEX "^#define SAA_CLIENT_VERSION_(MAJOR|MINOR|PATCH) +[0-9]+")
foreach(_part MAJOR MINOR PATCH)
  string(REGEX REPLACE ".*SAA_CLIENT_VERSION_${_part} +([0-9]+).*" "\\1" SAA_VERSION_${_part} "${_saa_ver}")
endforeach()
project(saa_c
  VERSION ${SAA_VERSION_MAJOR}.${SAA_VERSION_MINOR}.${SAA_VERSION_PATCH}
  LANGUAGES C)

option(SAA_BUILD_TESTS        "Build the tests"                                  ON)
option(SAA_BUILD_DEMO         "Build saa_client_demo"                            ON)
option(SAA_BUILD_SHARED       "Also build libsaaclient.so"                       OFF)
option(SAA_WITH_CAPTURE       "Build the ALSA + V4L2 capture module"             OFF)
option(SAA_ALLOW_INSECURE_TLS "Honour insecure_skip_verify (never in releases)"  OFF)
option(SAA_WERROR             "Treat warnings in saa-c's own sources as errors"  OFF)
option(SAA_REQUIRE_MOCK_TESTS "Fail the configure if the mock-server tests cannot run" OFF)
set(SAA_SANITIZE "" CACHE STRING "Sanitizer for saa-c's own code: address, undefined, thread, or empty")


set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

set(SAA_WARNINGS -Wall -Wextra)
if(SAA_WERROR)
  list(APPEND SAA_WARNINGS -Werror)
endif()
if(SAA_SANITIZE)
  add_compile_options(-fsanitize=${SAA_SANITIZE} -fno-omit-frame-pointer)
  add_link_options(-fsanitize=${SAA_SANITIZE})
endif()

include(GNUInstallDirs)
include(CheckCSourceCompiles)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
# our own prefix so no touching consumer variables
pkg_check_modules(SAAC_LWS REQUIRED IMPORTED_TARGET libwebsockets)
message(STATUS "libwebsockets ${SAAC_LWS_VERSION}")

# only need headers
if(APPLE AND NOT OPENSSL_ROOT_DIR AND EXISTS /opt/homebrew/opt/openssl@3)
  set(OPENSSL_ROOT_DIR /opt/homebrew/opt/openssl@3)
endif()
find_package(OpenSSL)

# The capture sources: a microphone through ALSA, a camera through V4L2.
if(SAA_WITH_CAPTURE)
  if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "SAA_WITH_CAPTURE needs Linux: its sources are ALSA and V4L2")
  endif()
  pkg_check_modules(SAAC_ALSA REQUIRED IMPORTED_TARGET alsa)
  include(CheckIncludeFile)
  check_include_file(linux/videodev2.h SAA_HAVE_VIDEODEV2)
  if(NOT SAA_HAVE_VIDEODEV2)
    message(FATAL_ERROR "SAA_WITH_CAPTURE needs linux/videodev2.h, from the kernel headers")
  endif()
  message(STATUS "capture: ALSA ${SAAC_ALSA_VERSION}, V4L2")
endif()

# 32-bit atomics are builtins everywhere we build; link libatomic only if the
# toolchain says otherwise.
set(_saa_atomic_probe "#include <stdint.h>
int main(void) { uint32_t v = 0; __atomic_fetch_add(&v, 1u, __ATOMIC_ACQ_REL);
                 return (int)__atomic_load_n(&v, __ATOMIC_ACQUIRE); }")
check_c_source_compiles("${_saa_atomic_probe}" SAA_ATOMIC_BUILTIN)
set(SAA_ATOMIC_LIB "")
if(NOT SAA_ATOMIC_BUILTIN)
  set(CMAKE_REQUIRED_LIBRARIES atomic)
  check_c_source_compiles("${_saa_atomic_probe}" SAA_ATOMIC_WITH_LIB)
  unset(CMAKE_REQUIRED_LIBRARIES)
  if(NOT SAA_ATOMIC_WITH_LIB)
    message(FATAL_ERROR "no usable __atomic builtins, with or without libatomic")
  endif()
  set(SAA_ATOMIC_LIB atomic)
endif()

# Vendored: cJSON 1.7.18 (MIT) and a small base64 codec.
add_library(saa_third_party STATIC
  third_party/cJSON/cJSON.c
  third_party/base64/base64.c)
target_include_directories(saa_third_party PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/cJSON
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/base64)
set_target_properties(saa_third_party PROPERTIES POSITION_INDEPENDENT_CODE ON)
set_source_files_properties(third_party/cJSON/cJSON.c PROPERTIES COMPILE_FLAGS "-w")

# ── libsaaclient ──────────────────────────────────────────────────────────
# The vendored sources are compiled in, so the archive is self-contained.
set(SAA_CLIENT_SOURCES
  src/audio_intake.c
  src/backoff.c
  src/capture.c
  src/client.c
  src/clock.c
  src/jpeg.c
  src/log.c
  src/protocol.c
  src/resolver.c
  src/transport_lws.c
  src/url.c
  src/video_intake.c
  $<$<BOOL:${SAA_WITH_CAPTURE}>:src/capture/alsa_source.c>
  $<$<BOOL:${SAA_WITH_CAPTURE}>:src/capture/v4l2_mjpeg_source.c>
  third_party/cJSON/cJSON.c
  third_party/base64/base64.c)

function(saa_client_target name type)
  add_library(${name} ${type} ${SAA_CLIENT_SOURCES})
  target_include_directories(${name}
    PUBLIC  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
            $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
            ${CMAKE_CURRENT_SOURCE_DIR}/third_party/cJSON
            ${CMAKE_CURRENT_SOURCE_DIR}/third_party/base64)
  target_compile_definitions(${name} PRIVATE SAA_CLIENT_BUILDING
    _POSIX_C_SOURCE=200809L $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>
    $<$<BOOL:${SAA_ALLOW_INSECURE_TLS}>:SAA_ALLOW_INSECURE_TLS>
    $<$<BOOL:${SAA_WITH_CAPTURE}>:SAAC_WITH_CAPTURE>)
  target_compile_options(${name} PRIVATE ${SAA_WARNINGS})
  target_link_libraries(${name} PRIVATE PkgConfig::SAAC_LWS Threads::Threads m ${SAA_ATOMIC_LIB})
  if(SAA_WITH_CAPTURE)
    target_link_libraries(${name} PRIVATE PkgConfig::SAAC_ALSA)
  endif()
  if(OpenSSL_FOUND)
    target_include_directories(${name} PRIVATE ${OPENSSL_INCLUDE_DIR})
  endif()
  set_target_properties(${name} PROPERTIES
    OUTPUT_NAME saaclient
    C_VISIBILITY_PRESET hidden
    POSITION_INDEPENDENT_CODE ON
    VERSION ${PROJECT_VERSION}
    SOVERSION ${PROJECT_VERSION_MAJOR})
endfunction()

saa_client_target(saaclient STATIC)
set(SAA_INSTALL_TARGETS saaclient)
if(SAA_BUILD_SHARED)
  saa_client_target(saaclient_shared SHARED)
  target_compile_definitions(saaclient_shared PUBLIC SAA_CLIENT_SHARED)
  set_target_properties(saaclient_shared PROPERTIES EXPORT_NAME saaclient_shared)
  list(APPEND SAA_INSTALL_TARGETS saaclient_shared)
endif()

# pkg-config. A static-only install must hand its dependencies to every consumer,
# so `pkg-config --libs saaclient` links; the shared library carries its own.
set(_saa_deps "-lpthread -lm")
if(SAA_ATOMIC_LIB)
  string(APPEND _saa_deps " -latomic")
endif()
set(_saa_requires "libwebsockets")
if(SAA_WITH_CAPTURE)
  string(APPEND _saa_requires " alsa")
endif()
if(SAA_BUILD_SHARED)
  set(SAA_PC_REQUIRES "Requires.private: ${_saa_requires}")
  set(SAA_PC_LIBS "")
  set(SAA_PC_LIBS_PRIVATE "Libs.private: ${_saa_deps}")
else()
  set(SAA_PC_REQUIRES "Requires: ${_saa_requires}")
  set(SAA_PC_LIBS "${_saa_deps}")
  set(SAA_PC_LIBS_PRIVATE "")
endif()
# The prefix is found from the .pc file's own location, so an install to any
# prefix (cmake --install --prefix, an unpacked tarball) stays correct.
file(RELATIVE_PATH SAA_PC_TO_PREFIX "${CMAKE_INSTALL_FULL_LIBDIR}/pkgconfig" "${CMAKE_INSTALL_PREFIX}")
string(REGEX REPLACE "/$" "" SAA_PC_TO_PREFIX "${SAA_PC_TO_PREFIX}")
configure_file(saaclient.pc.in ${CMAKE_CURRENT_BINARY_DIR}/saaclient.pc @ONLY)
install(TARGETS ${SAA_INSTALL_TARGETS} EXPORT saaclientTargets
  ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
  RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
install(DIRECTORY include/saa DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/saaclient.pc DESTINATION ${CMAKE_INSTALL_LIBDIR}/pkgconfig)

# CMake package: find_package(saaclient) gives saaclient::saaclient.
include(CMakePackageConfigHelpers)
set(SAA_CMAKE_DIR ${CMAKE_INSTALL_LIBDIR}/cmake/saaclient)
install(EXPORT saaclientTargets NAMESPACE saaclient:: DESTINATION ${SAA_CMAKE_DIR})
configure_package_config_file(cmake/saaclientConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/saaclientConfig.cmake INSTALL_DESTINATION ${SAA_CMAKE_DIR})
write_basic_package_version_file(${CMAKE_CURRENT_BINARY_DIR}/saaclientConfigVersion.cmake
  VERSION ${PROJECT_VERSION} COMPATIBILITY SameMinorVersion)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/saaclientConfig.cmake
              ${CMAKE_CURRENT_BINARY_DIR}/saaclientConfigVersion.cmake
        DESTINATION ${SAA_CMAKE_DIR})

# ── demo ──────────────────────────────────────────────────────────────────
if(SAA_BUILD_DEMO)
  add_executable(saa_client_demo examples/demo/main.c examples/demo/mjpeg_split.c)
  target_link_libraries(saa_client_demo PRIVATE saaclient Threads::Threads m)
  target_compile_definitions(saa_client_demo PRIVATE _POSIX_C_SOURCE=200809L
    $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
  target_compile_options(saa_client_demo PRIVATE ${SAA_WARNINGS})
  install(TARGETS saa_client_demo RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
  # the sample recording, once committed (tests/data/README.md)
  if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/examples/demo/sample_drive_thru.wav)
    install(FILES examples/demo/sample_drive_thru.wav DESTINATION ${CMAKE_INSTALL_DATADIR}/saa-c)
  endif()
endif()

# C++ is used only by the wrapper's example and tests; the library is C.
include(CheckLanguage)
check_language(CXX)
if(CMAKE_CXX_COMPILER)
  enable_language(CXX)
endif()

# The minimal integration, in C and in C++, built here too so it never goes
# stale (not installed).
if(SAA_BUILD_DEMO)
  add_executable(saa_minimal examples/minimal/main.c)
  target_link_libraries(saa_minimal PRIVATE saaclient)
  target_compile_definitions(saa_minimal PRIVATE _POSIX_C_SOURCE=200809L
    $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
  target_compile_options(saa_minimal PRIVATE ${SAA_WARNINGS})
  if(CMAKE_CXX_COMPILER)
    add_executable(saa_minimal_cpp examples/cpp/minimal.cpp)
    set_target_properties(saa_minimal_cpp PROPERTIES CXX_STANDARD 11 CXX_STANDARD_REQUIRED ON CXX_EXTENSIONS OFF)
    target_link_libraries(saa_minimal_cpp PRIVATE saaclient)
    target_compile_options(saa_minimal_cpp PRIVATE ${SAA_WARNINGS})
  endif()
endif()

# ── protocol spike (local only; not part of the package) ──────────────────
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/spike/m0_stream.c)
  add_executable(m0_stream spike/m0_stream.c)
  target_compile_definitions(m0_stream PRIVATE _POSIX_C_SOURCE=200809L _DARWIN_C_SOURCE
    SAA_BASE64_LEGACY_NAMES)
  target_compile_options(m0_stream PRIVATE -Wall -Wextra)
  target_link_libraries(m0_stream PRIVATE saa_third_party PkgConfig::SAAC_LWS m)
  if(OpenSSL_FOUND)
    target_link_libraries(m0_stream PRIVATE OpenSSL::SSL OpenSSL::Crypto)
  endif()
endif()

# ── tests ─────────────────────────────────────────────────────────────────
if(SAA_BUILD_TESTS AND EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/tests/CMakeLists.txt)
  enable_testing()
  add_subdirectory(tests)
endif()
