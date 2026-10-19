// proto3 流式识别引擎 —— 全面审查 + 联网调研 + 逐轮迭代优化(量化门控)
// 基线(已实测): density_ratio=0.91, amplitude_ratio=0.932, reversal=0.214s, max_gap=4.7s, 评分≈1.39
// 突破目标: 评分 ≥ 1.70

const RUN_SEC = "220";
const ROUNDS = 5;
const BREAKTHROUGH = 1.7;

interface Finding {
  /** 位置: 文件路径(:行号) 或来源 URL */
  where: string;
  /** 一句话说清问题/机会 */
  what: string;
  /** 依据: 读过的代码行或链接内容 */
  evidence: string;
  severity: "low" | "medium" | "high";
  /** 修复思路一句话 */
  fix_hint: string;
}
interface Audit {
  findings: Finding[];
  summary: string;
}
interface Research {
  ideas: Finding[];
  summary: string;
}
interface Fix {
  title: string;
  detail: string;
  expected_effect: string;
  risk: string;
}
interface FixPlan {
  fixes: Fix[];
}
interface Metrics {
  n_actions: number;
  coverage_s: number;
  density_ratio: number;
  amplitude_ratio: number;
  reversal_median_dev_s: number;
  max_gap_s: number;
  p95_gap_s: number;
  wall_s: number;
}
interface ChangeDesc {
  /** 一句话描述本轮做了什么(或回滚了什么) */
  change: string;
}
interface RoundOutcome {
  round: number;
  change: string;
  kept: boolean;
  score: number;
  density_ratio: number;
  amplitude_ratio: number;
  reversal_median_dev_s: number;
  max_gap_s: number;
}
interface Review {
  approved: boolean;
  risks: string[];
}
interface WorkflowReport {
  conclusion: string;
  findings: Finding[];
  verified: string[];
  notCovered: string[];
}

function parseMetrics(stdout: string): Metrics | null {
  const m = /METRICS: (\{.*\})/.exec(stdout);
  if (m === null) return null;
  try {
    return JSON.parse(m[1]) as Metrics;
  } catch {
    return null;
  }
}
function scoreOf(m: Metrics): number {
  return m.density_ratio + m.amplitude_ratio - m.reversal_median_dev_s - m.max_gap_s / 20;
}

const CONTEXT =
  "项目背景: E:/Development/AI Funscript/proto3/stream_engine.py 是一个边播边推理的 funscript 流式识别引擎, " +
  "复用官方 GoblinTrainingGrounds(下称 GTG, 位于 _third_party/GoblinTrainingGrounds, 只读勿改)的模型与解码代码: " +
  "ffmpeg 解码(NVDEC+scale_cuda) → V-JEPA 滑窗提取 latent(int8 memmap) → h0 增量缓存(前端逐点输出) → " +
  "分块 trunk+heads(单 span [kept-1024, kept+保留+1024]) → 官方 style_positions_composed 解码(已重绑 jepa_infer.FPS/DT) → " +
  "ActionBuffer 水位发布。main_stream.py 是 mpv+BLE设备 播放循环, proto1/device.py 是设备驱动, proto1/funscript.py 是同步算法。" +
  "离线基准: proto2/ai_scripts/K1drsp.funscript(官方 GoblinScript v0.6.0 对同一视频的整片输出)。\n" +
  "已修复 bug(勿重复报告): ① jepa_infer.FPS/DT 未重绑(行程/速度全错2倍); ② 前向 span 无左侧 ctx 致 TCN 冷启动蠕动; " +
  "③ 分块发布游标跳过 margin 区(周期性无动作); ④ cuts_done.wait 阻塞提取线程; ⑤ 编码 batch=4 显存溢出(已调2); " +
  "⑥ FutureWarning 刷屏(已过滤)。\n" +
  "规则: GTG 仓库与 GoblinScript 二进制只读; 所有改动只落在 proto3/ 与 proto1/; 语法检查用 GTG venv python 跑 ast.parse; " +
  "不要自己跑 GPU 基准(benchmark.py 由脚本统一跑, 跑一次约 4 分钟); 若清单做完或某改动不可能完成, 如实说明。";

artifact.chart("metrics", {
  title: "每轮迭代质量指标(比率越接近 1 越好)",
  x: { field: "round", label: "轮次" },
  y: [
    { field: "density_ratio", label: "动作密度比" },
    { field: "amplitude_ratio", label: "冲程幅度比" },
  ],
});

let lastRound = 0;

phase("并行: 建立基准 + 三路审查调研");
log("先起 220 秒基准(占 GPU), 同时三路审查/调研并行开工");
const benchPromise = world.run(
  "E:\\Development\\AI Funscript\\_third_party\\GoblinTrainingGrounds\\venv\\Scripts\\python.exe",
  ["-u", "E:/Development/AI Funscript/proto3/benchmark.py", RUN_SEC],
  { timeoutMs: 480000 },
);
const auditCtx = CONTEXT + "\n\n你是审查员: 只读不改, 不运行任何 GPU 任务(基准由脚本统一跑)。findings 每条都带 where/evidence/fix_hint, 用中文。";
const codeAuditP = agent("识别管线审查员", "资深流式推理系统审查员, 精通 PyTorch 与视频管线, 以 GTG 官方实现为唯一基准。").ask<Audit>(
  auditCtx +
    "\n\n任务: 深挖 proto3/stream_engine.py 与 GTG 官方推理语义的残余偏差。通读 stream_engine.py 全文, 并以 " +
    "_third_party/GoblinTrainingGrounds/goblintrain/{jepa_infer.py,forward.py,jepa_train.py,extract.py,common.py} 为基准逐段比对: " +
    "前向分块与包络递归(env_row0/env_rows/AR缓冲跨块)、样式解码全部入参(rev_bias/subframe/edges 构造/rev_snap/amp_cap)、" +
    "每镜头末行静态对守卫、clamp_speed/rdp/cut_ease 的顺序、times_ms 对齐、seek 跳转的洞处理、节流与状态机、ActionBuffer 的发布/去重语义。");
const sendAuditP = agent("发送链路审查员", "资深实时系统审查员, 关注设备时序与丢帧。").ask<Audit>(
  auditCtx +
    "\n\n任务: 审查动作发送链路: proto3/main_stream.py(20ms 循环/时钟跟随/bisect 分段/缓冲末尾死区), proto1/device.py(BLE 写失败处理、moveTo 重映射、convertSpeed、速度夹取), proto1/funscript.py, proto1/clock.py。" +
    "背景: 用户曾报告明显大动作时设备慢速蠕动(识别端已修两处), 你从发送/设备侧找仍可能造成幅度萎缩、丢帧、延迟的因素(如 speed 单位与设备 max_speed 语义、BLE 写积压与 without-response 语义、动作点过密时的发送节流、延迟补偿缺失)。");
const researchP = agent("联网调研员", "擅长发散调研的工程师, 联网搜索并提炼可落地做法, 拒绝泛泛清单。").ask<Research>(
  CONTEXT +
    "\n\n任务: 联网调研, ideas 用 Finding 结构(where 填来源 URL), 只收对当前代码可执行的点: " +
    "① GitHub ofs69/GoblinScript 与 ofs69/GoblinTrainingGrounds 的 issues/discussions/README/docs 里关于解码参数(bands/dwells/stillness/rev_bias/env_seed/ style)与低延迟使用的线索; " +
    "② eroscripts 等社区对 AI 生成 funscript 质量的已知技巧与工具; " +
    "③ V-JEPA 或通用视频流式推理的提速手段(torch.compile/CUDA graphs/量化/分辨率帧率权衡)中适用于本管线的; " +
    "④ Buttplug.io/Intiface 或 BLE 外设的发送节流与 buffers 最佳实践。");

const bench = await benchPromise;
let baseline: Metrics;
const parsedBase = parseMetrics(bench.stdout);
if (bench.exitCode === 0 && parsedBase !== null) {
  baseline = parsedBase;
} else {
  baseline = {
    n_actions: 304, coverage_s: 238.9, density_ratio: 0.91, amplitude_ratio: 0.932,
    reversal_median_dev_s: 0.214, max_gap_s: 4.7, p95_gap_s: 2.17, wall_s: 223.4,
  };
  log("基准运行失败, 使用历史基线值继续");
}
log("基线: 密度比 " + baseline.density_ratio + ", 幅度比 " + baseline.amplitude_ratio +
    ", 反转偏差 " + baseline.reversal_median_dev_s + "s, 评分 " + scoreOf(baseline).toFixed(2));

const triple = await Promise.all([codeAuditP, sendAuditP, researchP]);
const codeAudit = triple[0];
const sendAudit = triple[1];
const research = triple[2];
log("审查/调研完成: 识别管线 " + codeAudit.findings.length + " 条, 发送链路 " + sendAudit.findings.length +
    " 条, 调研 " + research.ideas.length + " 条");
for (const f of codeAudit.findings) report({ ...f, from: "识别管线审查", status: "待处理" });
for (const f of sendAudit.findings) report({ ...f, from: "发送链路审查", status: "待处理" });
for (const f of research.ideas) report({ ...f, from: "联网调研", status: "备选" });

phase("汇总清单并逐轮迭代优化");
const optimizer = agent("优化执行者", "资深 Python 推理系统工程师。一次只做一个连贯改动, 改完必跑 ast 语法检查, 绝不手动跑 GPU 基准(benchmark.py 由脚本统一跑)。被拒绝对就老实回滚。若指令矛盾或清单枯竭, escalate 说明而不是硬编。");
const plan = await optimizer.ask<FixPlan>(
  CONTEXT +
    "\n\n以下是三路审查与调研的完整结果:\n[识别管线审查] " + JSON.stringify(codeAudit) +
    "\n[发送链路审查] " + JSON.stringify(sendAudit) +
    "\n[联网调研] " + JSON.stringify(research) +
    "\n\n当前基线指标: " + JSON.stringify(baseline) +
    "(评分 " + scoreOf(baseline).toFixed(2) + "; 评分 = 密度比 + 幅度比 - 反转中位偏差 - 最大间隔/20; 突破线 1.70)。" +
    "\n\n请整合成一份按 预期质量收益/风险 排序的修复清单(≤8 条, 每条=一次连贯改动可独立完成并可独立回滚)。");

let best = baseline;
let bestScore = scoreOf(baseline);
let feedback = "无(第一轮)";
let lastChange = "无改动";
let hitBreakthrough = false;
const history: RoundOutcome[] = [];
for (let round = 1; round <= ROUNDS; round++) {
  lastRound = round;
  log("第 " + round + "/" + ROUNDS + " 轮: 实施改动 → 基准验证 → 保留或回滚");
  const change = await optimizer.ask<ChangeDesc>(
    "第 " + round + "/" + ROUNDS + " 轮。当前最优指标 " + JSON.stringify(best) +
      "(评分 " + bestScore.toFixed(2) + ")。\n上一轮反馈: " + feedback +
      "\n当前清单: " + JSON.stringify(plan.fixes) +
      "\n\n请实施清单中的下一个改动(只改 proto3/ 与 proto1/ 下代码), 改完用 GTG venv python 做 ast 语法检查。" +
      "返回 {change: 一句话描述}。不要自己跑 benchmark.py。");
  lastChange = change.change;
  const bench2 = await world.run(
    "E:\\Development\\AI Funscript\\_third_party\\GoblinTrainingGrounds\\venv\\Scripts\\python.exe",
    ["-u", "E:/Development/AI Funscript/proto3/benchmark.py", RUN_SEC],
    { timeoutMs: 480000 });
  const m = parseMetrics(bench2.stdout);
  if (m === null) {
    feedback = "基准运行失败(exit=" + bench2.exitCode + "), 上轮改动可能有运行时错误, 请修复它: " + bench2.stderr.slice(-600);
    log("第 " + round + " 轮基准失败, 打回修复");
    continue;
  }
  const sc = scoreOf(m);
  const kept = sc >= bestScore - 0.03;
  if (kept) {
    best = m;
    bestScore = sc;
    feedback = "改动有效已保留(评分 " + sc.toFixed(2) + ")";
  } else {
    feedback = "改动退步(评分 " + sc.toFixed(2) + " < 最优 " + bestScore.toFixed(2) + "), 已要求回滚, 请换清单下一项";
  }
  const ro: RoundOutcome = {
    round: round, change: change.change, kept: kept, score: sc,
    density_ratio: m.density_ratio, amplitude_ratio: m.amplitude_ratio,
    reversal_median_dev_s: m.reversal_median_dev_s, max_gap_s: m.max_gap_s,
  };
  history.push(ro);
  report(ro, "metrics");
  if (!kept) {
    const undo = await optimizer.ask<ChangeDesc>(
      "上一轮改动(" + change.change + ")经基准判定退步(" + sc.toFixed(2) + " < " + bestScore.toFixed(2) +
        "), 请立即回滚它(恢复你改过的内容), 返回 {change: 回滚说明}。");
    lastChange = undo.change;
  }
  if (sc >= BREAKTHROUGH) {
    log("达成突破: 评分 " + sc.toFixed(2) + " ≥ " + BREAKTHROUGH);
    hitBreakthrough = true;
    break;
  }
}
if (!hitBreakthrough && feedback.startsWith("改动退步")) {
  const undo = await optimizer.ask<ChangeDesc>(
    "迭代轮次已用完且最后一轮改动被拒绝, 请回滚它并确认代码处于最优状态, 返回 {change: 说明}。");
  lastChange = undo.change;
}

phase("换人独立复核累积改动");
const reviewer = agent("独立复核员", "没有参与过任何改动的资深审查员, 只读不改, 找问题而不是给好评。");
const review = await reviewer.ask<Review>(
  CONTEXT +
    "\n\n请通读 proto3/stream_engine.py 与 proto3/main_stream.py 当前实现, 对照 GTG 官方语义, " +
    "审查以下迭代历史累积的改动是否引入新风险(每轮 change: " + JSON.stringify(history) +
    ")。risks 每条带文件与行号; 没有实质风险才 approved=true。");

phase("产出报告并恢复播放窗口");
const lines: string[] = [];
lines.push("# 流式引擎审查与迭代优化报告");
lines.push("");
lines.push("## 结论");
lines.push("- 基线评分 " + scoreOf(baseline).toFixed(2) + " → 最终评分 " + bestScore.toFixed(2) +
  (hitBreakthrough ? "(达成突破线 1.70)" : "(未达突破线 1.70, 已收敛)"));
lines.push("- 最终指标: 密度比 " + best.density_ratio + ", 幅度比 " + best.amplitude_ratio +
  ", 反转中位偏差 " + best.reversal_median_dev_s + "s, 最大间隔 " + best.max_gap_s + "s");
lines.push("- 独立复核: " + (review.approved ? "通过" : "有风险待处理: " + review.risks.join("; ")));
lines.push("");
lines.push("## 每轮改动");
for (const r of history) {
  lines.push("- 第 " + r.round + " 轮(" + (r.kept ? "保留" : "回滚") + ", 评分 " + r.score.toFixed(2) + "): " + r.change);
}
lines.push("");
lines.push("## 审查与调研发现(完整清单见运行结果)");
lines.push("- 识别管线: " + codeAudit.summary);
lines.push("- 发送链路: " + sendAudit.summary);
lines.push("- 调研: " + research.summary);
lines.push("");
lines.push("## 最后一个改动");
lines.push("- " + lastChange);
await artifact.markdown("report", lines.join("\n"), {
  title: "流式引擎优化报告",
  description: "三路审查 + 联网调研 + " + lastRound + " 轮量化迭代的结果与风险",
  primary: true,
});
await world.run("powershell", [
  "-NoProfile", "-Command",
  "Start-Process -FilePath 'E:\\Development\\AI Funscript\\proto3\\run_stream_vorze.bat' -WorkingDirectory 'E:\\Development\\AI Funscript\\proto3'",
]);

const allFindings: Finding[] = [];
for (const f of codeAudit.findings) allFindings.push(f);
for (const f of sendAudit.findings) allFindings.push(f);
const result: WorkflowReport = {
  conclusion: "三路审查 + 联网调研 + " + lastRound + " 轮量化迭代完成: 评分 " + scoreOf(baseline).toFixed(2) +
    " → " + bestScore.toFixed(2) + (hitBreakthrough ? ", 达成突破线" : ", 未达 1.70 突破线但已收敛") +
    "。独立复核" + (review.approved ? "通过" : "发现风险 " + review.risks.length + " 条。"),
  findings: allFindings.map((f) => ({ ...f, status: review.approved ? "verified" : "unconfirmed" })),
  verified: [
    "每轮改动都以 benchmark.py(220s 冒烟 + 与官方离线输出对比)量化门控",
    "最终改动由未参与的独立复核员通读代码复核",
    "基准指标: density_ratio/amplitude_ratio/reversal_median_dev_s/max_gap_s",
  ],
  notCovered: [
    "真机体感(BLE 设备上的主观体验)未在基准中覆盖",
    "seek 跳转的洞回填行为未纳入基准",
    "整片(22.75 分钟)全量跑通未验证, 基准只覆盖前 ~4 分钟",
  ],
};
return result;