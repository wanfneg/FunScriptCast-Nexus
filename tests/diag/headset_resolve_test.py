# -*- coding: utf-8 -*-
"""头显侧路径 → PC 侧视频身份的解析回归测试。

为什么值得单独测：头显不知道 PC 上的绝对路径，它发过来的是
`file:///sdcard/Movies/a.mp4`（本地播放）或 DLNA 流 URL（`http://<pc>:8899/...`）。
这两者都不可能直接命中 PC 的缓存键，必须靠**按文件名在已知媒体根里反查**。

这一步一旦失效，表现是"头显看第二遍还是重跑一遍 ASR"——没有任何报错，
只是慢，非常难被发现（本次改动之前，头显连查缓存的请求都发不到 PC 上）。

用法： .venv\\Scripts\\python.exe tests\\diag\\headset_resolve_test.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

APP = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(APP))

import host_server as hs  # noqa: E402

_fails: list = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        _fails.append(label)


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hs_resolve_"))
    media = tmp / "media"
    media.mkdir()
    video = media / "FCVR-40-3.mp4"
    video.write_bytes(b"\x00" * 64)          # 内容无所谓，只测路径解析

    # 把"已知媒体根"换成临时目录，避免动用户真实配置
    hs.load_settings = lambda: {"dlna_roots": [str(media)], "video_folder": "", "script_folder": ""}
    print(f"临时媒体根: {media}")
    print(f"目标文件  : {video.name}\n")

    cases = [
        ("PC 绝对路径直通", str(video), str(video)),
        ("头显本地播放 file:// URI", "file:///sdcard/Movies/FCVR-40-3.mp4", str(video)),
        ("头显本地播放裸路径", "/sdcard/Movies/FCVR-40-3.mp4", str(video)),
        ("DLNA 流 URL", "http://192.168.2.2:8899/video/FCVR-40-3.mp4", str(video)),
        ("DLNA 流 URL（带查询串）", "http://192.168.2.2:8899/v/FCVR-40-3.mp4?sid=1", str(video)),
        ("未知文件名（应原样返回）", "file:///sdcard/Movies/nope.mp4", "file:///sdcard/Movies/nope.mp4"),
    ]
    for label, given, want in cases:
        got = hs._resolve_video_path(given)
        check(pathlib.Path(got) == pathlib.Path(want) or got == want, label,
              f"{given!r} → {got!r}")

    # 端到端：解析出的身份必须和 PC 侧直接算出来的缓存键一致
    k_device = hs.subtitle_cache_key(hs._resolve_video_path("file:///sdcard/Movies/FCVR-40-3.mp4"), "ja")
    k_pc = hs.subtitle_cache_key(str(video), "ja")
    check(k_device == k_pc, "设备侧 URI 与 PC 路径算出同一个缓存键",
          f"{k_device[:12]}… vs {k_pc[:12]}…")

    # DLNA 每次播放可能带不同的 sid/token。带着查询串算身份会让同一部片子每次
    # 都得到一个新键、命中率恒为 0，所以要归一化掉。
    k1 = hs.subtitle_cache_key(hs._resolve_video_path("http://192.168.2.2:8899/v/a.mp4?sid=1"), "ja")
    k2 = hs.subtitle_cache_key(hs._resolve_video_path("http://192.168.2.2:8899/v/a.mp4?sid=999"), "ja")
    check(k1 == k2, "带不同 sid 的 DLNA URL 归一到同一个缓存键",
          f"{k1[:12]}… vs {k2[:12]}…")

    # 头显存回来的缓存要能带上覆盖范围（cover_ms），否则读的一侧无法判断
    # "这份缓存看完了没有"
    saved = hs.subtitle_cache_save("file:///sdcard/Movies/FCVR-40-3.mp4", "ja",
                                   [{"start_ms": 0, "end_ms": 5000, "text": "a"},
                                    {"start_ms": 6000, "end_ms": 61_000, "text": "b"}],
                                   {"from": "test"})
    check(saved.get("ok") is True, "保存返回 ok", str(saved)[:100])
    got = hs.subtitle_cache_get("file:///sdcard/Movies/FCVR-40-3.mp4", "ja")
    check(got.get("hit") is True, "同一设备 URI 能立刻命中自己存的缓存")
    check(got.get("cover_ms") == 61_000, "回传 cover_ms", f"cover_ms={got.get('cover_ms')}")
    check(got.get("count") == 2, "段数正确", f"count={got.get('count')}")
    try:
        pathlib.Path(saved["path"]).unlink()
    except Exception:
        pass

    try:
        video.unlink()
        media.rmdir()
        tmp.rmdir()
    except Exception:
        pass
    print()
    if _fails:
        print(f"FAILED {len(_fails)} 项：" + "; ".join(_fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
