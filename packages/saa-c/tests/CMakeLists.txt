# Public headers: each compiles on its own, warning-free, as C99, C11, and
# C++11, and the values that make up the ABI stay where they are.
set(_hdr_flags -Wall -Wextra -pedantic)
if(SAA_WERROR)
  list(APPEND _hdr_flags -Werror)
endif()

foreach(std 99 11)
  add_executable(header_check_c${std} headers/header_check.c)
  set_target_properties(header_check_c${std} PROPERTIES
    C_STANDARD ${std} C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
  target_include_directories(header_check_c${std} PRIVATE ${PROJECT_SOURCE_DIR}/include)
  target_compile_options(header_check_c${std} PRIVATE ${_hdr_flags})
  add_test(NAME header_check_c${std} COMMAND header_check_c${std})
endforeach()

if(CMAKE_CXX_COMPILER)
  add_executable(header_check_cxx11 headers/header_check.cpp)
  set_target_properties(header_check_cxx11 PROPERTIES
    CXX_STANDARD 11 CXX_STANDARD_REQUIRED ON CXX_EXTENSIONS OFF)
  target_include_directories(header_check_cxx11 PRIVATE ${PROJECT_SOURCE_DIR}/include)
  target_compile_options(header_check_cxx11 PRIVATE ${_hdr_flags})
  add_test(NAME header_check_cxx11 COMMAND header_check_cxx11)

  # The C++ wrapper compiles alone as C++11 through C++20, and without exceptions.
  foreach(std 11 14 17 20)
    if(NOT "cxx_std_${std}" IN_LIST CMAKE_CXX_COMPILE_FEATURES)
      continue()
    endif()
    add_executable(header_check_hpp${std} headers/header_check_hpp.cpp)
    set_target_properties(header_check_hpp${std} PROPERTIES
      CXX_STANDARD ${std} CXX_STANDARD_REQUIRED ON CXX_EXTENSIONS OFF)
    target_link_libraries(header_check_hpp${std} PRIVATE saaclient Threads::Threads m)
    target_compile_options(header_check_hpp${std} PRIVATE ${_hdr_flags})
    add_test(NAME header_check_hpp${std} COMMAND header_check_hpp${std})
  endforeach()
  if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    add_executable(header_check_hpp_noexcept headers/header_check_hpp.cpp)
    set_target_properties(header_check_hpp_noexcept PROPERTIES
      CXX_STANDARD 11 CXX_STANDARD_REQUIRED ON CXX_EXTENSIONS OFF)
    target_link_libraries(header_check_hpp_noexcept PRIVATE saaclient Threads::Threads m)
    target_compile_options(header_check_hpp_noexcept PRIVATE ${_hdr_flags} -fno-exceptions)
    add_test(NAME header_check_hpp_noexcept COMMAND header_check_hpp_noexcept)
  endif()
endif()

# Unit tests: internal modules, no network (the resolver test looks up localhost).
function(saa_unit_test name)
  add_executable(${name} unit/${name}.c)
  target_include_directories(${name} PRIVATE ${PROJECT_SOURCE_DIR}/src ${CMAKE_CURRENT_SOURCE_DIR}/unit)
  target_link_libraries(${name} PRIVATE saaclient Threads::Threads m)
  target_compile_definitions(${name} PRIVATE _POSIX_C_SOURCE=200809L
    $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
  target_compile_options(${name} PRIVATE ${SAA_WARNINGS})
  add_test(NAME ${name} COMMAND ${name})
endfunction()

saa_unit_test(test_audio_intake)
saa_unit_test(test_backoff)
saa_unit_test(test_callback_guard)
saa_unit_test(test_capture)
saa_unit_test(test_jpeg)
saa_unit_test(test_log)
saa_unit_test(test_mjpeg_split)
target_sources(test_mjpeg_split PRIVATE ${PROJECT_SOURCE_DIR}/examples/demo/mjpeg_split.c)
target_include_directories(test_mjpeg_split PRIVATE ${PROJECT_SOURCE_DIR}/examples/demo)
saa_unit_test(test_resample)
target_sources(test_resample PRIVATE ${PROJECT_SOURCE_DIR}/examples/voice_agent/resample.c)
target_include_directories(test_resample PRIVATE ${PROJECT_SOURCE_DIR}/examples/voice_agent)
saa_unit_test(test_protocol)
saa_unit_test(test_resolver)
saa_unit_test(test_url)
saa_unit_test(test_video_intake)

# Capture builds: the ALSA source through ALSA's file plugin, which needs no
# sound card; and a probe for real devices, which is not a CTest.
if(SAA_WITH_CAPTURE)
  saa_unit_test(test_alsa_file)
  target_link_libraries(test_alsa_file PRIVATE PkgConfig::SAAC_ALSA)
  add_executable(capture_probe integration/capture_probe.c)
  target_include_directories(capture_probe PRIVATE ${PROJECT_SOURCE_DIR}/src)
  target_link_libraries(capture_probe PRIVATE saaclient Threads::Threads m)
  target_compile_definitions(capture_probe PRIVATE _POSIX_C_SOURCE=200809L)
  target_compile_options(capture_probe PRIVATE ${SAA_WARNINGS})
endif()

find_package(Python3 COMPONENTS Interpreter)

# The static library exports nothing outside saa-c's namespaces (sanitizer
# builds add their own globals, so the check is skipped there).
if(Python3_FOUND AND NOT SAA_SANITIZE)
  add_test(NAME symbols
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/check_symbols.py $<TARGET_FILE:saaclient> "${CMAKE_NM}")
endif()

# The sample recording meets its spec (tests/data/README.md), once it is committed.
if(Python3_FOUND AND EXISTS ${PROJECT_SOURCE_DIR}/examples/demo/sample_drive_thru.wav)
  add_test(NAME sample_recording
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/check_recording.py
            ${PROJECT_SOURCE_DIR}/examples/demo/sample_drive_thru.wav)
endif()

# Synthetic test audio: generated at test time, never committed.
if(Python3_FOUND)
  add_test(NAME gen_audio
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/gen_audio.py
            --check ${CMAKE_CURRENT_BINARY_DIR}/audio)
endif()

# Tests against the mock server need websockets 13 or newer: the mock uses its
# asyncio API, which older releases (Debian bookworm ships 10.4) do not have.
# Probe the module the mock imports, not just the package.
set(SAA_MOCK_TESTS OFF)
if(Python3_FOUND)
  execute_process(COMMAND ${Python3_EXECUTABLE} -c "import websockets.asyncio.server"
                  RESULT_VARIABLE _saa_ws_rc OUTPUT_QUIET ERROR_QUIET)
  if(_saa_ws_rc EQUAL 0)
    set(SAA_MOCK_TESTS ON)
  endif()
endif()
if(NOT SAA_MOCK_TESTS)
  set(_saa_ws_why "they need Python 3 with websockets 13 or newer \
(pip install \"websockets>=13\"); Python3_EXECUTABLE=${Python3_EXECUTABLE}")
  if(SAA_REQUIRE_MOCK_TESTS)
    message(FATAL_ERROR "SAA_REQUIRE_MOCK_TESTS is ON but the mock-server tests cannot run: ${_saa_ws_why}")
  endif()
  message(WARNING "skipping the mock-server tests: ${_saa_ws_why}")
endif()

# An integrator's view: install to a temporary prefix, then build the minimal
# example with pkg-config and with find_package (and stream, given the mock).
if(Python3_FOUND AND TARGET saa_minimal AND NOT CMAKE_CROSSCOMPILING AND NOT SAA_SANITIZE)
  set(_saa_consumer_args)
  if(CMAKE_CXX_COMPILER)
    list(APPEND _saa_consumer_args --cxx ${CMAKE_CXX_COMPILER})
  endif()
  if(SAA_MOCK_TESTS)
    list(APPEND _saa_consumer_args --run)
  endif()
  add_test(NAME consumer
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_consumer.py
            ${PROJECT_BINARY_DIR} ${CMAKE_C_COMPILER} ${_saa_consumer_args})
  set_tests_properties(consumer PROPERTIES TIMEOUT 180)
endif()

# Feeder thread -> ring -> wakeup -> write, end to end.
add_executable(slice_feed integration/slice_feed.c)
target_link_libraries(slice_feed PRIVATE saaclient Threads::Threads m)
target_compile_definitions(slice_feed PRIVATE _POSIX_C_SOURCE=200809L
  $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
target_compile_options(slice_feed PRIVATE ${SAA_WARNINGS})
# Resync, calls made while closed, callback order, and stop(), against the mock.
add_executable(client_harness integration/client_harness.c)
target_include_directories(client_harness PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/unit)
target_link_libraries(client_harness PRIVATE saaclient Threads::Threads m)
target_compile_definitions(client_harness PRIVATE _POSIX_C_SOURCE=200809L
  $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
target_compile_options(client_harness PRIVATE ${SAA_WARNINGS})

# The client's own capture, with fake devices, against the mock.
add_executable(capture_harness integration/capture_harness.c)
target_include_directories(capture_harness PRIVATE ${PROJECT_SOURCE_DIR}/src ${CMAKE_CURRENT_SOURCE_DIR}/unit)
target_link_libraries(capture_harness PRIVATE saaclient Threads::Threads m)
target_compile_definitions(capture_harness PRIVATE _POSIX_C_SOURCE=200809L
  $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
target_compile_options(capture_harness PRIVATE ${SAA_WARNINGS})

# saa::Client against the mock: a turn, a move, and a handler that throws.
if(CMAKE_CXX_COMPILER)
  add_executable(cpp_smoke integration/cpp_smoke.cpp)
  set_target_properties(cpp_smoke PROPERTIES CXX_STANDARD 11 CXX_STANDARD_REQUIRED ON CXX_EXTENSIONS OFF)
  target_link_libraries(cpp_smoke PRIVATE saaclient Threads::Threads m)
  target_compile_options(cpp_smoke PRIVATE ${SAA_WARNINGS})
endif()

# CPU and feed-to-wire latency, for device_report.py (timing: not a CTest).
add_executable(feed_bench integration/feed_bench.c)
target_link_libraries(feed_bench PRIVATE saaclient Threads::Threads m)
target_compile_definitions(feed_bench PRIVATE _POSIX_C_SOURCE=200809L
  $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
target_compile_options(feed_bench PRIVATE ${SAA_WARNINGS})

if(SAA_MOCK_TESTS)
  add_test(NAME slice_feed
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_slice.py
            $<TARGET_FILE:slice_feed> 3)
  add_test(NAME client_harness
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_harness.py
            $<TARGET_FILE:client_harness>)
  if(CMAKE_CXX_COMPILER)
    add_test(NAME cpp_smoke
      COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_with_mock.py $<TARGET_FILE:cpp_smoke>)
    set_tests_properties(cpp_smoke PROPERTIES TIMEOUT 120)
  endif()
  add_test(NAME capture_harness
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_capture.py
            $<TARGET_FILE:capture_harness>)
  set_tests_properties(capture_harness PROPERTIES TIMEOUT 120)
  set_tests_properties(client_harness PROPERTIES TIMEOUT 90)
  if(TARGET saa_voice_agent)
    # the voice agent against the SAA mock and the Realtime mock (about 25 s)
    add_test(NAME voice_agent
      COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_voice_agent.py $<TARGET_FILE:saa_voice_agent>)
    set_tests_properties(voice_agent PROPERTIES TIMEOUT 90)
  endif()
  if(TARGET saa_client_demo)
    # the demo against every scripted mock scenario (about 35 s, run in parallel);
    # the TLS scenarios need the openssl command, which CI must have
    set(_saa_conf_args)
    if(SAA_REQUIRE_MOCK_TESTS)
      list(APPEND _saa_conf_args --require-tls)
    endif()
    add_test(NAME conformance
      COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/conformance.py
              $<TARGET_FILE:saa_client_demo> ${_saa_conf_args})
    set_tests_properties(conformance PROPERTIES TIMEOUT 150)
  endif()
endif()
