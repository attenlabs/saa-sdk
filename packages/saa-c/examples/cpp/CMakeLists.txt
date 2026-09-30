# The C++ minimal example, built against an installed saa-c:
#   cmake -S examples/cpp -B build-cpp -DCMAKE_PREFIX_PATH=/path/to/prefix
#   cmake --build build-cpp
cmake_minimum_required(VERSION 3.16)
project(saa_minimal_cpp CXX)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(saaclient REQUIRED)
add_executable(minimal_cpp minimal.cpp)
target_link_libraries(minimal_cpp PRIVATE saaclient::saaclient)
