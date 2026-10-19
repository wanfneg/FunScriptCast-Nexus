# native/vision

原生视觉识别引擎（C++20 + ONNX Runtime + DirectML）。
设计、阶段和验收见 [docs/NATIVE-VISION-PLAN.md](../../docs/NATIVE-VISION-PLAN.md)。

当前状态：**阶段 0 骨架**——C ABI 与 CMake 已建，未接 ORT。

构建（VS2022 + CMake）：

```powershell
cmake -S native\vision -B native\vision\build -G "Visual Studio 17 2022" -A x64
cmake --build native\vision\build --config Release
```
