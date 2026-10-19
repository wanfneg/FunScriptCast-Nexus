# AI 动作识别：运行原理、置信度与静止门

> 本文回答三个问题：**模型怎么把画面变成 funscript 动作**、**能不能拿到置信度**、
> **能不能用置信度决定“这里该不该动”、阈值怎么定**。
> 所有数字来自 2026-10-19 的**全新重跑**（重新转码 + 重新提取 latent + 重新解码，
> 不依赖任何旧缓存），复现命令见 §7。

---

## 0. 结论速览

1. 模型每 1/30 秒产出一行，行上是 7 组轨道（速度/位置/包络/驻留/反转/极值/置信度）；
   最终动作由**样式合成**（不是分类头）决定。
2. **置信度 conf 存在，且逐行有效**：区分“人工脚本静止”与“运动”的 AUC
   = 0.953（K1cztm）/ 0.924（K1drsp）。它被官方 ONNX bundle 导出，但在 Python
   推理链路里被丢掉（forward.py 的 v, _conf, _gate = model(...)）。
3. 但 **conf 不是最好的静止判据**：|vmarg|（速度边际）AUC 0.966 / 0.934，
   两者组合 (vmarg/p50)×conf 达到 **0.980 / 0.948**。
4. **官方静止门偏松**：STILL_EPS = 22 pos/s。在人工脚本显式“保持不动”的区间，
   官方仍以 1.22（K1cztm）/ 1.90（K1drsp）动作/s 出点，而人工只有 0.30。
5. 把门提到 **50 pos/s**（约 2.3× 官方值）：平台内降到 **0.11 / 0.40 动作/s**，
   平台外只损失 **9% / 6%**。这就是“不该动的地方不动”的可落地参数。

---

## 1. 管线（逐级，全部按当前代码核对）

| 级 | 做什么 | 关键事实（代码位置） |
|---|---|---|
| 1 转码 | 480p / 30fps / x264 crf23，音频 AAC | project.TRANSCODE；同目录缓存复用 |
| 2 解码 | ffmpeg rgb24 管道，**384×384 @30fps** | enc.res=384（encoders.py:64）；grid_fps=30（project.py:33） |
| 3 编码 | V-JEPA 2.1 ViT-B（384px，patch 16 → 24×24 网格），冻结 fp16 | encoders.py:64；32 帧窗口、tubelet_stride=2、4 个时间对齐 |
| 4 降维 | 冻结的 PCA 白化取前 64 维 → int8（scale 8/127） | extract.py；每行 64×24×24 ≈ 36KB |
| 5 主干 | 前端 mask-net → h0（256 ch）→ 10 个膨胀 TCN 块 | DILATIONS 1..512（jepa_train.py:921）；每块 2 个 kernel-3 卷积 |
| 6 头部 | 7 组输出，见 §2 | jepa_train.py:495-542 |
| 7 解码 | 分块 1024 行 + 两侧各 1024 行真实上下文 | DECODE_CHUNK_S / DECODE_CTX_S = 34.13s |
| 8 样式 | style_positions_composed → 位置序列 | jepa_infer.py:470 |
| 9 出点 | extrema_actions（平滑后拐点 + 强制顶点 + RDP 1.0 + 剪缝 ease 250） | jepa_infer.py:735 |
| 10 限速 | clamp_speed(600 pos/s)，只改深度不改时间 | common.MAX_POS_RATE |

**感受野**：10 个膨胀块、每块 2 个 kernel-3 卷积，d=1..512
⇒ 1 + 4×(1+2+…+512) = **4093 行 ≈ 136 秒**。
解码只给两侧各 1024 行上下文，是感受野的一半（工程折中，不是完整 RF）。

---

## 2. 每个头贡献什么

| 头 | 输出 | 在样式里的作用 |
|---|---|---|
| head_v | 41 bin 速度分布 → 期望速度 v | **反转时刻**（v 过零）与**行程形态**（按 |v| 累积分配位置） |
| head_p | 位置分布 → level（0..100，pos_temp=0.25 锐化） | 冲程的水平位置 |
| head_e | 生成式包络 env（AR，ctx=16 行） | 冲程幅度：level ± min(env,100)/2 |
| head_plat | 3 类（none/top/bottom） | 驻留锁：把平台段钉在轨道上但保留纹波 |
| head_rev | 3 类（peak/valley/none） | Viterbi 反转事件 + 亚帧反转时间 |
| head_xlo/xhi | 极值轨道 | 端点贴边（EXT_SNAP，部署默认 0=关） |
| head_conf | 1 通道 sigmoid | **当前部署链路未使用**（见 §3） |

---

## 3. 置信度：存在、可用、但语义是“整段相关性”

### 3.1 是什么

    # jepa_train.py:534
    self.head_conf = nn.Conv1d(tcn_ch, 1, 1)
    # jepa_train.py:842
    conf = torch.sigmoid(self.head_conf(h)).squeeze(1)   # 逐行 0..1

训练损失（jepa_train.py:1562-1566）：

    corr_w = masked_pearson(pred, tgt, m).clamp(0, 1)   # 整段预测-真值的相关
    cm = (conf * m).sum(dim=1) / m.sum(dim=1).clamp_min(1)   # conf 的掩码均值
    l_conf = F.mse_loss(cm, corr_w)                      # W_CONF = 0.05

即：**约束的是 conf 的整段均值 ≈ 该段的 Pearson 相关**，不是逐点“对/错”。
但卷积头仍能从特征里学出逐行差异——实测它确实能分辨静止/运动（下表）。

### 3.2 官方导出，但 Python 链路丢弃

- 官方 ONNX bundle 把它作为逐行输出导出（export_bundle.py:308-310），名字 conf；
- 但 forward.predict_shots 内部是 v, _conf, _gate = model(...)（forward.py:91/102），
  collect_tracks 也不收 conf。
- 结论：**接出来零算力成本**（前向本来就算），只是没人把返回值放进 tracks。

### 3.3 实测区分度（两片，全新轨道）

真值：人工脚本的长平台（相邻关键帧 ≥3s 且 |Δpos| ≤2）= 静止行；
人工平滑速度 ≥20 pos/s = 运动行；只统计人工覆盖区间。

| 信号 | K1cztm AUC | K1drsp AUC | 运动中位 | 静止中位 |
|---|---|---|---|---|
| |vmarg| pos/s | **0.966** | **0.934** | 169.6 / 176.8 | 6.2 / 10.9 |
| **conf（head）** | **0.953** | **0.924** | 0.979 / 0.976 | 0.405 / 0.395 |
| plat 概率（取反） | 0.848 | 0.858 | — | — |
| rev 概率 | 0.426 | 0.476 | 弱 | 弱 |
| env/(|vmarg|+1) | 0.728 | 0.478 | 不稳定 | — |
| **(vmarg/p50)×conf** | **0.980** | **0.948** | 1.35 / 1.62 | 0.017 / 0.037 |

**回答**：能。conf 单独就是 0.92-0.95 的逐行判据；和速度组合后接近 0.98。
但 |vmarg| 更简单、略强，conf 更适合作为**第二票**（组合信号）。

---

## 4. 阈值怎么定

### 4.1 为什么不是简单乘 p50

两片的 p50|vmarg| 是 **120.9 / 100.2 pos/s**（只差 1.2 倍，且都在 30Hz 的 pos/s 量纲）。
旧文档说的“两片 p50 差 5 倍”来自坏缓存，不再成立。
所以**绝对阈值是可移植的**，不必强行用相对值。

### 4.2 固定阈值在两片上的动作级效果

门控方式：|vmarg| 9 点平滑后 < 阈值、且连续 ≥1 秒的 run 内抑制出点。

| 阈值 pos/s | K1cztm 平台内/s | K1cztm 平台外/s | K1drsp 平台内/s | K1drsp 平台外/s |
|---|---|---|---|---|
| 无门 | 1.29 | 3.34 | 2.06 | 4.12 |
| 22（官方 STILL_EPS） | 0.40 | 3.21 | 1.03 | 4.05 |
| 30 | 0.25 | 3.15 | 0.71 | 3.99 |
| 40 | 0.12 | 3.09 | 0.53 | 3.93 |
| **50** | **0.11** | **3.04** | **0.40** | **3.88** |
| 60 | 0.10 | 2.96 | 0.32 | 3.80 |
| 人工基准 | 0.30 | 3.28 | 0.30 | 4.24 |

**推荐：50 pos/s**（≈2.3× 官方 STILL_EPS）。
平台内 0.11/0.40（人工 0.30），平台外 -9%/-6%。
要更狠可用 60；要更保守用 40。

### 4.3 用 conf 直接当门（对照）

conf < 0.704（= 0.75×该片 conf p50）：平台内 0.38 / 0.59，平台外 3.11 / 3.65。
比 |vmarg| 门差一档（同平台内水平下，运动段损失更大）。

### 4.4 落点建议

- **A（推荐，改动小）**：在 _decode_chunk 里、样式合成**之前**，
  用累积 vmarg 算 still run，把 run 内的速度置零再交给官方样式机——
  复用官方 still 路径，不新增后处理；
- **B**：在发布前对 run 内动作做 hold 化（压成两端平台点）。更可控，但改变
  extrema_actions 的产物语义。
- 两方案都属于**改变输出行为**，上线前需确认。

---

## 5. 三个必须做对的解码细节（旧离线工具就是错在这）

| 细节 | 正确做法 | 做错的后果 |
|---|---|---|
| 速度量纲 | vel = nan_to_num(vmarg) * ck[v_std]（pos/s） | 所有片段判成 still，只出小碎步，动作数只有官方 1/4 |
| 行率 | 用缓存重绑 jepa_infer.FPS/DT | 平滑窗/行程/速度全错 2 倍 |
| 镜头切点 | 用 TransNetV2 真实切点做 edges | 单 shot 近似会改变驻留/反转的段边界 |

三条都做对后，离线解码与官方二进制的动作数对齐到 **0.2%**：

| 片子 | 官方 v0.6.0 | 新离线解码 |
|---|---|---|
| K1cztm | 3139 | 3133 |
| K1drsp | 3991 | 4002 |

---

## 6. 已知限制

- 置信度是整段相关的监督，跨片段比较时偏向“这段素材好不好读”，不是绝对概率。
- 门控是**规则**，不是模型能力：它无法区分“作者故意的小幅微动”和“模型抖动”，
  只能靠阈值权衡。
- 平台定义依赖人工脚本；人工未覆盖区（K1drsp 前 262s、K1cztm 前 350s）不能当真值。
- 以上都是离线解码的结论；流式实跑已验证与其一致（K1drsp 流式 10-17 会话：
  平台内 2.02 / 平台外 4.07，官方 1.90 / 4.16）。

---

## 7. 复现（全新跑，不用旧缓存）

    cd "E:/Development/FunScriptCast-Nexus/tools/vision"

    # 1) 转码 + 提取 latent（每片约 11 分钟，产物在 run/vision/）
    python extract_latent.py "E:/testvideo/funscript test/K1cztm.mp4" ../run/vision/K1cztm
    python extract_latent.py "E:/testvideo/funscript test/K1drsp.mp4" ../run/vision/K1drsp

    # 2) TransNetV2 切点 + 全片解码 + 收集 conf（约 2 分钟/片）
    python dump_tracks_conf.py "E:/testvideo/funscript test/K1cztm.mp4" ../run/vision/K1cztm
    python dump_tracks_conf.py "E:/testvideo/funscript test/K1drsp.mp4" ../run/vision/K1drsp

    # 3) 信号区分度 + 阈值
    python analyze_confidence_gate.py ../run/vision/K1cztm ../run/vision/K1drsp
