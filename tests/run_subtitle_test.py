# -*- coding: utf-8 -*-
"""对真实视频跑一遍 AI 字幕链路（日语 → 中文）。

流程：拉起 host_server → /api/subtitle/start 等 ready → 调 vendor/subtitle/tools/test_client.py
      按块抽音频 POST /transcribe → 打印 ASR/翻译结果与耗时 → 输出 SRT。

用法：
    .venv\\Scripts\\python.exe tests\\run_subtitle_test.py "D:\\path\\video.mp4" [分钟数]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PY = APP_DIR / ".venv" / "Scripts" / "python.exe"
CLIENT = APP_DIR / "vendor" / "subtitle" / "tools" / "test_client.py"
OUT_DIR = APP_DIR / "tests" / "subtitle_out"
LOG = APP_DIR / "tests" / "_subtest_log.txt"


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get(path: str, timeout: float = 10.0) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8790{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": repr(e)}


def post(path: str, payload: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:8790{path}", method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": repr(e)}


def main() -> int:
    if len(sys.argv) < 2:
        emit("用法: run_subtitle_test.py <视频> [分钟数]")
        return 2
    video = Path(sys.argv[1])
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    if not video.exists():
        emit(f"找不到视频：{video}")
        return 2

    # ffmpeg 只认 PATH：原先写得再全的兜底绝对路径也只对作者本机成立，
    # 换台机器/别人 clone 下来只会得到一个含个人用户名的误导路径，报错还不如直接说清。
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        emit("PATH 里找不到 ffmpeg：请先安装并加入 PATH（vendor 的 test_client 里写死的是 \"ffmpeg\"）")
        return 2
    emit(f"视频: {video.name}  时长测试上限: {minutes} 分钟  ffmpeg: {ffmpeg}")

    # 0) 端口必须空着，否则会打到别人的宿主（例如 dist-app 的 EXE）上
    import socket
    with socket.socket() as s:
        s.settimeout(1.0)
        if s.connect_ex(("127.0.0.1", 8790)) == 0:
            emit("8790 已被占用：先关掉正在运行的 FunScriptCast-Nexus / host_server")
            return 1

    # 1) 宿主
    host = subprocess.Popen([str(PY), str(APP_DIR / "host_server.py")], cwd=str(APP_DIR))
    try:
        st = {}
        for _ in range(60):
            time.sleep(0.5)
            st = get("/api/state")
            if "_error" not in st:
                break
        if "_error" in st:
            emit(f"宿主未就绪：{st['_error']}")
            return 1
        sys.path.insert(0, str(APP_DIR))
        import host_server as H  # 确认走的是源码目录而不是 dist-app
        emit(f"宿主就绪 | SUBTITLE_DIR={H.SUBTITLE_DIR} | PY={H.SUBTITLE_VENV_PY}")
        # 2) 字幕服务
        post("/api/subtitle/start", {})
        t0 = time.time()
        while time.time() - t0 < 180:
            time.sleep(3)
            st = get("/api/state")
            sub = st.get("subtitle", {})
            if sub.get("status") in ("ready", "error"):
                break
        emit(f"字幕服务: status={sub.get('status')} err={sub.get('error')}")
        if sub.get("status") != "ready":
            return 1
        emit(f"health: {json.dumps(sub.get('health'), ensure_ascii=False)}")
        emit(f"gpu: {json.dumps(st.get('gpu'), ensure_ascii=False)}")

        # 3) 测试客户端（把 ffmpeg 所在目录加进 PATH，vendor 脚本里写死的是 "ffmpeg"）
        env = dict(os.environ)
        env["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + env.get("PATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [str(PY), str(CLIENT), "--input", str(video), "--lang", "ja",
               "--url", "http://127.0.0.1:8756", "--chunk", "25", "--overlap", "2",
               "--max-minutes", str(minutes), "--out", str(OUT_DIR)]
        emit("$ " + " ".join(cmd))
        proc = subprocess.run(cmd, cwd=str(APP_DIR / "vendor" / "subtitle"),
                              env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.stdout:
            emit(proc.stdout.rstrip())
        if proc.stderr:
            emit("[stderr] " + proc.stderr.rstrip()[-2000:])
        emit(f"客户端退出码={proc.returncode}")

        # 4) 收尾：客户端退出码以前只打印不判定，失败照样报 PASS
        if proc.returncode != 0:
            emit(f"客户端非零退出：{proc.returncode}（上面的 traceback/stderr 才是根因）")
            return 1
        # 产物要真的有时间轴才叫字幕：0 段时 test_client 也会写出一个空 SRT，
        # 只判「文件存在」会把「整段没识别出东西」当成通过。
        out_srt = OUT_DIR / f"{video.stem}.srt"
        if not out_srt.exists():
            emit(f"没有产物 SRT：{out_srt}")
            return 1
        n_cues = sum(1 for ln in out_srt.read_text(encoding="utf-8", errors="replace").splitlines()
                     if "-->" in ln)
        if n_cues < 1:
            emit(f"产物 SRT 里没有一条时间轴（{out_srt}）：0 段也被判通过过，这里直接判失败")
            return 1
        emit(f"产物 SRT 共 {n_cues} 条字幕：{out_srt}")
        post("/api/subtitle/stop", {}, timeout=30)
        emit("字幕服务已停止（显存释放）")
    finally:
        try:
            post("/api/quit", {}, timeout=5)
        except Exception:
            pass
        time.sleep(3)
        if host.poll() is None:
            host.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
