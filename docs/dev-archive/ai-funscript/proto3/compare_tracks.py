# 逐行 diff: 官方轨道 vs 我们流式轨道(同视频同窗)
import numpy as np

off = np.load(r"compare\official_tracks.npz")
strm = np.load(r"compare\K1cztm_stream_final_tracks.npz")

w1 = 16380  # 546s x 30 rows
keys = ["vmarg", "level", "env", "blo", "bhi", "rev_top", "rev_bot"]
print(f"对比窗 0-{w1/30:.0f}s ({w1} 行):")
print(f"{'轨道':<10}{'官方absmax':>12}{'流式absmax':>12}{'官方std':>10}{'流式std':>10}{'相关系数':>10}")
for k in keys:
    o = np.nan_to_num(off[k][:w1].astype(np.float64))
    s = np.nan_to_num(strm[k][:w1].astype(np.float64))
    c = np.corrcoef(o, s)[0, 1] if o.std() > 0 and s.std() > 0 else 0.0
    print(f"{k:<10}{np.abs(o).max():>12.3f}{np.abs(s).max():>12.3f}"
          f"{o.std():>10.3f}{s.std():>10.3f}{c:>10.3f}")
print(f"\ntimes 对齐: 官方[1000]={off['times_ms'][1000]:.0f}ms 流式[1000]={strm['times_ms'][1000]:.0f}ms" if 'times_ms' in strm else "")
