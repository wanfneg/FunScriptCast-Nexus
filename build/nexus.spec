# -*- mode: python ; coding: utf-8 -*-
"""FunScriptCast-Nexus 单文件 EXE 打包配置。

设计取舍：
  * **EXE 只装应用本体**（宿主进程 + pywebview + DLNA 模块）。torch 约 4 GB、
    模型约 8 GB，绝不进 EXE——字幕服务继续以子进程方式调用 .venv 里的 Python。
  * `ui/` 与 `vendor/` 作为**外置数据**放在 EXE 旁边（不塞进 onefile 包），
    这样启动不用解压、改前端不用重打包。
  * 因此产物是「一个 EXE + 两个目录」，双击 EXE 即用。

构建：
    .venv\\Scripts\\python.exe -m PyInstaller build\\nexus.spec --noconfirm
    （或直接跑 build\\build_exe.ps1，它会顺带组装 dist-app/）
"""

from PyInstaller.utils.hooks import collect_data_files

# 必须先打补丁：Python 3.10.0 的 dis._unpack_opargs 有 bug，分析 bottle.py 时会崩。
# 见 build/_pyi_patch.py 的说明。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(SPEC)))
import _pyi_patch  # noqa: E402

_pyi_patch.apply()

block_cipher = None

hiddenimports = [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    "tkinter",              # 托盘右键菜单用
    "tkinter.font",
]

datas = collect_data_files("webview", subdir="lib")

a = Analysis(
    ["../host_server.py"],
    pathex=[".."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 这些是字幕服务（子进程）才需要的，不进 EXE
        "torch", "torchaudio", "torchvision", "transformers", "fastapi", "uvicorn",
        "gradio", "scipy", "sklearn", "pandas", "matplotlib", "av", "numba", "llvmlite",
        "numpy", "cv2", "onnxruntime", "modelscope", "funasr", "librosa", "soundfile",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="FunScriptCast-Nexus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 无控制台窗口；日志走文件与界面
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="../tools/icon.ico",
)
