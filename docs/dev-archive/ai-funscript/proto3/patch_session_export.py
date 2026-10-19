# 会话数据导出补丁: main_stream 退出时保存 轨道/动作/运动能量/元数据
p = "main_stream.py"
src = open(p, encoding="utf-8").read()

def rep(old, new):
    global src
    assert src.count(old) == 1, f"{src.count(old)}x: {old[:60]!r}"
    src = src.replace(old, new)

rep(
    "import argparse\nimport asyncio\nimport bisect\nimport os\nimport sys",
    "import argparse\nimport asyncio\nimport bisect\nimport os\nimport sys\nfrom datetime import datetime\nfrom pathlib import Path")

rep(
    """    finally:
        print()
        engine.stop(cleanup=True)""",
    """    finally:
        print()
        # 会话数据导出: 每次运行都留下可离线分析的数据
        try:
            sess_dir = Path(__file__).resolve().parent / "sessions" / \\
                datetime.now().strftime("%Y%m%d_%H%M%S")
            sess_dir.mkdir(parents=True, exist_ok=True)
            import numpy as np
            trk = getattr(engine, "_trk", None)
            if trk:
                np.savez(sess_dir / "tracks.npz", **trk,
                         motion=np.asarray(engine.motion),
                         times_ms=np.asarray(engine.times_ms))
            actions, gen_until = engine.buffer.snapshot()
            json.dump(
                {"video": args.video, "device": args.device,
                 "n_actions": len(actions), "gen_until_ms": gen_until,
                 "coverage_s": actions[-1]["at"] / 1000.0 if actions else 0.0,
                 "actions": actions},
                open(sess_dir / "actions.json", "w", encoding="utf-8"),
                ensure_ascii=False, indent=1)
            meta = {"video": args.video, "delay_ms": args.delay_ms,
                    "frames_sent": frames, "engine_state": engine.buffer.state,
                    "note": engine.buffer.note}
            json.dump(meta, open(sess_dir / "meta.json", "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print(f"会话数据已存: {sess_dir}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"会话导出失败(不影响退出): {e}", flush=True)
        engine.stop(cleanup=True)""")

open(p, "w", encoding="utf-8", newline="\n").write(src)
print("会话导出已实现")
