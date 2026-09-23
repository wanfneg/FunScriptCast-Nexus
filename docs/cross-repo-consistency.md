# 双端复制文件一致性规约（FunScriptCast ↔ VRFunScriptCast）

> 背景（2026-09-22 三项目评审，发现 F32）：手机端 FunScriptCast 与头显端
> VRFunScriptCast 的 funscriptcore 有十余个核心文件是**全量手工复制**维护的，
> 且已实际分叉——VR 侧至少 4 处崩溃/安全修复（clamp 区间规范化、move 帧百分比
> 夹取、NaN 关键帧拦截、escapeJson 控制字符转义）未回移手机端，其中 clamp 崩溃
> 可永久终止手机端脚本同步。本文件记录：已知复制清单、分叉现状、回移记录与
> 后续维护规约，并配套一个**信息性**检查脚本（见第 4 节）。

## 1. 已知双端复制文件清单

两侧仓库根目录（本脚本默认假设与 Nexus 同级）：

- 手机端：`E:/Development/FunScriptCast`（下称 **phone**，源码根 `app/src/main/java/com/funscriptcast/`）
- 头显端：`E:/Development/VRFunScriptCast`（下称 **vr**，库源码根 `funscriptcore/src/main/java/com/funscriptcast/`）

| # | phone 路径 | vr 路径 | 说明 |
|---|---|---|---|
| 1 | `data/AiSubtitleEngine.kt` | `engine/AiSubtitleEngine.kt` | AI 字幕引擎（注释明言"移植自 VRFunScriptCast"） |
| 2 | `ble/BleDeviceService.kt` | `ble/BleDeviceService.kt` | BLE 设备服务 |
| 3 | `ble/DeviceProtocols.kt` | `ble/DeviceProtocols.kt` | BLE 协议（clamp/move帧/escapeJson） |
| 4 | `data/Funscript.kt` | `data/Funscript.kt` | .funscript 解析（NaN 关键帧防护） |
| 5 | `sync/SyncEngine.kt` | `sync/SyncEngine.kt` | 同步引擎 |
| 6 | `sync/QuickMoves.kt` | `sync/QuickMoves.kt` | 一键动作（持久化语义两侧刻意不同） |
| 7 | `sync/PresetDefs.kt` | `sync/PresetDefs.kt` | 预设定义（手机 26 个 vs VR 9 个） |
| 8 | `sync/PresetPlayer.kt` | `sync/PresetPlayer.kt` | 预设播放 |
| 9 | `data/DlnaClient.kt` | `data/DlnaClient.kt` | DLNA 客户端（/scripts 私有端点） |
| 10 | `data/SmbClient.kt` | `data/SmbClient.kt` | SMB 客户端（路径穿越防护） |
| 11 | `data/WebDavClient.kt` | `data/WebDavClient.kt` | WebDAV 客户端 |
| 12 | `data/ServeuApi.kt` | `data/ServeuApi.kt` | 通知/固件接口（scheme 校验只在 VR） |

## 2. 分叉现状快照（2026-09-22，`tools/check_cross_repo_consistency.py` 实测）

全部 12 对文件**内容均已不同**（逐字节比较；行数为 unified diff 两侧变更行合计）。
已知**有意**的分叉（改"一致"反而是错的）与**事故性**分叉（修复未回移，需要回移）：

| 文件 | 分叉量 | 类别 | 说明 |
|---|---:|---|---|
| DeviceProtocols.kt | 4 行 | 事故 | VR 已修 clamp lo>hi 崩溃（F29）、percent 夹取（F30）、escapeJson 控制字符（F33），手机仍是旧实现——**待回移** |
| Funscript.kt | 8 行 | 事故 | VR 已拦 NaN 关键帧（F31），手机未回移——**待回移** |
| ServeuApi.kt | 23 行 | 事故 | firmware_url scheme 校验与有界读取只在 VR（评审 F32 附带核实）——**待回移** |
| PresetDefs.kt | 362 行 | 有意 | 预设集不同：手机 26 个 vs VR 9 个 |
| QuickMoves.kt | 380 行 | 有意 | 手机持久化设置 vs VR"连接即重置"的安全语义 |
| AiSubtitleEngine.kt | 892 行 | 部分有意 | 平台差异（AudioSink 接线/缓存路径），但协议常量应两侧同步 |
| BleDeviceService.kt | 428 行 | 混合 | 平台差异 + 修复（BLE 写超时 F2 属手机侧新修） |
| 其余 5 对 | 51–491 行 | 混合 | 平台差异为主，需按文件逐处甄别 |

## 3. 本轮回移记录

| 日期 | 文件 | 方向 | 内容 |
|---|---|---|---|
| 2026-09-22 | — | — | 本轮（Nexus 修复轮）**未动**两端 Kotlin 代码，无回移。上表"待回移"三项保持原状，归属手机端仓库的修复任务（F29/F30/F31/F33 的处置），不在本仓库范围内 |

> 规约：以后任何一次跨仓库回移/同步，都必须在本表追加一行（日期、文件、方向、
> 一句话内容），保证"哪侧修了什么、搬没搬过"永远有账可查。

## 4. 信息性检查脚本

```bash
python tools/check_cross_repo_consistency.py            # 比对全部 12 对
python tools/check_cross_repo_consistency.py --phone <手机仓库根> --vr <头显仓库根>
```

- 逐文件输出 SHA256 一致/不一致，并给 diff 行数量级；
- **尽力而为**：任一仓库不存在时打印跳过原因，不算失败；
- **退出码仅提示，不作硬门禁**（0=全一致或无法比对；1=存在内容差异；2=脚本自身
  出错）。当前两侧本就处于"刻意分叉 + 待回移并存"状态，差异是预期结果，
  供回移前后对照用，不要把它接进会失败中止的 CI 门禁。

## 5. 后续维护规约

1. **改一侧必须评估另一侧**：碰上表 12 对文件里的任何一个，同一轮改动里要么
   同步另一侧（并在第 3 节记账），要么在第 2 节表中登记"有意分叉"及理由。
   禁止只改一侧且不留记录。
2. **协议/常量优先同步**：帧格式、clamp 区间、转义规则、分块/重叠常量、
   AI 字幕协议字段（如 `recommended_chunk_sec`、`X-FSC-Subtitle-Token`）属三端
   共同契约，两侧必须逐字符一致——这类文件分叉是事故不是演进。
3. **回移前先跑检查脚本**留基线，回移后再跑一次核对变化范围。
4. **长期方向**：把 funscriptcore 抽成双端共用的 Kotlin 模块（AAR），从根上消灭
   全量复制；在此之前，本文件与脚本就是唯一的一致性防线。
