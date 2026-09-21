# -*- coding: utf-8 -*-
"""混合切句状态机（R63 立项实现）——RMS 静音定界 + silero VAD 赋时 + 段-组对齐。

R61/R62 的实测依据（详见 iteration_shturl.md）：
  · 定长块管线的时序好（+30~50ms）但句子被 3s 一刀剁碎（85% 配对句多碎片、
    长度比 1.5、块内 vad_filter 有气声漏识之嫌）；
  · RMS 参考方案的句界干净（长度比 1.00 / 零越界）但时间戳是模型预测的（−260~−470ms）；
  · R62 离线原型把两者拼起来：两片零漏识、时序 +72/+118ms、句界三件套全继承。

本模块把原型搬进服务端，**头显协议零改动**（音频照旧按 3s/1s 块 POST /transcribe）：

  请求进来 → 按绝对时间轴拼进滚动缓冲（重叠区丢弃、空洞补零、跳变重置）
           → 评估切句规则：缓冲 >2.0s 且尾部 1.0s RMS<0.01 → 定界切；
             缓冲 >5.0s → 硬切；整段全静音 → 丢弃
           → 切下的 span 交 VAD 量语音区、交 ASR 整段转写（whisper 需关 vad_filter）
           → ASR 段按中点对齐回 VAD 语音组，起止改用 VAD 实测值。

线程模型：/transcribe 可能并发进线程池，缓冲全部操作持锁；单客户端是既有事实
（_LAST_CTX 同样是全局单份），重置条件（跳变/换语言）兜底会话切换。

与离线原型的两处已知差异（都是流式化的必然代价，R63 有记录）：
  · 切句评估按请求粒度（头显 ~2s 一块），比原型的 0.2s 步进粗——只影响出字
    延迟（多等 0~2s），不影响时间戳（时间戳来自 VAD 实测语音起点）；
  · 硬切上限 5.0s 在请求间隙可能实际切在 ~5~7s。
"""
from __future__ import annotations

import threading

import numpy as np

from text_filters import join_tokens

SR = 16000

# realtime-subtitle 的切句口径（R61/R62 与参考项目对齐的那套默认值）
SILENCE_THRESHOLD = 0.01   # RMS，float [-1,1]
SILENCE_TAIL_SEC = 1.0     # 尾部静音判据长度
MIN_PHRASE_SEC = 2.0       # standard_cut 的最短缓冲
MAX_PHRASE_SEC = 5.0       # hard_cut 的上限
REGION_MERGE_GAP = 0.3     # VAD 语音区间隙小于此值并成同一句
MAX_SNAP_SEC = 0.6         # ASR 段中点离最近语音组超过此距离就保留 ASR 自报时间
JUMP_RESET_MS = 1000       # 请求起点比缓冲末端回跳超过此值 → 视为 seek/重连，重置
GAP_RESET_MS = 10000       # 请求起点比缓冲末端前跳超过此值 → 视为新会话，重置
LONG_SILENCE_TAIL_SEC = 0.4  # 两级端点：长缓冲的句尾静音确认（R63.4 选项①）
LONG_PHRASE_SEC = 4.0        # 缓冲超过此时长后启用长句级确认


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


class HybridBuffer:
    """滚动缓冲 + 切句决策（纯状态，不碰 ASR/VAD，方便单测）。

    feed() 返回 (span_pcm, span_start_ms, reason)：切下了完整句子就交给调用方
    去转写； (None, 0, "") 表示还在积累，本轮无出句。
    """

    def __init__(self, silence_threshold: float = SILENCE_THRESHOLD,
                 silence_tail_sec: float = SILENCE_TAIL_SEC,
                 min_phrase_sec: float = MIN_PHRASE_SEC,
                 max_phrase_sec: float = MAX_PHRASE_SEC,
                 long_silence_tail_sec: float = LONG_SILENCE_TAIL_SEC,
                 long_phrase_sec: float = LONG_PHRASE_SEC,
                 vad_fn=None):
        self.thr = float(silence_threshold)
        self.tail_sec = float(silence_tail_sec)
        self.min_sec = float(min_phrase_sec)
        self.max_sec = float(max_phrase_sec)
        # 两级端点（R63.4 选项①，realtime-subtitle main.py:165-175 同思路）：
        # 缓冲攒到 long_sec 之后，句尾静音确认从 tail_sec 降到 long_tail_sec——
        # 长句已经"押"了很多内容在缓冲里，早切 0.3s 的收益大于偶尔误切的风险。
        self.long_tail_sec = float(long_silence_tail_sec)
        self.long_sec = float(long_phrase_sec)
        # vad_fn(pcm) -> [(start_sec, end_sec)]（相对输入）：silero 语音区。
        # BGM 内容的整段 RMS 永远压不过静音阈值（R63.1 实测：全片切句全是
        # 5.2s 硬切），RMS 只配当免费第一优先，句尾停顿的最终裁判是 VAD——
        # 音乐不是语音，silero 的语音区间隙就是句间停顿。
        self.vad_fn = vad_fn
        self.last_cut_regions = None   # 切句时的 VAD 语音区（相对 span 秒），供对齐复用
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self._pcm = np.empty(0, dtype=np.float32)
        self._start_ms = 0     # 缓冲第一个采样的绝对时间
        self._next_ms = 0      # 缓冲末端之后应续上的绝对时间
        self._lang = ""

    def state(self) -> dict:
        return {"buffered_ms": int(len(self._pcm) / SR * 1000),
                "start_ms": self._start_ms, "lang": self._lang}

    def snapshot(self, min_sec: float = 1.5):
        """partial 用：当前缓冲的快照（拷贝 + 绝对起点）。不足 min_sec 回 None。"""
        with self._lock:
            if len(self._pcm) < int(min_sec * SR):
                return None
            return self._pcm.copy(), self._start_ms

    def feed(self, pcm: np.ndarray, lang: str, video_start_ms: int):
        """拼块 + 切句评估。见类注释。reason ∈ standard|hard|silent|""。

        时间轴规则（R63 回归实测教训）：切句/丢静音只清**音频**，`_next_ms`
        （时间轴末端）必须续着切点——头显的块带 1s 重叠，下一块的头部属于
        上一句，丢了切点就会把重叠区重复入账，整段起点标早 1s（实测
        中位 −1030ms、相邻 span 互相重叠 1s 的根源）。跳变（seek/换片）
        才走 reset() 全清。
        """
        with self._lock:
            lang = (lang or "").split("-")[0].strip().lower()
            if lang != self._lang:
                self.reset()
                self._lang = lang
            video_start_ms = int(video_start_ms)
            # 会话级重置：缓冲非空时的跳变；缓冲空但时间轴离新块太远（切句后
            # 隔了太久/seek）也一样全清
            if len(self._pcm) and (
                    video_start_ms < self._next_ms - JUMP_RESET_MS or
                    video_start_ms > self._next_ms + GAP_RESET_MS):
                self.reset()
                self._lang = lang
            elif not len(self._pcm) and self._next_ms and (
                    video_start_ms < self._next_ms - JUMP_RESET_MS or
                    video_start_ms > self._next_ms + GAP_RESET_MS):
                self.reset()
                self._lang = lang
            if not len(self._pcm):
                # 新缓冲的时间基点：没有时间轴时=块起点；有时间轴（切句/丢静音
                # 之后）时=时间轴末端——重叠块的头部属于上一句，空洞处则补零接上
                base = self._next_ms if self._next_ms else video_start_ms
                self._start_ms = base
                self._next_ms = base
            # 重叠区丢弃：只追加缓冲末端之后的新音频；空洞（丢块）补零保时间轴
            skip = max(0, self._next_ms - video_start_ms)
            if video_start_ms > self._next_ms:
                gap_n = int(round((video_start_ms - self._next_ms) / 1000 * SR))
                if gap_n > 0:
                    self._pcm = np.concatenate(
                        [self._pcm, np.zeros(gap_n, dtype=np.float32)])
                    self._next_ms += int(round(gap_n / SR * 1000))
            n = len(pcm) - int(round(skip / 1000 * SR))
            if n > 0:
                self._pcm = np.concatenate([self._pcm, pcm[len(pcm) - n:]])
                self._next_ms += int(round(n / SR * 1000))

            dur = len(self._pcm) / SR
            if dur < 0.5:
                return None, 0, ""
            # 切点裁决，三优先级：
            #   ① RMS 静音扫描（免费，纯安静内容秒判；两级端点——缓冲过 4s 后
            #      尾窗从 0.7s 收窄到 0.4s，长句早切）；
            #   ② VAD 语音区间隙（BGM 内容的真正裁判——音乐不是语音，语音区
            #      的间隙就是句间停顿；silero 每次喂块都跑，成本与生产旧管线
            #      的逐块 VAD 同级）；
            #   ③ 5.2s 硬切兜底（连续说话/两条判据都瞎时封顶延迟）。
            # 切点之后的残余音频**留在缓冲里**（可能已是下一句的头）。
            step_n = int(0.2 * SR)
            min_n = int(self.min_sec * SR)
            max_n = int(self.max_sec * SR)
            long_n = int(self.long_sec * SR)

            def _tail_n(pos_n: int) -> int:
                return (int(self.long_tail_sec * SR) if pos_n >= long_n
                        else int(self.tail_sec * SR))

            cut_n, reason = 0, ""
            i = min_n
            while i <= len(self._pcm):
                if i > max_n:
                    cut_n, reason = i, "hard"
                    break
                tn = _tail_n(i)
                if i >= tn and _rms(self._pcm[i - tn:i]) < self.thr:
                    cut_n, reason = i, "standard"
                    break
                i += step_n
            vad_regions = None
            if not cut_n and self.vad_fn is not None and dur >= self.min_sec:
                try:
                    vad_regions = self.vad_fn(self._pcm)
                except Exception:
                    vad_regions = None
                if vad_regions:
                    last_end = float(vad_regions[-1][1])
                    tail = (self.long_tail_sec if dur >= self.long_sec
                            else self.tail_sec)
                    pause = dur - last_end
                    if pause >= tail and last_end * SR >= int(tail * SR):
                        cut_n = min(int(round((last_end + 0.15) * SR)),
                                    len(self._pcm))
                        reason = "vad"
                elif vad_regions is not None and dur > max_n:
                    pass  # VAD 明确说整段无语音：交给下面的硬切路径处理
            if not cut_n and len(self._pcm) > max_n + step_n:
                cut_n, reason = max_n + step_n, "hard"
            if not cut_n:
                return None, 0, ""
            # 丢弃只在**切点**评估（参考项目同语义）：切下的这段整段 RMS<阈值
            # 才算真静音。不能每块都查——气声/轻声对白的块整段 RMS 也会低于
            # 阈值（实测 sivr002 被这样扔掉 205 块 ≈ 全片 1/3 语音）。
            if _rms(self._pcm[:cut_n]) < self.thr:
                self._pcm = self._pcm[cut_n:]
                self._start_ms += int(round(cut_n / SR * 1000))
                return None, 0, "silent"
            span, start_ms = self._pcm[:cut_n], self._start_ms
            self._pcm = self._pcm[cut_n:]
            self._start_ms += int(round(cut_n / SR * 1000))
            # _next_ms 续着时间轴末端（不清零）；残余缓冲从切点继续攒下一句
            self.last_cut_regions = (list(vad_regions)
                                     if (vad_regions and reason == "vad") else None)
            return span, start_ms, reason


def align_segments_to_groups(segs: list, groups_ms: list,
                             max_snap_ms: int = int(MAX_SNAP_SEC * 1000)) -> list:
    """ASR 段按中点对齐到 VAD 语音组，起止改用组时间；落不进任何组的段保留自报时间。

    groups_ms: [(start_ms, end_ms)]（已按间隙合并、升序）。
    对不上的常见原因：whisper 把一句话拆成多段 / VAD 组数与段数不等（R62 原型
    的"兜底"路径；这里用中点桶化取代原型的按序 zip，兜底率应显著下降）。
    """
    if not segs:
        return []
    if not groups_ms:
        return sorted(segs, key=lambda s: s.get("start_ms") or 0)
    buckets: list[list] = [[] for _ in groups_ms]
    loose: list = []
    for s in segs:
        s0 = int(s.get("start_ms") or 0)
        s1 = int(s.get("end_ms") or s0)
        mid = (s0 + s1) / 2
        hit = next((i for i, (ga, gb) in enumerate(groups_ms) if ga <= mid <= gb), None)
        if hit is None:
            # 中点落在语音组外（呼吸间隙/两端垫音）：吸附到最近的组
            dists = [min(abs(mid - ga), abs(mid - gb)) for ga, gb in groups_ms]
            i = dists.index(min(dists))
            hit = i if dists[i] <= max_snap_ms else None
        if hit is None:
            loose.append(s)
        else:
            buckets[hit].append(s)
    out: list = []
    for (ga, gb), bucket in zip(groups_ms, buckets):
        if not bucket:
            continue
        bucket.sort(key=lambda s: s.get("start_ms") or 0)
        # join_tokens（R68 审查修复）：中日文直接相连，英文/数字之间补空格——
        # 旧实现 "".join 会把英语 hybrid 模式同组的两段粘成 "helloworld"。
        out.append({"start_ms": int(ga), "end_ms": int(gb),
                    "text": join_tokens([(s.get("text") or "").strip()
                                         for s in bucket]).strip()})
    out.extend(loose)
    return sorted(out, key=lambda s: s.get("start_ms") or 0)


def merge_regions(regions, gap_sec: float = REGION_MERGE_GAP) -> list:
    """silero 语音区（秒）按间隙合并 → [(start_ms, end_ms)]。"""
    ms = [(int(a * 1000), int(b * 1000)) for a, b in regions if b > a]
    ms.sort()
    out: list = []
    for a, b in ms:
        if out and a - out[-1][1] <= int(gap_sec * 1000):
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]
