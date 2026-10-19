"""三方对比: 原版哥布林 vs 流式哥布林(KEY) + 双方 vs 人工脚本。
用法: compare_three.py OFFICIAL STREAM HAND [LABEL]
窗口: ①流式覆盖∩官方覆盖(官方vs流式主对比) ②人工覆盖窗(三方 vs 人工)。
"""
import bisect
import json
import statistics
import sys


def load(p):
    return json.load(open(p, encoding="utf-8"))["actions"]


def win(xs, w0, w1):
    return [a for a in xs if w0 <= a["at"] <= w1]


def stats(xs, w0, w1):
    xs = win(xs, w0, w1)
    s = (w1 - w0) / 1000.0
    if len(xs) < 2 or s <= 0:
        return None
    amp = statistics.mean(abs(b["pos"] - a["pos"]) for a, b in zip(xs, xs[1:])
                          if b["at"] > a["at"])
    travel = sum(abs(b["pos"] - a["pos"]) for a, b in zip(xs, xs[1:])
                 if b["at"] > a["at"]) / s
    return {"n": len(xs), "density": len(xs) / s, "amp": amp, "travel": travel}


def rev_dev(a_actions, ref_actions, w0, w1):
    """a 的每个反转点到 ref 最近反转的中位偏差(ms)。"""
    refs = [a["at"] for a in win(ref_actions, w0, w1)]
    mine = [a["at"] for a in win(a_actions, w0, w1)]
    if not refs or not mine:
        return None
    ds = []
    for at in mine:
        i = bisect.bisect_left(refs, at)
        cand = [refs[j] for j in (i - 1, i) if 0 <= j < len(refs)]
        ds.append(min(abs(at - c) for c in cand))
    return statistics.median(ds)


def ratio(a, b):
    return round(a / b, 3) if b else None


def main():
    official = load(sys.argv[1])
    stream = load(sys.argv[2])
    hand = load(sys.argv[3])
    label = sys.argv[4] if len(sys.argv) > 4 else ""

    s_end = stream[-1]["at"]
    o0, o1 = official[0]["at"], official[-1]["at"]
    w0, w1 = max(o0, 0), min(s_end, o1)
    print(f"== {label} 官方vs流式 主对比窗: {w0/1000:.0f}-{w1/1000:.0f}s ==")
    so, ss = stats(official, w0, w1), stats(stream, w0, w1)
    dev = rev_dev(stream, official, w0, w1)
    if so and ss:
        print(f"密度:   官方 {so['density']:.2f}/s | 流式 {ss['density']:.2f}/s "
              f"(比 {ratio(ss['density'], so['density'])})")
        print(f"幅度:   官方 {so['amp']:.1f} | 流式 {ss['amp']:.1f} "
              f"(比 {ratio(ss['amp'], so['amp'])})")
        print(f"行程/s: 官方 {so['travel']:.1f} | 流式 {ss['travel']:.1f} "
              f"(比 {ratio(ss['travel'], so['travel'])})")
        print(f"流式反转→官方最近反转 中位偏差: {dev if dev is None else round(dev, 1)} ms")
    else:
        print("窗口数据不足")

    h0, h1 = hand[0]["at"], hand[-1]["at"]
    h0 = max(h0, w0)
    h1 = min(h1, s_end)
    print(f"-- 三方 vs 人工窗: {h0/1000:.0f}-{h1/1000:.0f}s --")
    ho, hs, hd = stats(official, h0, h1), stats(stream, h0, h1), stats(hand, h0, h1)
    if ho and hs and hd:
        print(f"密度:   人工 {hd['density']:.2f}/s | 官方 {ho['density']:.2f}/s "
              f"(比 {ratio(ho['density'], hd['density'])}) | 流式 {hs['density']:.2f}/s "
              f"(比 {ratio(hs['density'], hd['density'])})")
        print(f"幅度:   人工 {hd['amp']:.1f} | 官方 {ho['amp']:.1f} "
              f"(比 {ratio(ho['amp'], hd['amp'])}) | 流式 {hs['amp']:.1f} "
              f"(比 {ratio(hs['amp'], hd['amp'])})")
        print(f"行程/s: 人工 {hd['travel']:.1f} | 官方 {ho['travel']:.1f} "
              f"(比 {ratio(ho['travel'], hd['travel'])}) | 流式 {hs['travel']:.1f} "
              f"(比 {ratio(hs['travel'], hd['travel'])})")
        d_o = rev_dev(official, hand, h0, h1)
        d_s = rev_dev(stream, hand, h0, h1)
        fmt = lambda x: "n/a" if x is None else f"{x:.0f}ms"
        print(f"反转→人工 中位偏差: 官方 {fmt(d_o)} | 流式 {fmt(d_s)}")


if __name__ == "__main__":
    main()
