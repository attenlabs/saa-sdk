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

if(SAA_WITH_CAPTURE)
  message(FATAL_ERROR "SAA_WITH_CAPTURE: the capture module is not implemented yet")
endif()

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
pkg_check_modules(LWS REQUIRED IMPORTED_TARGET libwebsockets)
message(STATUS "libwebsockets ${LWS_VERSION}")

# lws and openssl shenanigans
if(APPLE AND NOT OPENSSL_ROOT_DIR AND EXISTS /opt/homebrew/opt/openssl@3)
  set(OPENSSL_ROOT_DIR /opt/homebrew/opt/openssl@3)
endif()
find_package(OpenSSL)

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
  src/client.c
  src/clock.c
  src/log.c
  src/protocol.c
  src/resolver.c
  src/transport_lws.c
  src/url.c
  src/video_intake.c
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
    $<$<BOOL:${SAA_ALLOW_INSECURE_TLS}>:SAA_ALLOW_INSECURE_TLS>)
  target_compile_options(${name} PRIVATE ${SAA_WARNINGS})
  target_link_libraries(${name} PRIVATE PkgConfig::LWS Threads::Threads m ${SAA_ATOMIC_LIB})
  if(OpenSSL_FOUND)
    target_link_libraries(${name} PRIVATE OpenSSL::SSL OpenSSL::Crypto)
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
  list(APPEND SAA_INSTALL_TARGETS saaclient_shared)
endif()

configure_file(saaclient.pc.in ${CMAKE_CURRENT_BINARY_DIR}/saaclient.pc @ONLY)
install(TARGETS ${SAA_INSTALL_TARGETS}
  ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(DIRECTORY include/saa DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/saaclient.pc DESTINATION ${CMAKE_INSTALL_LIBDIR}/pkgconfig)

# ── demo ──────────────────────────────────────────────────────────────────
if(SAA_BUILD_DEMO)
  add_executable(saa_client_demo examples/demo/main.c)
  target_link_libraries(saa_client_demo PRIVATE saaclient Threads::Threads m)
  target_compile_definitions(saa_client_demo PRIVATE _POSIX_C_SOURCE=200809L
    $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
  target_compile_options(saa_client_demo PRIVATE ${SAA_WARNINGS})
  install(TARGETS saa_client_demo RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()

# ── protocol spike (local only; not part of the package) ──────────────────
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/spike/m0_stream.c)
  add_executable(m0_stream spike/m0_stream.c)
  target_compile_definitions(m0_stream PRIVATE _POSIX_C_SOURCE=200809L _DARWIN_C_SOURCE
    SAA_BASE64_LEGACY_NAMES)
  target_compile_options(m0_stream PRIVATE -Wall -Wextra)
  target_link_libraries(m0_stream PRIVATE saa_third_party PkgConfig::LWS m)
  if(OpenSSL_FOUND)
    target_link_libraries(m0_stream PRIVATE OpenSSL::SSL OpenSSL::Crypto)
  endif()
endif()

# ── tests ─────────────────────────────────────────────────────────────────
if(SAA_BUILD_TESTS AND EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/tests/CMakeLists.txt)
  include(CheckLanguage)
  check_language(CXX)
  if(CMAKE_CXX_COMPILER)
    enable_language(CXX)
  endif()
  enable_testing()
  add_subdirectory(tests)
endif()
