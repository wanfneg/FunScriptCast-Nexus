# 第三方组件与许可声明（THIRD-PARTY-NOTICES）

> 本文件是 FunScriptCast-Nexus 的开源组件台账（SBOM）。配合《开源许可合规审查-R69.md》使用。
> 分发形态：安装包含 宿主 exe / embeddable Python runtime / ui / vendor / tools；
> **模型权重不进安装包**，由用户在界面内自行下载（setup.iss 明确不碰 models\）。

## 1. 随安装包分发的组件

| 组件 | 许可证 | 版权/来源 | 许可文本位置 | R69 整改 |
|---|---|---|---|---|
| Python 3.10 embeddable runtime | PSF-2.0 | Python Software Foundation | `runtime\LICENSE.txt`（随包 ✓） | 无 |
| tools/adb（adb.exe、AdbWinApi.dll、AdbWinUsbApi.dll） | Apache-2.0 | The Android Open Source Project | `tools\adb\LICENSE.txt` + `NOTICE` | ✅ 已补 |
| vendor/llama（llama.cpp 运行时：llama-server.exe、ggml/cublas 等 DLL） | MIT | (c) 2023 Georgi Gerganov 及贡献者 | `vendor\llama\LICENSE-llama.cpp`（✅ 已补）；`LICENSE-LLVM-OpenMP`（原有 ✓） | ✅ 已补 |
| ui/fonts/Inter | SIL OFL-1.1 | The Inter Project Authors (rsms) | `ui\fonts\LICENSE-Inter-OFL.txt` | ✅ 已补 |
| ui/fonts/JetBrains Mono | SIL OFL-1.1 | The JetBrains Mono Project Authors | `ui\fonts\LICENSE-JetBrainsMono-OFL.txt` | ✅ 已补 |
| ui/fonts/Space Grotesk | SIL OFL-1.1 | The Space Grotesk Project Authors | `ui\fonts\LICENSE-SpaceGrotesk-OFL.txt` | ✅ 已补 |
| ui 图标（index.html 内联 SVG sprite） | ISC | (c) 2026 Lucide Icons and Contributors | `ui\LICENSE-Lucide-ISC.txt`；生成脚本 `tools\gen_lucide_sprite.py` 逐字取自官方 svg | ✅ 已补 |
| 宿主 exe 打包器：PyInstaller | GPL-2.0-or-later **含运行时例外** | PyInstaller 贡献者 | 例外条款允许以任意许可分发打出的 exe；完整文本以 https://pyinstaller.org/en/stable/license.html 为准 | 无（例外覆盖） |
| WebView2 Runtime | Microsoft 免费可再分发组件 | Microsoft | 系统组件，按 Microsoft 重新分发条款 | 无 |
| 安装器：Inno Setup | Inno Setup License（免费，允许再分发） | Jordan Russell / Martijn Laan | 安装器构建工具，不在安装产物内 | 无 |

### runtime 内 Python 包（R69 清理后 29 个）

```
PyYAML==6.0.3 annotated-doc==0.0.5 annotated-types==0.8.0 anyio==4.15.1
certifi==2026.7.22(MPL-2.0) click==8.5.0 colorama==0.4.6 exceptiongroup==1.3.1
fastapi==0.141.1(MIT) filelock==4.0.0 fsspec==2026.7.0 h11==0.16.0(MIT)
hf-xet==1.6.0 httpcore==1.0.9 httpx==0.28.1(BSD-3) huggingface_hub==1.32.0(Apache-2.0)
idna==3.20 numpy==2.2.6(BSD) packaging==26.3(Apache-2.0/MIT) protobuf==7.36.2(BSD-3)
pydantic==2.13.5(MIT) pydantic_core==2.46.5(MIT) pyreadline3==3.5.6(BSD)
starlette==1.6.0(BSD-3) tomli==2.4.1(MIT) tqdm==4.70.1(MPL-2.0/MIT)
typing-inspection==0.4.4(MIT) typing_extensions==4.16.0(MIT) uvicorn==0.53.0(BSD-3)
```
（括号内为上游声明的许可证；未标注者以各 dist-info 元数据为准，均为宽松许可。）

### R69 移除记录（合规整改）

| 包 | 许可证 | 移除原因 | 功能影响 |
|---|---|---|---|
| zhconv 1.4.3 | **GPLv2+** | GPL 组件进入闭源安装包且被自有代码 import | 无——`free_translators._to_hans` 自带 ImportError 降级，免费兜底翻译默认关闭 |
| av 17.1.0（含 av.libs 的 FFmpeg LGPL DLL 约 15 个） | LGPL-2.1+（PyAV 官方 wheel 构建） | 代码零引用（R58 删 PyTorch 引擎后的遗留） | 无 |
| faster-whisper / ctranslate2 / onnxruntime / tokenizers / sympy / mpmath / flatbuffers / coloredlogs / humanfriendly | 均宽松许可 | R58 后死依赖，纯瘦身（合计约 170MB） | 无 |

## 2. 运行时由用户下载的组件（非本程序分发）

| 组件 | 许可证 | 下载源 |
|---|---|---|
| Qwen3-ASR-0.6B / 1.7B（识别模型） | Apache-2.0 | ModelScope: Qwen/Qwen3-ASR-* |
| Sakura-7B / 1.5B GGUF（日译模型） | Apache-2.0（以模型卡为准） | HF 镜像/第三方量化仓（catalog 内直链） |
| Hy-MT2-7B / 1.8B GGUF（英译模型） | **腾讯混元社区许可（非 OSI 自定义许可）**，≤1亿 MAU 免费商用 | ModelScope: Tencent-Hunyuan/Hy-MT2-*-GGUF |
| llama-runtime-windows.zip | MIT（llama.cpp） | 本仓库 GitHub Releases（zip 已含 MIT + LLVM-OpenMP 许可文本） |
| silero VAD | MIT | 随 audiocpp 运行时的 assets 分发 |

⚠️ 若分发形态从"引导用户自行下载"改为"打包进安装介质"，需重新评估（尤其混元社区许可）。

## 3. 仅开发机本地使用、未分发的组件

| 组件 | 许可证 | 说明 |
|---|---|---|
| audio.cpp（audiocpp-portable，E:\audiocpp-portable） | Apache-2.0（ShugoAI LLC） | ASR 运行时。当前通过 `asr.audiocpp.dir` 绝对路径引用本机副本；**纳入安装包/下载目录前必须随附其 LICENSE + NOTICE** |

## 4. 参考项目台账（未复制代码；仅思路/实现参考）

| 项目 | 许可证 | 参考程度 | 涉及文件 |
|---|---|---|---|
| realtime-subtitle（Vanyoo） | MIT | **参考实现逻辑**（切句规则 A/B 复现）与**参考思路**（context carryover、两级端点、partial 渐进出字、回显判据）；时间轴状态机/VAD 裁决/段-组对齐为本项目自有实现 | tests/whisper_scheme.py（注明出处）、hybrid_segmenter.py、server_app.py、audiocpp_backend.py、text_filters.py |
| VideoCaptioner（WEIFENG2333） | **GPL-3.0** | **仅参考思路**（批量 JSON 翻译 + 键校验纠错 + 免费兜底的模式）。零代码搬运，实现为独立编写（中文式 JSON 归一化、历史最好一轮、熔断/预算等均为本项目实测自有）→ 不构成 GPL 衍生 | translate_engine.py、free_translators.py（均只注明"思路参考"） |
| sub-title（chenzhaoxuan0） | MIT | 借鉴 RMS 静音切分思路（测试工具） | tests/stream_to_json.py |
| auto-caption | MIT | 架构调研，无可搬点 | 无代码涉及 |
| LiveSubtitles | GPL-2.0 | 纯架构调研（C++/Qt 项目，无代码路径交叉）；思想/架构不受版权保护 | 无代码涉及 |

## 5. 维护说明

- 新增第三方依赖/素材时：本表加一行 + 许可文本落盘到对应目录。
- runtime 包清单变更时更新第 1 节（可用 `runtime\python.exe -c "from importlib.metadata import distributions; ..."` 重新生成）。
- 目录内已随附的许可证文本一律**逐字取自上游官方文件**（OFL/Apache/ISC 均从上游仓库 raw 文件拷贝），不要手抄改写。
- 下载列表 UI 标签刻意不标许可证（保持界面素净），许可证信息以本文件为准。
