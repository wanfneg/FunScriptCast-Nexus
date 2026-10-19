# -*- coding: utf-8 -*-
"""宿主端视觉依赖检查自检：按后端返回依赖，不启动应用、不拉起识别服务。"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import host_server as H  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))
    ok = ok and cond


tmp = Path(tempfile.mkdtemp(prefix="fsc_vision_host_"))
saved_dir = H.VISION_DIR
saved_env = os.environ.pop("FSC_VISION_BACKEND", None)
H.VISION_DIR = tmp
try:
    # 1) config 默认 onnx
    (tmp / "config.json").write_text(json.dumps({"engine": {"backend": "onnx"}}),
                                     encoding="utf-8")
    check("config=onnx → 后端 onnx", H._vision_backend() == "onnx", H._vision_backend())
    check("onnx 依赖 = numpy + onnxruntime",
          H._vision_deps() == ("numpy", "onnxruntime"), str(H._vision_deps()))
    check("onnx 依赖不含 torch", "torch" not in H._vision_deps(), str(H._vision_deps()))

    # 2) config 显式 torch
    (tmp / "config.json").write_text(json.dumps({"engine": {"backend": "torch"}}),
                                     encoding="utf-8")
    check("config=torch → 后端 torch", H._vision_backend() == "torch", H._vision_backend())
    check("torch 依赖含 torch", "torch" in H._vision_deps(), str(H._vision_deps()))

    # 3) 环境变量优先
    os.environ["FSC_VISION_BACKEND"] = "onnx"
    check("FSC_VISION_BACKEND=onnx 覆盖 config", H._vision_backend() == "onnx",
          H._vision_backend())
    (tmp / "config.json").write_text(json.dumps({"engine": {"backend": "onnx"}}),
                                     encoding="utf-8")
    os.environ["FSC_VISION_BACKEND"] = "torch"
    check("FSC_VISION_BACKEND=torch 覆盖 config", H._vision_backend() == "torch",
          H._vision_backend())
    os.environ.pop("FSC_VISION_BACKEND", None)

    # 4) 非法值 / 缺 config 都回退 onnx
    (tmp / "config.json").write_text(json.dumps({"engine": {"backend": "bogus"}}),
                                     encoding="utf-8")
    check("非法后端回退 onnx", H._vision_backend() == "onnx", H._vision_backend())
    (tmp / "config.json").unlink()
    check("缺 config 默认 onnx", H._vision_backend() == "onnx", H._vision_backend())

    # 5) 探测脚本按后端生成
    check("onnx 探测脚本含 numpy/onnxruntime",
          "onnxruntime" in H._vision_probe_script() and "numpy" in H._vision_probe_script(),
          H._vision_probe_script().splitlines()[2])
    check("torch 探测脚本含 torch", "torch" in H._vision_probe_script("torch"),
          H._vision_probe_script("torch").splitlines()[2])

    # 6) 用当前解释器实跑探测（只断言返回集合合法）
    miss = H._vision_missing_deps(Path(sys.executable), "onnx")
    have = all(importlib.util.find_spec(m) is not None for m in ("numpy", "onnxruntime"))
    if have:
        check("当前解释器满足 onnx 依赖", miss == [], str(miss))
    else:
        check("缺依赖时只报约定包名", set(miss) <= {"numpy", "onnxruntime"}, str(miss))

    # 7) 聚合状态暴露后端
    (tmp / "config.json").write_text(json.dumps({"engine": {"backend": "onnx"}}),
                                     encoding="utf-8")
    st = H.vision_state()
    check("vision_state 含 backend=onnx", st.get("backend") == "onnx", str(st.get("backend")))
    check("vision_state 未运行时不联网探测", st.get("running") is False, str(st.get("running")))

    # 8) 候选解释器按后端跳过依赖不全者（不真跑子进程）
    saved_app = H.APP_DIR
    saved_load = H.load_settings
    orig_missing = H._vision_missing_deps
    app = Path(tempfile.mkdtemp(prefix="fsc_vision_app_"))
    try:
        for rel in (Path("vision-runtime") / "Scripts" / "python.exe",
                    Path(".venv") / "Scripts" / "python.exe"):
            p = app / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")
        H.APP_DIR = app
        H.load_settings = lambda: {}
        os.environ.pop("NEXUS_VISION_PY", None)
        calls = []

        def fake_missing(py, backend=None):
            calls.append(str(py))
            return ["onnxruntime"] if "vision-runtime" in str(py) else []

        H._vision_missing_deps = fake_missing
        chosen = H._vision_python()
        check("跳过依赖不全的 vision-runtime", ".venv" in str(chosen), str(chosen))
        check("确实探测过候选解释器", len(calls) >= 2, str(calls))
    finally:
        H._vision_missing_deps = orig_missing
        H.APP_DIR = saved_app
        H.load_settings = saved_load
        shutil.rmtree(app, ignore_errors=True)
finally:
    H.VISION_DIR = saved_dir
    if saved_env is None:
        os.environ.pop("FSC_VISION_BACKEND", None)
    else:
        os.environ["FSC_VISION_BACKEND"] = saved_env
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("结果: " + ("ALL PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
