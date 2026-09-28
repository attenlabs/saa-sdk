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
endif()

find_package(Python3 COMPONENTS Interpreter)

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

# Feeder thread -> ring -> wakeup -> write, end to end.
add_executable(slice_feed integration/slice_feed.c)
target_link_libraries(slice_feed PRIVATE saaclient Threads::Threads m)
target_compile_definitions(slice_feed PRIVATE _POSIX_C_SOURCE=200809L
  $<$<PLATFORM_ID:Darwin>:_DARWIN_C_SOURCE>)
target_compile_options(slice_feed PRIVATE ${SAA_WARNINGS})
if(SAA_MOCK_TESTS)
  add_test(NAME slice_feed
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_slice.py
            $<TARGET_FILE:slice_feed> 3)
endif()
