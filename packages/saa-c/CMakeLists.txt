# saa-c: SAA cloud streaming client for C/C++ devices.
# M0 state: builds the spike only. The library targets land in M1.
cmake_minimum_required(VERSION 3.16)
project(saa_c VERSION 0.0.1 LANGUAGES C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(LWS REQUIRED IMPORTED_TARGET libwebsockets)
message(STATUS "libwebsockets ${LWS_VERSION}")

# Vendored: cJSON 1.7.18 (MIT), base64 (from SD_SAA_C).
add_library(saa_third_party STATIC
  third_party/cJSON/cJSON.c
  third_party/base64/base64.c)
target_include_directories(saa_third_party PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/cJSON
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/base64)
set_source_files_properties(third_party/cJSON/cJSON.c PROPERTIES COMPILE_FLAGS "-w")

# ── M0 spike ──────────────────────────────────────────────────────────────
add_executable(m0_stream spike/m0_stream.c)
target_compile_definitions(m0_stream PRIVATE _POSIX_C_SOURCE=200809L _DARWIN_C_SOURCE)
target_compile_options(m0_stream PRIVATE -Wall -Wextra)
target_link_libraries(m0_stream PRIVATE saa_third_party PkgConfig::LWS m)
