cmake_minimum_required(VERSION 3.20)
project(fsc_vision LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(fsc_vision SHARED src/engine.cpp)
target_include_directories(fsc_vision PUBLIC include)
target_compile_definitions(fsc_vision PRIVATE FSC_VISION_EXPORTS)
if (MSVC)
  # 源码/注释是 UTF-8，避免 C4819（代码页 936 下丢字符）
  target_compile_options(fsc_vision PRIVATE /utf-8)
endif()

# ORT/DirectML 依赖在阶段 1 接入：
#   find_package / 手工指向 NuGet Microsoft.ML.OnnxRuntime.DirectML
#   target_link_libraries(fsc_vision PRIVATE onnxruntime)
