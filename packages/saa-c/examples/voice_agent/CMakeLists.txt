# The voice agent, built against an installed saa-c, from this source tree:
#   cmake -S examples/voice_agent -B build-va -DCMAKE_PREFIX_PATH=/path/to/prefix
#   cmake --build build-va
# It uses the vendored cJSON and base64 from third_party/, and the demo's WAV
# reader and MJPEG splitter, so it builds from a checkout, not from the install
# alone. saa-c's own build makes the same program as saa_voice_agent.
cmake_minimum_required(VERSION 3.16)
project(saa_voice_agent C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
find_package(saaclient REQUIRED)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(LWS REQUIRED IMPORTED_TARGET libwebsockets)

set(SAA_C ${CMAKE_CURRENT_SOURCE_DIR}/../..)
add_executable(saa_voice_agent main.c realtime.c playback.c resample.c
  ${SAA_C}/examples/demo/wav_reader.c ${SAA_C}/examples/demo/mjpeg_split.c
  ${SAA_C}/third_party/cJSON/cJSON.c ${SAA_C}/third_party/base64/base64.c)
target_include_directories(saa_voice_agent PRIVATE
  ${SAA_C}/examples/demo ${SAA_C}/third_party/cJSON ${SAA_C}/third_party/base64)
target_compile_definitions(saa_voice_agent PRIVATE _POSIX_C_SOURCE=200809L
  $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
target_link_libraries(saa_voice_agent PRIVATE saaclient::saaclient PkgConfig::LWS Threads::Threads m)

# libwebsockets' headers include OpenSSL's where lws was built with it
if(APPLE AND NOT OPENSSL_ROOT_DIR AND EXISTS /opt/homebrew/opt/openssl@3)
  set(OPENSSL_ROOT_DIR /opt/homebrew/opt/openssl@3)
endif()
find_package(OpenSSL)
if(OpenSSL_FOUND)
  target_include_directories(saa_voice_agent PRIVATE ${OPENSSL_INCLUDE_DIR})
endif()

# the ALSA speaker where ALSA is; the file speaker everywhere
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  pkg_check_modules(ALSA QUIET IMPORTED_TARGET alsa)
  if(ALSA_FOUND)
    target_link_libraries(saa_voice_agent PRIVATE PkgConfig::ALSA)
    target_compile_definitions(saa_voice_agent PRIVATE VA_WITH_ALSA)
  endif()
endif()
