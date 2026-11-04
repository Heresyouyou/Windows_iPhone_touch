#!/bin/sh
# 在 macOS 或 Linux 上验证平台无关的核心（Windows 主机可直接用 CMake）。
# 本脚本是作者实际跑过的那条命令，不依赖 cmake。
set -e
cd "$(dirname "$0")"
CXX=${CXX:-clang++}
mkdir -p build
$CXX -std=c++17 -O2 -Wall -Wextra -Iinclude -Itests \
  src/hid_reports.cpp src/calibration.cpp src/geometry.cpp src/cursor_guard.cpp src/hogp_winrt.cpp \
  tests/test_core.cpp -o build/test_core
./build/test_core
