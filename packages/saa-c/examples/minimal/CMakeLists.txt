# The minimal example, built against an installed saa-c:
#   cmake -S examples/minimal -B build-minimal -DCMAKE_PREFIX_PATH=/path/to/prefix
#   cmake --build build-minimal
cmake_minimum_required(VERSION 3.16)
project(saa_minimal C)

find_package(saaclient REQUIRED)
add_executable(minimal main.c)
target_link_libraries(minimal PRIVATE saaclient::saaclient)
