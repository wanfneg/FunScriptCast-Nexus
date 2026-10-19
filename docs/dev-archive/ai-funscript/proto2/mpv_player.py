"""mpv JSON IPC 客户端 —— 单条双工命名管道 + 同步请求-应答。

Windows 同步句柄同一时刻只允许一个 I/O 操作：读线程阻塞在 read() 时，
同句柄的 write() 会永久死锁。因此不做后台读线程——poll 频率只有 ~5Hz，
每次阻塞读几毫秒，单线程顺序收发即可。

用法:
    p = MpvPlayer(mpv_exe)
    p.launch(video_path)
    p.poll_pos() -> float | None
    p.paused() / p.rate() / p.set_pause(b) / p.seek_abs(t)
    p.is_running() / p.close()
"""
from __future__ import annotations

import json
import subprocess
import time

MPV_DEFAULT = r"C:\Program Files\MPV Player\mpv.exe"
PIPE_NAME = r"\\.\pipe\fsc-proto2-mpv"


class IpcDead(Exception):
    pass


class MpvPlayer:
    def __init__(self, mpv_exe: str = MPV_DEFAULT, pipe: str = PIPE_NAME):
        self.mpv_exe = mpv_exe
        self.pipe = pipe
        self.proc: subprocess.Popen | None = None
        self._fh = None
        self._buf = b""
        self._next_id = 1

    # ---- 生命周期 ----
    def launch(self, video: str, start: float | None = None, paused: bool = True,
               extra: list[str] | None = None) -> None:
        args = [
            self.mpv_exe, video,
            f"--input-ipc-server={self.pipe}",
            "--no-config", "--keep-open=yes",
            "--title=FSC-proto2",
            f"--pause={'yes' if paused else 'no'}",
        ]
        if start is not None:
            args.append(f"--start={start}")
        if extra:
            args += extra
        self.proc = subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        deadline = time.monotonic() + 15
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._fh = open(self.pipe, "r+b", buffering=0)
                break
            except (FileNotFoundError, OSError) as e:
                last_err = e
                time.sleep(0.2)
        if self._fh is None:
            raise RuntimeError(f"连不上 mpv IPC 管道: {last_err}")

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        try:
            if self._fh:
                self._fh.close()
        except OSError:
            pass
        self._fh = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    # ---- 同步请求-应答 ----
    def _read_line(self) -> bytes | None:
        """从缓冲/管道取一行；EOF 返回 None。事件行由调用方跳过。"""
        while b"\n" not in self._buf:
            try:
                chunk = self._fh.read(4096)
            except OSError as e:
                raise IpcDead(f"mpv IPC 读失败: {e}") from e
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def command(self, *cmd, timeout: float = 5.0):
        """发送命令/取属性，返回 (error, data)。事件行在等待中被跳过。"""
        if self._fh is None:
            raise IpcDead("mpv IPC 未连接")
        rid = self._next_id
        self._next_id += 1
        line = json.dumps({"command": list(cmd), "request_id": rid}) + "\n"
        try:
            self._fh.write(line.encode("utf-8"))
        except OSError as e:
            raise IpcDead(f"mpv IPC 写失败: {e}") from e
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self._read_line()
            if raw is None:
                raise IpcDead("mpv IPC 已断开（EOF）")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("request_id") == rid:
                return msg.get("error"), msg.get("data")
            # 事件行（无 request_id 或不匹配）跳过
        raise IpcDead(f"mpv IPC 响应超时: {cmd}")

    # ---- 高层 ----
    def poll_pos(self) -> float | None:
        try:
            err, data = self.command("get_property", "time-pos", timeout=2.0)
        except IpcDead:
            return None
        return data if (err == "success" and isinstance(data, (int, float))) else None

    def paused(self) -> bool:
        try:
            err, data = self.command("get_property", "pause", timeout=2.0)
            return bool(data) if err == "success" else False
        except IpcDead:
            return False

    def rate(self) -> float:
        try:
            err, data = self.command("get_property", "speed", timeout=2.0)
            return float(data) if err == "success" else 1.0
        except IpcDead:
            return 1.0

    def set_pause(self, value: bool) -> None:
        self.command("set_property", "pause", bool(value))

    def seek_abs(self, seconds: float) -> None:
        self.command("seek", float(seconds), "absolute")
