// 全流程审查: 识别引擎 + 发送链路 + 性能剖析 + 联网调研 → 复核 → 优化路线图
// 上轮工作流遗留 22 条 unconfirmed 发现, 本轮: ① 验证它们在当前代码里是否属实
// ② 找清单之外的新问题 ③ 实测性能剖析 ④ 产出带优先级的优化路线图

interface Finding {
  /** 位置: 文件路径(:行号) 或来源 URL */
  where: string;
  /** 一句话说清问题/机会 */
  what: string;
  /** 依据: 读过的代码行、命令输出或链接内容 */
  evidence: string;
  severity: "low" | "medium" | "high";
  /** 修复思路一句话 */
  fix_hint: string;
  /** 上轮已列出=遗留; 本轮新发现=新增 */
  origin: string;
}
interface Audit {
  findings: Finding[];
  summary: string;
}
interface ProfileComp {
  /** 部件名, 如 "V-JEPA编码/h0构建/解码ffmpeg/TCN+heads/样式解码" */
  component: string;
  /** 占整条管线墙钟的百分比(估计) */
  share_pct: number;
  note: string;
}
interface ProfileReport {
  summary: string;
  breakdown: ProfileComp[];
  /** 引擎吞吐, 视频秒/墙钟秒 */
  throughput_x: number;
  /** 最值得优化的热点, 按序 */
  hotspots: string[];
}
interface Confirm {
  verdict: "confirmed" | "refuted" | "partial";
  evidence: string;
}
interface RoadmapItem {
  title: string;
  /** 质量/性能/体验/架构 */
  category: string;
  impact: string;
  effort: string;
  risk: string;
}
interface Roadmap {
  items: RoadmapItem[];
  quick_wins: string[];
  summary: string;
}
interface WorkflowReport {
  conclusion: string;
  findings: Finding[];
  verified: string[];
  notCovered: string[];
}

const KNOWN =
  "上轮遗留的未修复发现清单(先验证这些在当前代码是否属实, 再找清单外的新问题):\n" +
  "1.[high] publish 只增不替换 vs margin 重推: 剪缝在块界前窗内时重解码已发布区, 漂 1 行产生 33ms 双重反转 (publish 的 at 去重掩盖大部分)\n" +
  "2.[high] 吞吐仅 ~1.27x + 播放头贴 gen_until: 批间设备僵在上一顶点(已部分缓解: 发布节奏已改 256行/批+guard 32行, 需复测实际效果)\n" +
  "3.[high] BLE 写已加 try/except+重连(已修), 但设备位置 est 追赶在写失败时的行为未验证\n" +
  "4.[high→已改] --max-speed 默认已跟随设备上报 500(已修)\n" +
  "5.[med] rev_bias 每 30s 对全前缀跑 ~33 次 Viterbi 拟合且在提取/解码线程上, 成本线性增长停摆 gen 水位; 拟合窗口外的行因 nan_to_num 计入 still 带\n" +
  "6.[med] EOF 尾帧不补静态对、尾段 <2s 跳过: 片尾 1-4s 无动作\n" +
  "7.[med] 无端到端延迟补偿: offset 默认 0, BLE+电机+管线延迟无补偿; MediaClock 10% 渐进校正慢\n" +
  "8.[med] 20ms 循环已改 deadline sleep+IPC 合并(已修), 但 poll_pos 仍同步阻塞(2s 超时上限)\n" +
  "9.[med] 冻结恢复只按段均速发送不补偿积压距离——已加 est 追赶, 需验证正确性\n" +
  "10.[low] ffmpeg 中途死被当正常 EOF(官方特意防此); 11.[low] TransNet GPU 快路径死代码(max_s 恒传); " +
  "12.[low] 剪缝行换算 round vs 官方 searchsorted 差 0-1 行; 13.[low] 分块伪边界被当剪缝参与 cut_ease/slew; " +
  "14.[low] seek 跳转只写标志无消费者; 15.[low] 跨块速度 clamp 漏检; 16.[low] ctx 轨道未裁剪污染 rev_bias 统计; " +
  "17.[low] VFR 视频 total_rows 越界 IndexError; 18.[low] CUDA graph capture 与切分回填并发风险; " +
  "19.[low] banner mask 未应用; 20.[low] ActionBuffer.query 死代码; 21.[low] int() vs kround(已改 kround); " +
  "22.[low] 0x42 限制帧未下发(已修)";

const CONTEXT =
  "项目背景: E:/Development/AI Funscript/proto3/stream_engine.py 是边播边推理的 funscript 流式识别引擎, " +
  "复用官方 GTG(_third_party/GoblinTrainingGrounds, 只读勿改)的模型与解码代码: " +
  "ffmpeg 解码(NVDEC+scale_cuda, 回退CPU) → V-JEPA 滑窗提取(extract.py 对齐交织, batch=2) → latent int8 memmap + h0 增量缓存 → " +
  "分块 trunk+heads(前向 span [kept-1024, kept+256+1024]) → 官方 style_positions_composed(已重绑 FPS/DT) → ActionBuffer。" +
  "main_stream.py 是 mpv+BLE 播放循环(设备位置 est 追赶补偿), proto1/device.py 是 BLE 驱动。" +
  "离线基准: proto2/ai_scripts/K1drsp.funscript(官方 v0.6.0 整片输出)。测试视频 E:/testvideo/K1drsp.mp4(1080p50, 22.75分钟)。" +
  "运行环境: GTG venv python = E:/Development/AI Funscript/_third_party/GoblinTrainingGrounds/venv/Scripts/python.exe。" +
  "当前实测基线: 引擎吞吐 ~1.3x 实时, 识别质量为离线的 91-93%(密度/幅度), 反转偏差 0.21s, 评分 1.42(突破线 1.70 未达)。\n" +
  "规则: GTG 与 GoblinScript 二进制只读; 改动只允许落在 proto3/ 与 proto1/; " +
  "性能剖析员可以停掉 main_stream/mvp 实例(过滤 CommandLine 含 main_stream)独占 GPU, 但结束时不恢复窗口(脚本统一恢复); " +
  "审查员只读不改不跑 GPU。用户语言中文。\n" +
  KNOWN;

artifact.chart("profile", {
  title: "管线耗时分布(占比%)",
  x: { field: "component", label: "部件" },
  y: [{ field: "share_pct", label: "占比%" }],
});

phase("并行: 语义审查 + 时序审查 + 性能剖析 + 联网调研");
log("四路并行: 两路代码审查(只读), 一路实测性能剖析(独占GPU), 一路联网调研");
const semP = agent("官方语义对齐审查员", "资深推理系统审查员, 精通 PyTorch/视频管线, 以 GTG 官方实现为唯一基准, 找问题而非给好评。只读不改, 不跑 GPU。").ask<Audit>(
  CONTEXT +
    "\n\n任务: 通读 proto3/stream_engine.py 当前全文, 对照 _third_party/GoblinTrainingGrounds/goblintrain/{jepa_infer.py,forward.py,jepa_train.py,extract.py,common.py} 官方实现。" +
    "① 逐条验证上面遗留清单里的识别端条目(1,5,6,10,11,12,13,14,15,16,17,18,19)在当前代码是否属实(代码在最近两轮迭代中变过多处, 行号可能漂移, 以当前代码为准); " +
    "② 找清单之外的新偏差(发布节奏 256/32 改动后引入的新问题尤其要查: stable/e/edges 与 256 行块的配合、guard 32 行是否足够、rev_bias 拟合窗口与新节奏的交互)。findings 的 origin 填 遗留 或 新增。");
const timingP = agent("发送与时序审查员", "资深实时系统审查员, 关注端到端时序、设备语义与丢帧。只读不改, 不跑 GPU。").ask<Audit>(
  CONTEXT +
    "\n\n任务: 审查 proto3/main_stream.py + proto1/device.py + proto1/funscript.py + proto2/media_clock.py + proto2/mpv_player.py: " +
    "① 验证遗留清单 2,3,4,7,8,9,21(当前位置估计 est 追赶的正确性: 冻结恢复/seek/暂停后 est 的初值与收敛; catchup 速度是否会过冲; max-speed 跟随设备后的行为); " +
    "② 新增问题: 发布节奏 256 行(8.5s)/guard 32 行改动后, 播放端 bisect 分段与 缓冲末尾 死区的实际表现; BLE reconnect 循环与 est/last_index 的交互。origin 填 遗留 或 新增。");
const profP = agent("性能剖析员", "资深性能工程师, 用数据说话: 每个结论都附测量命令与输出。可以停掉 main_stream/mvp 实例独占 GPU, 结束时不恢复窗口。").ask<ProfileReport>(
  CONTEXT +
    "\n\n任务: 实测性能剖析, 回答 编码器/解码/TCN/样式解码/其余 各占多少: " +
    "① 先停掉 main_stream 与 mpv 实例(powershell 过滤 CommandLine 含 main_stream + mpv.exe); " +
    "② 写一个独立的 proto3/profile_run.py(不改 stream_engine.py 本体): import StreamEngine, 用 time.perf_counter 包装 _flush_group/_build_h0/_predict_shots_live 调用与样式段计时, 跑 150 秒, 每 20s 采样一次 nvidia-smi(util+mem); " +
    "③ 输出 breakdown(占比和为100)与 throughput_x(视频秒/墙钟秒), 给出 top 热点与可行动的优化建议(如: 某环节可并行/可省/可换实现)。");
const researchP = agent("联网调研员", "擅长发散调研的工程师, 联网搜索并提炼可落地做法, 拒绝泛泛清单。").ask<Audit>(
  CONTEXT +
    "\n\n任务: 联网调研, findings 的 where 填来源 URL, origin 填 调研: " +
    "① GitHub ofs69/GoblinScript 与 ofs69/GoblinTrainingGrounds 自 2026-10-05 以来的新提交/issue/discussion(尤其 six models/band 的用法与是否可下载); " +
    "② 社区(eroscripts 等)对流式/低延迟 funscript 生成的实践; " +
    "③ 视频流式理解(V-JEPA 或同类)降低推理成本的手段: token 合并/帧跳过/蒸馏/ONNX-DirectML/TensorRT; " +
    "④ funscript 平滑与设备命令整形(避免设备顶速饱和抖动)的已知做法。");

const sem = await semP;
const timing = await timingP;
const prof = await profP;
const research = await researchP;
for (const f of sem.findings) report({ ...f, from: "语义审查" });
for (const f of timing.findings) report({ ...f, from: "时序审查" });
for (const c of prof.breakdown) report(c, "profile");
log("四路完成: 语义 " + sem.findings.length + " 条, 时序 " + timing.findings.length +
    " 条, 剖析吞吐 " + prof.throughput_x + "x, 调研 " + research.findings.length + " 条");

phase("换独立复核员验证高优先级发现");
const confirmer = agent("独立复核员", "复核员的唯一职责: 用证据证实或证伪, 绝不顺着原结论说。只读不改, 不跑 GPU。每个 verdict 都要附你自己读到的代码行或命令输出。");
const highFindings = [...sem.findings, ...timing.findings].filter(
  (f) => f.severity === "high" || f.severity === "medium");
const confirmResults: { finding: Finding; confirm: Confirm }[] = [];
for (let i = 0; i < highFindings.length; i++) {
  const f = highFindings[i];
  const c = await confirmer.ask<Confirm>(
    "复核第 " + (i + 1) + "/" + highFindings.length + " 条发现(独立判断, 不要假设它对):\n" +
      JSON.stringify(f) + "\n\n亲自读代码/跑只读检查验证, verdict: confirmed(证据支持)/refuted(不成立)/partial(部分成立)。");
  confirmResults.push({ finding: f, confirm: c });
  report({ where: f.where, what: f.what, verdict: c.verdict, evidence: c.evidence });
}

phase("汇总优化路线图并产出报告");
const synthesizer = agent("路线图规划员", "资深架构师, 把发现变成可执行的路线图: 每项给影响/工作量/风险, 区分速赢与结构项。");
const roadmap = await synthesizer.ask<Roadmap>(
  CONTEXT +
    "\n\n以下是本轮全部材料:\n[语义审查] " + JSON.stringify(sem) +
    "\n[时序审查] " + JSON.stringify(timing) +
    "\n[性能剖析] " + JSON.stringify(prof) +
    "\n[联网调研] " + JSON.stringify(research) +
    "\n[复核结果] " + JSON.stringify(confirmResults.map((c) => ({ what: c.finding.what, verdict: c.confirm.verdict, evidence: c.confirm.evidence }))) +
    "\n\n产出优化路线图: items 按 收益/成本 排序(≤12 条, 每条 category∈{质量,性能,体验,架构}, impact 说清预期量化收益, effort∈{小时级,天级,周级}); " +
    "quick_wins 列出 3-5 条 小时级可完成且立刻见效 的; summary 三五句。被 refuted 的发现不要进路线图。");

const md: string[] = [];
md.push("# 全流程审查与优化路线图");
md.push("");
md.push("## 结论");
md.push("- " + prof.summary);
md.push("- 语义审查 " + sem.findings.length + " 条 / 时序审查 " + timing.findings.length + " 条 / 调研 " + research.findings.length + " 条; 高中优先级发现复核 " +
  confirmResults.filter((c) => c.confirm.verdict === "confirmed").length + " 条确认、" +
  confirmResults.filter((c) => c.confirm.verdict === "refuted").length + " 条证伪、" +
  confirmResults.filter((c) => c.confirm.verdict === "partial").length + " 条部分成立");
md.push("");
md.push("## 性能剖析");
for (const c of prof.breakdown) md.push("- " + c.component + ": " + c.share_pct + "% — " + c.note);
md.push("- 热点: " + prof.hotspots.join(" → "));
md.push("");
md.push("## 优化路线图");
for (const it of roadmap.items) {
  md.push("- [" + it.category + "] " + it.title + " — 影响: " + it.impact + " | 工作量: " + it.effort + " | 风险: " + it.risk);
}
md.push("");
md.push("## 速赢清单");
for (const q of roadmap.quick_wins) md.push("- " + q);
md.push("");
md.push("## 复核详情");
for (const c of confirmResults) {
  md.push("- [" + c.confirm.verdict + "] " + c.finding.where + " — " + c.finding.what + " | 复核: " + c.confirm.evidence.slice(0, 200));
}
await artifact.markdown("report", md.join("\n"), {
  title: "全流程审查与优化路线图",
  description: "四路并行审查 + 性能剖析 + 逐条复核 + 带优先级的优化路线图",
  primary: true,
});
await world.run("powershell", [
  "-NoProfile", "-Command",
  "Start-Process -FilePath 'E:\\Development\\AI Funscript\\proto3\\run_stream_vorze.bat' -WorkingDirectory 'E:\\Development\\AI Funscript\\proto3'",
]);

const allFindings: Finding[] = [
  ...sem.findings, ...timing.findings, ...research.findings,
];
const confMap = new Map(confirmResults.map((c) => [c.finding.what, c.confirm.verdict]));
const result: WorkflowReport = {
  conclusion: "四路并行审查完成: 吞吐 " + prof.throughput_x + "x, 热点 [" + prof.hotspots.join("; ") +
    "]。高/中优先级发现 " + highFindings.length + " 条中 " +
    confirmResults.filter((c) => c.confirm.verdict === "confirmed").length + " 条确认、" +
    confirmResults.filter((c) => c.confirm.verdict === "refuted").length + " 条证伪。路线图 " +
    roadmap.items.length + " 项(速赢 " + roadmap.quick_wins.length + " 条), 详见报告。",
  findings: allFindings.map((f) => ({
    ...f,
    status: confMap.get(f.what) === "confirmed" ? "verified" : "unconfirmed",
  })),
  verified: [
    "性能剖析: profile_run.py 实测 150s + nvidia-smi 采样",
    "高/中优先级发现逐条由独立复核员对照当前代码验证",
  ],
  notCovered: [
    "真机体感未测(无设备参与)",
    "整片 22.75 分钟全量未验证",
    "低优先级遗留发现(清单 10-22)未逐条复核",
  ],
};
return result;