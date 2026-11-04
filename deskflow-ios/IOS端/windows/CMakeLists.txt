cmake_minimum_required(VERSION 3.16)
project(ios_screen CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 平台无关的核心：报文编码、校准曲线、几何、光标计数。
# 这部分在 macOS 上就能编译和跑单测，改完先在这里过一遍再上 Windows。
add_library(ios_screen_core STATIC
  src/hid_reports.cpp
  src/calibration.cpp
  src/geometry.cpp
  src/cursor_guard.cpp
)
target_include_directories(ios_screen_core PUBLIC include)

if(MSVC)
  target_compile_options(ios_screen_core PRIVATE /utf-8 /W4)
else()
  target_compile_options(ios_screen_core PRIVATE -Wall -Wextra)
endif()

enable_testing()
add_executable(test_core tests/test_core.cpp)
target_link_libraries(test_core PRIVATE ios_screen_core)
target_include_directories(test_core PRIVATE tests)
add_test(NAME core COMMAND test_core)

# Windows 专有部分：本仓库作者没有 Windows 环境，以下目标未经验证。
if(WIN32)
  add_executable(ios_screen WIN32_EXECUTABLE OFF
    src/main_win.cpp
    src/hogp_winrt.cpp
  )
  target_link_libraries(ios_screen PRIVATE ios_screen_core user32 gdi32)
  target_compile_options(ios_screen PRIVATE /utf-8)

  option(IOS_SCREEN_WITH_WINRT "启用 C++/WinRT 的 HOGP 发布端（需 Windows SDK 10.0.17763+）" OFF)
  if(IOS_SCREEN_WITH_WINRT)
    target_compile_definitions(ios_screen PRIVATE IOS_SCREEN_WITH_WINRT)
    target_link_libraries(ios_screen PRIVATE windowsapp)
  endif()
else()
  message(STATUS "非 Windows：只构建核心库与单测（ios_screen 可执行文件在 Windows 上才会生成）")
endif()
