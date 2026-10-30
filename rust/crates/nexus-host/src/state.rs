//! 宿主共享状态：设置、日志、设备层、服务句柄。

use std::sync::atomic::{AtomicBool, AtomicU64};
use std::sync::Arc;
use std::time::Instant;

use nexus_device::{build, DeviceArbiter, SERVEU, VORZE};
use serde_json::{json, Map, Value};

use crate::dlna_service::DlnaService;
use crate::logging::LogStore;
use crate::paths;
use crate::settings::Settings;

pub struct AppState {
    pub settings: Arc<Settings>,
    pub logs: Arc<LogStore>,
    /// 设备文件同步（脚本/视频 → Quest）的运行状态；与 `arb.sync`（动作时间轴）无关
    pub sync: Arc<crate::sync::SyncService>,
    pub arb: Arc<DeviceArbiter>,
    pub dlna: Arc<DlnaService>,
    pub library: Arc<nexus_library::Library>,
    pub subtitle: Arc<tokio::sync::Mutex<crate::subtitle_service::SubtitleService>>,
    pub vision: Arc<nexus_vision::service::VisionService>,
    /// DLNA 日志环（UI 的 DLNA 面板要读 200 条）
    pub dlna_logs: LogStore,
    /// 字幕状态的短缓存（2s）：/api/state 每秒被轮询，
    /// 不能每次都去探测端口、更不能排在长操作后面等锁。
    pub sub_cache: std::sync::Mutex<(Option<std::time::Instant>, serde_json::Value)>,
    /// 字幕服务是否正在后台启动（防重复拉起；Python `RT.sub_starting` 同款）。
    pub sub_starting: AtomicBool,
    /// 启动/停止代数：停止时递增，让在途启动任务完成后自行撤下。
    pub sub_gen: AtomicU64,
    /// 最近一次 subtitle_stop 的代数：在途启动作废的兜底 run_stop 只在
    /// "该 stop 是我之后唯一动作"时执行，防止误杀新启动刚拉起的进程
    /// （同配置下 start_parts 返回的是同一 backend Arc 实例）。
    pub sub_last_stop_gen: AtomicU64,
    /// 最近一次 run_start 的失败原因：字幕状态缓存的 error 字段透出真实原因，
    /// 固定派生文案（"字幕后端未全部就绪"）只做兜底。
    pub sub_last_error: std::sync::Mutex<String>,
    /// AI live 自愈节流：先开 AI 后连设备时，tick 每 5s 探一次并补 live
    pub live_heal_at: std::sync::Mutex<Instant>,
    pub started: Instant,
    pub version: String,
    pub port: u16,
}

impl AppState {
    pub fn new(version: impl Into<String>, port: u16) -> Arc<Self> {
        let _ = paths::ensure_dirs();
        let settings = Arc::new(Settings::load(paths::settings_file()));
        // 开机自启的真值在 HKCU Run 键里，不在设置文件中（Windows 设置/任务管理器
        // 侧关掉也要反映出来）。逐条对应 Python `load_settings` 的
        // `s["launch_on_boot"] = _autostart_enabled()`。
        // 测试进程不碰真实注册表：`cargo test` 不应依赖开发机是否装过本程序。
        #[cfg(all(windows, not(test)))]
        {
            let real = crate::autostart::enabled();
            if settings.get_bool("launch_on_boot").unwrap_or(false) != real {
                // 只回填内存：读注册表不该成为"重写设置文件"的理由
                settings.set_top_in_memory("launch_on_boot", json!(real));
            }
        }
        let settings_clone = Arc::clone(&settings);
        let logs = Arc::new(LogStore::new(Some(paths::host_log())));
        // 让没有 AppState 的地方（IPC 桥）也能写进同一份日志
        crate::logging::set_global(Arc::clone(&logs));

        let ch = Arc::new(nexus_device::DeviceChannel::new());
        let arb = Arc::new(build(ch, &paths::presets_file()));
        apply_device_settings(&settings, &arb);

        logs.info(format!("设置文件：{}", paths::settings_file().display()));
        logs.info(format!("预设：{} 个", arb.preset.presets().len()));
        // ONNX 运行库是视觉的硬依赖：**启动时就写进日志**，出问题时一眼能看出是缺库
        // 还是模型/推理的问题（build() 内部会把 ORT_DYLIB_PATH 指过去）。
        match crate::vision_service::resolve_ort_dylib() {
            Some(p) => logs.info(format!("ONNX 运行库：{}", p.display())),
            None => logs.warn("找不到 onnxruntime.dll：视觉推理不可用（安装包应带 runtime/ort/）"),
        }

        // GPU 探测（nvidia-smi / audio.cpp --list-devices）要同步跑子进程，
        // 启动即后台预热：字幕状态轮询（effective_backend）绝不持锁现算
        crate::subtitle_service::prewarm_gpu_probes(
            &paths::app_dir().join("vendor").join("audiocpp").join("vulkan").join("audiocpp_server.exe"),
        );

        Arc::new(Self {
            settings,
            logs,
            sync: Arc::new(crate::sync::SyncService::new()),
            arb,
            dlna: Arc::new(DlnaService::new()),
            library: Arc::new(nexus_library::Library::new(
                paths::data_dir().join("library_index.json"),
                paths::data_dir().join("library_thumbs"),
            )),
            vision: crate::vision_service::build(&settings_clone),
            subtitle: Arc::new(tokio::sync::Mutex::new(
                crate::subtitle_service::SubtitleService::new(
                    paths::data_dir().join("subtitle_config.json"),
                    paths::app_dir().join("models"),
                    paths::run_dir(),
                ),
            )),
            dlna_logs: LogStore::new(None),
            sub_cache: std::sync::Mutex::new((None, serde_json::json!(null))),
            sub_starting: AtomicBool::new(false),
            sub_gen: AtomicU64::new(0),
            sub_last_stop_gen: AtomicU64::new(0),
            sub_last_error: std::sync::Mutex::new(String::new()),
            live_heal_at: std::sync::Mutex::new(Instant::now()),
            started: Instant::now(),
            version: version.into(),
            port,
        })
    }

    pub fn uptime_secs(&self) -> f64 {
        self.started.elapsed().as_secs_f64()
    }
    /// 启动时刻的 Unix 秒（UI「关于」页显示）。
    pub fn started_at_epoch(&self) -> f64 {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        now - self.uptime_secs()
    }
}

/// 数值读取：数字、数字字符串都认（对齐 Python `_clamp_video_link` 里 `float(x)` 的口径）。
fn as_num(v: &Value) -> Option<f64> {
    v.as_f64().or_else(|| {
        v.as_str()
            .and_then(|s| s.trim().parse::<f64>().ok())
            .filter(|x| x.is_finite())
    })
}

/// 视频联动页数值夹紧（逐行对齐 Python `_clamp_video_link`，`host_server.py:542-573`）。
///
/// 只动认识且出现的键；非法值直接删键（读侧回退默认），**绝不注入新键**。
/// 夹紧后 `lo > hi` 的一对值交换 —— 否则 span 为负，所有动作速度会被静默归零。
pub fn clamp_video_link(vl: &Value) -> Value {
    let Some(obj) = vl.as_object() else {
        return json!({});
    };
    let mut out = obj.clone();

    for key in [
        "range_min", "range_max", "idle_min", "idle_max", "burst_min", "burst_max",
    ] {
        if let Some(v) = out.get(key).cloned() {
            match as_num(&v).map(|x| x.clamp(0.0, 100.0)) {
                Some(v2) => {
                    out.insert(key.to_string(), json!(v2));
                }
                None => {
                    out.remove(key);
                }
            }
        }
    }
    for (lo_k, hi_k) in [
        ("range_min", "range_max"),
        ("idle_min", "idle_max"),
        ("burst_min", "burst_max"),
    ] {
        let lo = out.get(lo_k).and_then(as_num);
        let hi = out.get(hi_k).and_then(as_num);
        if let (Some(lo), Some(hi)) = (lo, hi) {
            if lo > hi {
                out.insert(lo_k.to_string(), json!(hi));
                out.insert(hi_k.to_string(), json!(lo));
            }
        }
    }
    for key in ["max_speed", "idle_speed", "burst_speed"] {
        if let Some(v) = out.get(key).cloned() {
            match as_num(&v).map(|x| x.clamp(0.0, 500.0)) {
                Some(v2) => {
                    out.insert(key.to_string(), json!(v2 as i64));
                }
                None => {
                    out.remove(key);
                }
            }
        }
    }
    Value::Object(out)
}

fn copy_card_key(src: &Value, from: &str, dst: &mut Map<String, Value>, to: &str) {
    if let Some(v) = src.get(from) {
        if !v.is_null() {
            dst.insert(to.to_string(), v.clone());
        }
    }
}

/// `video_link` 的两张联动卡 → QuickMoves 的 `(orgasm, slow)` 补丁。
///
/// Python `_apply_device_settings` 里 `vl_cards` 的同款映射（`host_server.py:4899-4907`）：
/// 待机缓动读 `idle_*`，一键爆发读 `burst_*`；只输出出现的键，未出现的保持原值。
pub fn video_link_quick_cards(vl: &Value) -> (Value, Value) {
    let mut slow = Map::new();
    copy_card_key(vl, "idle_min", &mut slow, "min_percent");
    copy_card_key(vl, "idle_max", &mut slow, "max_percent");
    copy_card_key(vl, "idle_speed", &mut slow, "max_speed");
    copy_card_key(vl, "idle_link", &mut slow, "link_percent");

    let mut orgasm = Map::new();
    copy_card_key(vl, "burst_min", &mut orgasm, "min_percent");
    copy_card_key(vl, "burst_max", &mut orgasm, "max_percent");
    copy_card_key(vl, "burst_speed", &mut orgasm, "max_speed");
    copy_card_key(vl, "burst_link", &mut orgasm, "link_percent");
    copy_card_key(vl, "burst_speed_link", &mut orgasm, "link_speed");

    (Value::Object(orgasm), Value::Object(slow))
}

/// 把设置里的设备段应用到运行中的会话（对应 Python `_apply_device_settings`）。
///
/// 这里是"设置页改了立刻生效"的唯一入口：所有字段都从设置现读，不做缓存。
pub fn apply_device_settings(settings: &Settings, arb: &DeviceArbiter) {
    let dev = settings.get_path("device").unwrap_or_else(|| json!({}));
    let vl = clamp_video_link(&settings.get_path("video_link").unwrap_or_else(|| json!({})));

    // 行程/限速：video_link 是主设置；缺键回退默认（Python `vl.get(密钥, 默认)`）。
    let lo = vl.get("range_min").and_then(as_num).unwrap_or(0.0);
    let hi = vl.get("range_max").and_then(as_num).unwrap_or(100.0);
    let speed = vl
        .get("max_speed")
        .and_then(as_num)
        .map(|x| x as i32)
        .unwrap_or(500);
    // 反转在设置页属 `device.reversed`；兼容直接写在 video_link 里的旧数据。
    let rev = dev
        .get("reversed")
        .and_then(|v| v.as_bool())
        .or_else(|| vl.get("reversed").and_then(|v| v.as_bool()))
        .unwrap_or(false);
    arb.ch.apply_motion(Some(lo), Some(hi), Some(speed), Some(rev));

    // 伪装设备（A10 模式）：权威映射 ServeU=0 / VorzePiston=1（protocols.py:42-44）。
    if let Some(n) = dev.get("a10_mode").and_then(as_num).map(|x| x as i32) {
        match n {
            0 => arb.ch.set_forced_toy(SERVEU),
            1 => arb.ch.set_forced_toy(VORZE),
            // 非法值：解除强制档案，但 mode_override 仍按 Python 原样保留
            _ => {
                arb.ch.set_forced_toy_opt(None);
                arb.ch.set_mode_override(Some(n));
            }
        }
    }
    // 狂暴模式只改通道旗标；真正下发由 /api/device/settings 的保存路径完成。
    let oc = dev.get("oc_mode").and_then(|v| v.as_bool()).unwrap_or(false);
    arb.ch.set_oc_mode_flag(oc);

    // 快捷动作：先应用 device 旧键（兼容旧配置），再用 video_link 联动卡覆盖
    // —— Python 注释明确写着"视频联动页那两张卡才是这两个动作的主设置"。
    if let Some(o) = dev.get("orgasm") {
        arb.quick.apply(Some(o), None);
    }
    if let Some(s) = dev.get("slow") {
        arb.quick.apply(None, Some(s));
    }
    let (orgasm_card, slow_card) = video_link_quick_cards(&vl);
    arb.quick.apply(Some(&orgasm_card), Some(&slow_card));

    // 设置页只管"空闲判定秒数"，单独补上，不被联动卡覆盖；
    // 有效上限夹紧 1-60（手机端 QuickMoves.coerceIn(1,60)，UI 能填 600、实际生效 60）。
    if let Some(secs) = dev
        .get("slow")
        .and_then(|s| s.get("idle_detect_seconds"))
        .and_then(as_num)
    {
        let secs = (secs as i64).clamp(1, 60) as i32;
        arb.quick.set_slow_settings(|s| s.idle_detect_seconds = secs);
    }

    // 脚本同步：跳过无动作段 / 空闲判定阈值
    if let Some(v) = dev.get("skip_idle").and_then(|v| v.as_bool()) {
        arb.sync.set_skip_idle(v);
    }
    if let Some(v) = dev.get("idle_threshold").and_then(as_num) {
        arb.sync.set_idle_threshold(v);
    }

    // 预设速度（0 视为无效，与 Python `if dev.get("preset_speed")` 一致）
    if let Some(v) = dev.get("preset_speed").and_then(|v| v.as_i64()) {
        if v > 0 {
            arb.preset.set_speed(v as i32);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn apply_moves_settings_into_sessions() {
        let dir = std::env::temp_dir().join(format!("nexus-state-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.json");
        let _ = std::fs::remove_file(&p);
        let s = Settings::load(p);
        s.patch(&json!({
            "device": {"preset_speed": 150, "a10_mode": 1, "slow": {"idle_detect_seconds": 3}},
            "video_link": {"range_min": 10, "range_max": 90, "max_speed": 300}
        }))
        .unwrap();

        let ch = Arc::new(nexus_device::DeviceChannel::new());
        let arb = build(ch, &s_default_presets());
        apply_device_settings(&s, &arb);

        assert_eq!(arb.preset.speed(), 150);
        assert_eq!(arb.ch.motion().max_speed, 300);
        assert_eq!(arb.ch.motion().range_lo, 10.0);
        assert_eq!(arb.ch.motion().range_hi, 90.0);
        assert_eq!(arb.ch.mode_override(), Some(1));
        assert_eq!(arb.quick.settings().1.idle_detect_seconds, 3);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 联动页夹紧：0-100 行程、0-500 速度、lo>hi 交换、非法值删键。
    #[test]
    fn clamp_video_link_matches_python_rules() {
        let vl = clamp_video_link(&json!({
            "range_min": 90, "range_max": 10,
            "idle_speed": 600, "burst_speed": -5,
            "burst_max": "oops", "unknown_key": 1
        }));
        assert_eq!(vl["range_min"].as_f64(), Some(10.0), "lo>hi 必须交换");
        assert_eq!(vl["range_max"].as_f64(), Some(90.0));
        assert_eq!(vl["idle_speed"].as_i64(), Some(500), "速度上限 500");
        assert_eq!(vl["burst_speed"].as_i64(), Some(0), "负速度夹到 0");
        assert!(vl.get("burst_max").is_none(), "非法值必须删键");
        assert_eq!(vl["unknown_key"], 1, "不认识的键原样保留");
    }

    /// 两张联动卡只映射出现的键，不能把未出现的键写成 null。
    #[test]
    fn video_link_cards_only_include_present_keys() {
        let (o, s) = video_link_quick_cards(&json!({
            "idle_min": 20, "idle_max": 80, "idle_link": true,
            "burst_speed": 400, "burst_speed_link": true
        }));
        assert_eq!(s["min_percent"], 20);
        assert_eq!(s["max_percent"], 80);
        assert_eq!(s["link_percent"], true);
        assert!(s.get("max_speed").is_none(), "idle_speed 未出现不应注入");
        assert!(o.get("min_percent").is_none());
        assert_eq!(o["max_speed"], 400);
        assert_eq!(o["link_speed"], true);
    }

    /// 设备段 + 联动页 → 运行中的通道/同步/快捷动作（含 1-60 秒夹紧与 A10 档案）。
    #[test]
    fn apply_maps_video_link_and_device_prefs() {
        let dir = std::env::temp_dir().join(format!("nexus-state-vl-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.json");
        let _ = std::fs::remove_file(&p);
        let s = Settings::load(p);
        s.patch(&json!({
            "video_link": {
                "range_min": 15, "range_max": 85, "max_speed": 320,
                "idle_min": 20, "idle_max": 70, "idle_speed": 120, "idle_link": true,
                "burst_min": 10, "burst_max": 90, "burst_speed": 420,
                "burst_link": true, "burst_speed_link": true
            },
            "device": {
                "reversed": true, "a10_mode": 0, "oc_mode": true,
                "skip_idle": true, "idle_threshold": 42.5,
                "slow": {"idle_detect_seconds": 600}
            }
        }))
        .unwrap();

        let ch = Arc::new(nexus_device::DeviceChannel::new());
        let arb = build(ch, &s_default_presets());
        apply_device_settings(&s, &arb);

        let m = arb.ch.motion();
        assert_eq!((m.range_lo, m.range_hi, m.max_speed), (15.0, 85.0, 320));
        assert!(m.reversed, "device.reversed 必须应用到通道");
        assert_eq!(arb.ch.forced_toy().map(|t| t.id), Some("serveu"));
        assert_eq!(arb.ch.mode_override(), Some(0));
        assert!(arb.ch.oc_mode(), "oc_mode 必须落到通道旗标");

        let (o, sl) = arb.quick.settings();
        assert_eq!((sl.min_percent, sl.max_percent, sl.max_speed), (20, 70, 120));
        assert!(sl.link_percent);
        assert_eq!(sl.idle_detect_seconds, 60, "UI 填 600 实际生效 60");
        assert_eq!((o.min_percent, o.max_percent, o.max_speed), (10, 90, 420));
        assert!(o.link_percent && o.link_speed);

        let sy = arb.sync.state();
        assert!(sy.skip_idle);
        assert_eq!(sy.idle_threshold, 42.5);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn s_default_presets() -> std::path::PathBuf {
        crate::paths::presets_file()
    }
}
