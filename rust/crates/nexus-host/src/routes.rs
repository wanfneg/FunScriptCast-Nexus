//! HTTP 路由。逐条对应 Python `host_server.py` 的 `if path == "/api/..."` 链。

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::state::{apply_device_settings, AppState};
use nexus_subtitle::config::BackendKind;

type St = State<Arc<AppState>>;

fn ok(v: Value) -> Response {
    Json(v).into_response()
}

fn bad(msg: impl Into<String>) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"ok": false, "error": msg.into()})),
    )
        .into_response()
}

/// 未移植的子系统：明确回 501，不假装成功（前端能看见"还没做"）。
fn not_ported(name: &str) -> Response {
    (
        StatusCode::NOT_IMPLEMENTED,
        Json(json!({
            "ok": false,
            "error": format!("{name} 尚未移植到 Rust 宿主"),
            "not_ported": name
        })),
    )
        .into_response()
}

// ---------------------------------------------------------------- 状态


// ---------------------------------------------------------------- /api/state 子结构

/// Python `sub_state()` 的形状：`{status, alive, pid, port, error, foreignPid, health}`。
///
/// Rust 版没有独立的 8756 字幕服务（识别/翻译由本进程调度），
/// 所以 `pid`/`port` 如实给 null/0，状态与健康从真实后端推导。
async fn subtitle_state(st: &AppState) -> Value {
    // 只读缓存：真正的探测在 subtitle_refresher 里每 2 秒做一次。
    // 请求路径上绝不连端口、不等锁 —— 这是 15 秒超时的根治点。
    st.sub_cache
        .lock()
        .ok()
        .map(|g| g.1.clone())
        .filter(|v| !v.is_null())
        .unwrap_or_else(|| {
            json!({
                "status": "loading", "alive": false, "pid": Value::Null, "port": 0,
                "error": "字幕状态采集中", "foreignPid": Value::Null, "health": Value::Null,
            })
        })
}

/// 后台刷新字幕状态：每 2 秒探测一次，写进 `sub_cache`。
///
/// 为什么不在 handler 里探：/api/state 每秒被前端轮询，探测（含端口连接）
/// 一旦慢一点就会拖住整条热路径——用户实测出现过 "15 秒内无响应: /api/state"。
/// 现在请求路径只读缓存，最坏情况是 2 秒前的状态。
pub async fn subtitle_refresher(st: std::sync::Arc<AppState>) {
    loop {
        // 空闲回收（对应 Python `_idle_reaper`）：到点停掉本进程拉起的
        // llama-server / audiocpp，**不退出宿主进程**（Rust 侧用子进程生命周期管理）。
        // 判据在锁内取（try_lock 拿不到 = 有请求在用，直接跳过），停进程在锁外做。
        // 启动中（sub_starting）不回收：后台 run_start 还在等冷启动（60/120s），
        // 这时回收会把刚拉起的进程杀掉，用户点"启动"却永远起不来。
        let starting = st.sub_starting.load(std::sync::atomic::Ordering::SeqCst);
        let idle_parts = if starting {
            None
        } else {
            match st.subtitle.try_lock() {
                Ok(svc) if svc.idle_due() => {
                    Some((svc.start_parts(), svc.idle_release_min(), svc.activity.idle_secs()))
                }
                _ => None,
            }
        };
        if let Some((parts, mins, idle_sec)) = idle_parts {
            // 回收 = 一次隐性代数推进：与启动路径的 sub_gen 撤下机制同源，
            // 防止并发的启动任务把"刚被回收的进程"当成活的
            st.sub_gen.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let stopped = crate::subtitle_service::SubtitleService::run_stop(&parts).await;
            // 刷新时钟：下一次到期要重新等满一个窗口（宿主进程不退出，持续看护）
            if let Ok(svc) = st.subtitle.try_lock() {
                svc.touch();
            }
            if stopped {
                st.logs.info(format!(
                    "字幕服务空闲 {:.1} 分钟 ≥ {} 分钟，已释放模型（llama/audiocpp）",
                    idle_sec / 60.0,
                    mins
                ));
            }
        }

        if let Ok(svc) = st.subtitle.try_lock() {
            let backend = svc.engine.backend_name();
            let translate_model = svc.engine.model_name();
            let mt_enabled = svc.engine.mt_enabled();
            let mt_local = svc.engine.backend_kind() == BackendKind::Local;
            let idle_release_min = svc.idle_release_min();
            let last_req_ts = svc.last_req_ts();
            let asr_model = svc.cfg.asr.model.clone();
            let asr_device = svc.effective_backend();
            let asr_backend = svc.cfg.asr.backend.clone();
            let asr_port: u16 = svc
                .audiocpp
                .base_url()
                .rsplit(':')
                .next()
                .and_then(|p| p.parse().ok())
                .unwrap_or(8081);
            let mt_port: u16 = svc
                .cfg
                .translate
                .local_base_url
                .rsplit(':')
                .next()
                .and_then(|p| p.parse().ok())
                .unwrap_or(8082);
            drop(svc);

            let probe = |port: u16| async move {
                matches!(
                    tokio::time::timeout(
                        std::time::Duration::from_millis(150),
                        tokio::net::TcpStream::connect(("127.0.0.1", port))
                    )
                    .await,
                    Ok(Ok(_))
                )
            };
            let asr_up = probe(asr_port).await;
            // openai/ollama/none 不拉 llama-server，不能拿 8082 的结果否定就绪
            let mt_up = if mt_local { probe(mt_port).await } else { false };
            let running = asr_up && (!mt_local || mt_up);
            // 引擎侧最近一次 run_start 的真实失败原因（错误传播链不断在中途）
            let last_err = st
                .sub_last_error
                .lock()
                .map(|g| g.clone())
                .unwrap_or_default();
            let value = json!({
                "status": if running { "ready" } else if asr_up || mt_up { "loading" } else { "stopped" },
                "alive": asr_up || mt_up,
                "pid": Value::Null,
                "port": 0,
                "error": if running {
                    "".to_string()
                } else if !last_err.is_empty() {
                    last_err
                } else {
                    "字幕后端未全部就绪".to_string()
                },
                "foreignPid": Value::Null,
                "health": {
                    "asr_backend": asr_backend,
                    "asr_model": asr_model,
                    "device": asr_device,
                    "translate_backend": backend,
                    "translate_model": translate_model,
                    "mt_enabled": mt_enabled,
                    "asr_ready": asr_up,
                    "mt_warm": mt_up,
                    "idle_release_min": idle_release_min,
                    "last_req_ts": last_req_ts,
                    "vram_estimate": Value::Null,
                },
            });
            if let Ok(mut g) = st.sub_cache.lock() {
                *g = (Some(std::time::Instant::now()), value);
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
}



/// Python `vision_state()` 的形状。Rust 版是进程内引擎，没有独立服务，
/// 因此 `health`/`job`/`py_source` 如实给 null/空，其余按真实引擎状态填。
async fn vision_state(st: &AppState) -> Value {
    let s = st.vision.status().await;
    let running = s["engine_state"] == "running";
    let installed = st
        .vision
        .bundle
        .as_ref()
        .map(|b| b.pack_graphs_present())
        .unwrap_or(false);
    // 引擎真实错误必须透出（state=="error" 时 status() 已把它填进 error 字段）：
    // 恒空会让前端只能显示"识别服务启动失败"兜底文案，外来机排障就断了线索；
    // bundle 缺失时模型根本跑不了，那条提示优先于引擎错误。
    let engine_error: Value = if installed {
        s["error"].clone()
    } else {
        json!("视觉 bundle 缺失")
    };
    json!({
        "ok": true,
        "installed": installed,
        "running": running,
        "starting": s["state"] == "warming",
        "ready": installed,
        "auto": false,
        "port": 0,
        "error": engine_error,
        "health": Value::Null,
        "job": Value::Null,
        "engine_state": s["engine_state"],
        "n_actions": s["n_actions"],
        "coverage_ms": s["coverage_ms"],
        "warmup_pct": s["warmup_pct"].clone(),
        "note": s["note"],
        "elapsed_sec": 0,
        "py_source": "",
        "backend": "onnx",
    })
}

/// Python `SyncService.public()` +`SyncSlot.public()` 的形状。
///
/// UI 读的是 `sync.slots.{script,video}.{busy,result{local,device,pushed},error,
/// local_folder,device_folder,logs}` —— 平铺的 `SyncState` 它读不到。
/// 运行态（busy/result/error/logs）来自 `crate::sync::SyncService`（真实的扫描/推送
/// 结果，不是动作时间轴）；目录/连接状态来自设置，与 Python `slot.public()` 一致。
fn sync_state(st: &AppState) -> Value {
    let s = st.settings.get();
    let g = |k: &str| s.get(k).cloned().unwrap_or(json!(""));
    let serial = g("sync_serial").as_str().unwrap_or("").to_string();
    let slot = |kind: crate::sync::SyncKind, local_key: &str, device_key: &str| -> Value {
        let snap = st.sync.snapshot(kind);
        let result = snap
            .result
            .as_ref()
            .map(|r| {
                json!({
                    "local": r.local,
                    "device": r.device,
                    "pushed": r.pushed,
                    "skipped": r.skipped,
                    "deleted": r.deleted,
                    "failed": r.failed,
                })
            })
            .unwrap_or(Value::Null);
        json!({
            "kind": kind.key(),
            "label": kind.label(),
            "busy": snap.busy,
            "connected": !serial.is_empty(),
            "serial": serial.clone(),
            "device": serial.clone(),
            "local_folder": g(local_key),
            "device_folder": g(device_key),
            "error": snap.error,
            // 与 Python 一致：同步完成后才有 result（本地/设备/本次推送条数）
            "result": result,
            "logs": snap.logs,
        })
    };
    json!({
        "adb_path": crate::extras::adb_path_for_ui(),
        "connected": !serial.is_empty(),
        "serial": serial,
        "force_full": s.get("sync_force_full").and_then(|v| v.as_bool()).unwrap_or(false),
        "delete_extra": s.get("sync_delete_extra").and_then(|v| v.as_bool()).unwrap_or(false),
        "slots": {
            "script": slot(crate::sync::SyncKind::Script, "script_folder", "device_folder_script"),
            "video": slot(crate::sync::SyncKind::Video, "video_folder", "device_folder_video"),
        },
    })
}
pub async fn state(State(st): St) -> Response {
    let settings = st.settings.get();
    let subtitle = subtitle_state(&st).await;
    let vision = vision_state(&st).await;
    let sync = sync_state(&st);
    let dlna = {
        let roots = st
            .settings
            .get_path("dlna_roots")
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default();
        let s = st.dlna.status(
            roots
                .iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect(),
        );
        let port = if s.port == 0 {
            port_setting(&st, "dlna_port", 8899)
        } else {
            s.port
        };
        json!({
            "running": s.running,
            "starting": s.starting,
            "error": s.error,
            "port": port,
            "url": if s.running { Some(format!("http://{}:{}", crate::sysinfo::lan_ip(), port)) } else { None },
            "roots": settings.get("dlna_roots").cloned().unwrap_or(json!([])),
            "logs": st.dlna_logs.tail(200),
        })
    };
    ok(json!({
        "ok": true,
        "version": st.version,
        "dev_copy": false,
        "versionInfo": {"name": st.version, "code": 0, "channel": "dev", "dev_copy": false},
        "uptime": (st.uptime_secs() * 10.0).round() / 10.0,
        // Python host 段：端口 + 局域网地址 + 头显接口（UI「关于」页要读）
        "host": {
            "port": st.port,
            "lan_ip": crate::sysinfo::lan_ip(),
            "started_at": st.started_at_epoch(),
            "lan_api_port": crate::lan::lan_port(),
            "lan_api_url": format!("http://{}:{}", crate::sysinfo::lan_ip(), crate::lan::lan_port()),
        },
        "dlna": dlna,
        "subtitle": subtitle,
        "vision": vision,
        // 翻译层统计来自字幕服务的 /translate/stats；Rust 版无该服务，如实给空对象
        "translate": {},
        "sync": sync,
        "update": crate::update::snapshot(),
        "gpu": crate::sysinfo::gpu_info(),
        "sys": crate::sysinfo::sys_info(),
        "settings": settings,
        "events": st.logs.tail(40),
        // 以下三项 Python 的 /api/state 里没有，但 UI 的其它面板在用；
        // 保留为额外字段，不影响契约。
        "device": st.arb.ch.snapshot(),
        "preset": st.arb.preset.state(),
        "quick": st.arb.quick.state(),
    }))
}

pub async fn get_settings(State(st): St) -> Response {
    ok(json!({"ok": true, "settings": st.settings.get()}))
}

pub async fn post_settings(State(st): St, Json(body): Json<Value>) -> Response {
    let mut patch = body.get("settings").cloned().unwrap_or(body);
    // video_link 写盘前夹紧（Python `save_settings` → `_clamp_video_link`）：
    // 非法值不能进设置文件，否则下次启动仍会拿坏值去算 span。
    if let Some(vl) = patch.get("video_link").cloned() {
        if let Some(m) = patch.as_object_mut() {
            m.insert("video_link".into(), crate::state::clamp_video_link(&vl));
        }
    }
    // 开机自启的存储在 HKCU Run 键，不在设置文件里（Python `save_settings:591-603`）。
    // 写注册表失败必须如实返回错误，不能假装保存成功。
    if let Some(on) = patch.get("launch_on_boot").and_then(|v| v.as_bool()) {
        if let Err(e) = crate::autostart::set_launch_on_boot(on) {
            st.logs.error(format!("开机自启设置失败：{e}"));
            return bad(format!("开机自启设置失败：{e}"));
        }
        st.logs.info(format!(
            "开机自动启动：{}",
            if on { "已开启" } else { "已关闭" }
        ));
        // 以注册表真值回填（Windows 侧可能规范化过值）
        if let Some(m) = patch.as_object_mut() {
            m.insert(
                "launch_on_boot".into(),
                json!(crate::autostart::enabled()),
            );
        }
    }
    match st.settings.patch(&patch) {
        Ok(full) => {
            apply_device_settings(&st.settings, &st.arb);
            // 行程/限速改了要立刻下发（Python `_apply_device_settings` 里的 apply_limits）；
            // 未连接时只是返回 false，不影响保存。
            if patch.get("video_link").is_some() {
                let _ = st.arb.ch.apply_limits().await;
            }
            st.logs.info("设置已保存");
            ok(json!({"ok": true, "settings": full}))
        }
        Err(e) => bad(format!("保存失败：{e}")),
    }
}

pub async fn logs(State(st): St, Query(q): Query<HashMap<String, String>>) -> Response {
    let limit = q
        .get("limit")
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(300)
        .min(2000);
    ok(json!({"ok": true, "logs": st.logs.tail(limit)}))
}

// ---------------------------------------------------------------- 设备

pub async fn device_state(State(st): St) -> Response {
    st.arb.ch.sync_connected();
    let mut v = serde_json::to_value(st.arb.ch.snapshot()).unwrap_or(json!({}));
    if let Some(o) = v.as_object_mut() {
        let m = st.arb.ch.motion();
        o.insert("range".into(), json!([m.range_lo, m.range_hi]));
        o.insert("max_speed".into(), json!(m.max_speed));
        o.insert("reversed".into(), json!(m.reversed));
        o.insert("a10_mode".into(), json!(st.arb.ch.mode_override()));
        o.insert("sync".into(), serde_json::to_value(st.arb.sync.state()).unwrap_or(json!({})));
        o.insert("preset".into(), serde_json::to_value(st.arb.preset.state()).unwrap_or(json!({})));
        o.insert("quick".into(), serde_json::to_value(st.arb.quick.state()).unwrap_or(json!({})));
        // Python: st["available"] = _bleak_ok() —— BLE 栈是否可用
        o.insert("available".into(), json!(cfg!(feature = "ble")));
    }
    ok(json!({"ok": true, "device": v}))
}

pub async fn device_scan(State(st): St) -> Response {
    let _ = &st;
    #[cfg(feature = "ble")]
    {
        // 蓝牙故障（适配器不可用/扫描启动失败）与"范围内没有设备"必须可区分，
        // 否则用户只会对着"未找到设备"反复重试
        let resp = match
            nexus_device::ble::BleTransport::scan(std::time::Duration::from_secs(10), true)
                .await
        {
            Ok(found) => json!({"ok": true, "devices": found}),
            Err(e) => json!({"ok": true, "devices": [], "scan_error": e}),
        };
        ok(resp)
    }
    #[cfg(not(feature = "ble"))]
    {
        bad("本构建未启用 BLE（用 --features ble 重新编译）")
    }
}

pub async fn device_connect(State(st): St, Json(body): Json<Value>) -> Response {
    let addr = body.get("address").and_then(|v| v.as_str()).unwrap_or("");
    if addr.is_empty() {
        return bad("缺少 address");
    }
    let toy = body.get("toy").and_then(|v| v.as_str());
    #[cfg(feature = "ble")]
    {
        match nexus_device::ble::BleTransport::connect(addr, toy, &st.arb.ch).await {
            Ok(_) => {
                st.logs.info(format!("已连接设备 {addr}"));
                // 用户常常"先开 AI、后连设备"：识别在跑就补 live，
                // 否则 sync_tick 的 was_live 永远为 false，设备一帧都不会动。
                let video = st.vision.current_video().await;
                let estate = st.vision.status().await["engine_state"]
                    .as_str()
                    .unwrap_or("idle")
                    .to_string();
                let mut live = false;
                if !video.as_os_str().is_empty()
                    && matches!(estate.as_str(), "running" | "done")
                {
                    let r = st.arb.start_live(&video.to_string_lossy());
                    live = r.ok;
                    st.logs.info("AI 已就绪，连接后自动切到 live 跟随");
                }
                ok(json!({"ok": true, "state": st.arb.ch.snapshot(), "live": live}))
            }
            Err(e) => {
                st.logs.error(format!("连接失败：{e}"));
                ok(json!({"ok": false, "error": e}))
            }
        }
    }
    #[cfg(not(feature = "ble"))]
    {
        let _ = (toy, &st);
        bad("本构建未启用 BLE（用 --features ble 重新编译）")
    }
}

pub async fn device_disconnect(State(st): St) -> Response {
    st.arb.stop_all();
    st.arb.ch.cleanup();
    st.logs.info("设备已断开");
    ok(json!({"ok": true, "state": st.arb.ch.snapshot()}))
}

pub async fn device_move(State(st): St, Json(body): Json<Value>) -> Response {
    // 逐行对齐 Python：/api/device/move 读的是 **percent**（前端发的也是 percent），
    // 我之前读 pos，结果无论点哪个百分比都走到 50%。
    let pos = body
        .get("percent")
        .or_else(|| body.get("pos"))
        .and_then(|v| v.as_f64())
        .unwrap_or(50.0);
    let speed = body.get("speed").and_then(|v| v.as_i64()).map(|v| v as i32);
    let ok_ = st.arb.ch.move_to(pos, speed, false).await;
    ok(json!({"ok": ok_}))
}

pub async fn device_limit(State(st): St, Json(body): Json<Value>) -> Response {
    let lo = body.get("min").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
    let hi = body.get("max").and_then(|v| v.as_i64()).unwrap_or(100) as i32;
    let sp = body.get("speed").and_then(|v| v.as_i64()).unwrap_or(500) as i32;
    // Python `/api/device/limit` 只发临时限位帧，**不改**本地 motion：
    // motion 来自设置页的 video_link，由 apply_device_settings 统一注入。
    let r = st.arb.ch.set_limit(lo, hi, sp).await;
    ok(json!({"ok": r}))
}

pub async fn device_mode(State(st): St, Json(body): Json<Value>) -> Response {
    // Python 路由读 `a10`，前端设置页用 `a10_mode`：两者都接受。
    let m = body
        .get("a10_mode")
        .or_else(|| body.get("a10"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0) as i32;
    let r = st.arb.ch.set_mode(m).await;
    ok(json!({"ok": r, "a10_mode": m}))
}

pub async fn device_refresh(State(st): St) -> Response {
    let r = st.arb.ch.refresh_info().await;
    ok(json!({"ok": r, "info": st.arb.ch.snapshot().info}))
}

// ---------------------------------------------------------------- 同步

pub async fn get_sync(State(st): St) -> Response {
    ok(json!({"ok": true, "sync": st.arb.sync.state()}))
}

pub async fn sync_start(State(st): St, Json(body): Json<Value>) -> Response {
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or("");
    if path.is_empty() {
        return bad("缺少 path");
    }
    if !st.arb.ch.snapshot().connected {
        return ok(json!({"ok": false, "error": "设备未连接", "no_device": true}));
    }
    let r = st.arb.start_script(path);
    ok(serde_json::to_value(r).unwrap_or(json!({"ok": false})))
}

/// 把视觉缓冲里的**全量**动作表灌进同步引擎（AI 实时模式）。
///
/// 全量而非增量，是为了让"前缀重算"的修正立刻反映到设备上
/// （Python `/api/sync/tick` 每拍拉 `vision_actions(0, 1e18)` 同款）。
fn feed_vision_live(st: &AppState) {
    let (raw, coverage_ms) = st.vision.buffer.snapshot();
    let actions: Vec<nexus_device::Action> = raw
        .into_iter()
        .map(|a| nexus_device::Action { at: a.at, pos: a.pos })
        .collect();
    st.arb.sync.feed_live(actions, coverage_ms);
}

pub async fn sync_tick(State(st): St, Json(body): Json<Value>) -> Response {
    let t = body.get("t").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let rate = body.get("rate").and_then(|v| v.as_f64()).unwrap_or(1.0);
    // AI 实时模式：先推一拍视觉引擎，再把最新动作表喂给同步引擎。
    // Rust 版直接读进程内缓冲，比 Python 每拍走一次 HTTP 更轻。
    let was_live = st.arb.sync.is_live();
    if was_live {
        st.vision.pump(1e15).await;
        feed_vision_live(&st);
    } else if !st.arb.quick.is_orgasm() && !st.arb.sync.is_active() {
        /* 自愈（对齐 Python host_server 的 tick 分支）：先开 AI、后连设备，
           或 live 因识别服务重启丢失时，tick 自己把会话补成 live；
           否则 was_live 永远为假，设备一帧都收不到。5s 节流，避免 20ms 探测。 */
        let due = {
            let mut at = st
                .live_heal_at
                .lock()
                .unwrap_or_else(|p| p.into_inner());
            if at.elapsed() >= std::time::Duration::from_secs(5) {
                *at = std::time::Instant::now();
                true
            } else {
                false
            }
        };
        if due {
            let video = st.vision.current_video().await;
            let vs = st.vision.status().await;
            let estate = vs["engine_state"].as_str().unwrap_or("idle");
            if !video.as_os_str().is_empty()
                && matches!(estate, "running" | "done")
                && st.arb.ch.snapshot().connected
            {
                st.arb.start_live(&video.to_string_lossy());
                if st.arb.sync.is_live() {
                    st.vision.pump(1e15).await;
                    feed_vision_live(&st);
                }
            }
        }
    }
    let sent_before = st.arb.sync.sent();
    st.arb.sync.apply(t, rate).await;
    let sync = st.arb.sync.state();
    // 诊断字段：设备不动时能一眼看出是哪一环（live/动作表/越界/已发送）。
    // 前端只消费 ok/seek_to，多余字段无副作用。
    ok(json!({
        "ok": true,
        "seek_to": st.arb.sync.take_seek(),
        "t": t,
        "live": was_live,
        "active": sync.active,
        "actions": sync.actions,
        "coverage_ms": sync.live_coverage_ms,
        "sent": sync.sent,
        "sent_delta": sync.sent.saturating_sub(sent_before),
    }))
}

pub async fn sync_delay(State(st): St, Json(body): Json<Value>) -> Response {
    let ms = body.get("ms").and_then(|v| v.as_f64()).unwrap_or(0.0);
    st.arb.sync.set_delay_ms(ms);
    ok(json!({"ok": true, "delay_ms": ms}))
}

pub async fn sync_stop(State(st): St) -> Response {
    st.arb.sync.stop();
    ok(json!({"ok": true}))
}

// ---------------------------------------------------------------- 快捷 / 预设

pub async fn post_quick(State(st): St, Json(body): Json<Value>) -> Response {
    let kind = body.get("kind").and_then(|v| v.as_str()).unwrap_or("");
    let on = body.get("on").and_then(|v| v.as_bool()).unwrap_or(false);
    let res = match kind {
        "stop" => {
            let was = !st.arb.ch.allow_move();
            if on {
                // 急停 = 四个会话全部停止本身（I6），解除后不自动恢复
                st.arb.stop_all();
            }
            let r = st.arb.quick.set_stop(on);
            if was && !on {
                st.arb.sync.reset_last_index(); // 手机端：急停→继续 强制重发当前段
            }
            r
        }
        "orgasm" if on => st.arb.start_orgasm(),
        "slow" if on => st.arb.start_slow(),
        "orgasm" => st.arb.quick.stop_orgasm(),
        "slow" => st.arb.quick.stop_slow(),
        "pause" => st.arb.quick.pause_for_player(),
        "resume" => {
            if st.arb.preset.is_playing() {
                // 预设在播：丢弃恢复旗标，不让暂停过的爆发/缓动复活抢设备
                st.arb.quick.discard_resume()
            } else {
                let (want_o, want_s) = st.arb.quick.take_resume_flags();
                if want_o {
                    st.arb.start_orgasm()
                } else if want_s {
                    st.arb.start_slow()
                } else {
                    st.arb.quick.state()
                }
            }
        }
        _ => return bad("未知快捷动作"),
    };
    ok(json!({"ok": true, "quick": res}))
}

pub async fn get_quick(State(st): St) -> Response {
    ok(json!({"ok": true, "quick": st.arb.quick.state()}))
}

pub async fn post_preset(State(st): St, Json(body): Json<Value>) -> Response {
    let act = body.get("action").and_then(|v| v.as_str()).unwrap_or("");
    match act {
        "select" => {
            let id = body.get("id").and_then(|v| v.as_str());
            match st.arb.preset.select(id) {
                Ok(s) => ok(json!({"ok": true, "preset": s})),
                Err(e) => ok(json!({"ok": false, "error": e})),
            }
        }
        "play" | "start" => match st.arb.start_preset() {
            Ok(s) => ok(json!({"ok": true, "preset": s})),
            Err(e) => ok(json!({"ok": false, "error": e})),
        },
        "pause" | "stop" => ok(json!({"ok": true, "preset": st.arb.preset.stop()})),
        "toggle_play" => match st.arb.toggle_preset() {
            Ok(s) => ok(json!({"ok": true, "preset": s})),
            Err(e) => ok(json!({"ok": false, "error": e})),
        },
        "random" => ok(json!({"ok": true, "preset": st.arb.preset.toggle_random()})),
        "boost" => {
            let s = st.arb.preset.toggle_boost();
            // 只有取消（恢复原速）才落盘 —— 激活时不得把临时的 500 持久化
            if !s.boost {
                let _ = st
                    .settings
                    .patch(&json!({"device": {"preset_speed": s.speed}}));
            }
            ok(json!({"ok": true, "preset": s}))
        }
        "speed" => {
            let v = body.get("speed").and_then(|v| v.as_i64()).unwrap_or(100) as i32;
            let s = st.arb.preset.set_speed(v);
            let _ = st
                .settings
                .patch(&json!({"device": {"preset_speed": s.speed}}));
            ok(json!({"ok": true, "preset": s}))
        }
        _ => bad("未知预设动作"),
    }
}

/// 兼容旧前端的 `/api/vision/auto`：Rust 版由 `vision/open` 自动管理，
/// 没有独立子进程可"自动启动"。如实返回 auto=false，不假装成功。
pub async fn vision_auto() -> Response {
    ok(json!({"ok": true, "auto": false, "note": "Rust 版由 vision/open 自动管理"}))
}

/// 兼容旧前端的 `/api/vision/start`：进程内引擎无需单独启动。
pub async fn vision_start_compat() -> Response {
    ok(json!({"ok": true, "already": true, "note": "Rust 版引擎随 vision/open 启动"}))
}

/// `/api/win/show`：重复启动/托盘入口唤起已有窗口。
pub async fn win_show() -> Response {
    #[cfg(windows)]
    crate::post_window_cmd(crate::WinCmd::Show);
    ok(json!({"ok": true}))
}

pub async fn get_preset(State(st): St) -> Response {
    ok(json!({
        "ok": true,
        "preset": st.arb.preset.state(),
        "presets": st.arb.preset.presets().iter().map(|p| json!({
            "id": p.id, "name": p.name, "segments": p.segments, "keyframes": p.keyframes
        })).collect::<Vec<_>>()
    }))
}

// ---------------------------------------------------------------- 未移植子系统

pub async fn dlna_state(State(st): St) -> Response {
    let roots = st
        .settings
        .get_path("dlna_roots")
        .and_then(|v| v.as_array().cloned())
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(|s| s.to_string()))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let s = st.dlna.status(roots);
    ok(json!({
        "ok": true,
        "dlna": {
            "running": s.running, "starting": s.starting, "error": s.error,
            "port": if s.port == 0 { port_setting(&st, "dlna_port", 8899) } else { s.port },
            "url": s.url, "roots": s.roots,
        }
    }))
}

pub async fn dlna_start(State(st): St) -> Response {
    let port = port_setting(&st, "dlna_port", 8899);
    let roots: Vec<String> = st
        .settings
        .get_path("dlna_roots")
        .and_then(|v| v.as_array().cloned())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
        .unwrap_or_default();
    match st.dlna.start(port, roots).await {
        Ok(url) => {
            st.logs.info(format!("DLNA 已启动：{url}"));
            st.dlna_logs.info(format!("服务已启动 http://{url}"));
            ok(json!({"ok": true, "url": url, "port": port}))
        }
        Err(e) => {
            st.logs.error(format!("DLNA 启动失败：{e}"));
            st.dlna_logs.error(format!("启动失败：{e}"));
            ok(json!({"ok": false, "error": e}))
        }
    }
}

pub async fn dlna_stop(State(st): St) -> Response {
    st.dlna.stop();
    st.logs.info("DLNA 已停止");
    st.dlna_logs.info("服务已停止");
    ok(json!({"ok": true}))
}

pub async fn dlna_roots(State(st): St, Json(body): Json<Value>) -> Response {
    // 对齐 Python：逐个 norm_path 规整（去引号/空白）并丢掉空项，
    // 否则粘进来的引号会被当成路径的一部分存下去，目录永远不存在。
    let roots = match body.get("roots").and_then(|v| v.as_array()) {
        Some(arr) => json!(arr
            .iter()
            .map(|x| crate::extras::norm_path(x.as_str().unwrap_or("")))
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()),
        None => json!([]),
    };
    // 契约回归修复：Python 版会校验路径存在并回 missing/need_restart/restart_hint，
    // 前端三段提示依赖这些字段；缺了它们错路径也报"已添加"、DLNA 运行中
    // 加根也不提示"需重启才生效"。
    let empty: Vec<Value> = Vec::new();
    let items = roots.as_array().unwrap_or(&empty);
    // is_dir 是同步文件系统探测（用户粘进来的 UNC 路径可卡到 SMB 超时）：
    // 整段放 blocking，别占 tokio worker（同文件 937-940 的约定）
    let items2: Vec<String> = items.iter().filter_map(|x| x.as_str().map(String::from)).collect();
    let missing: Vec<String> = tokio::task::spawn_blocking(move || {
        items2
            .iter()
            .filter(|s| !std::path::Path::new(s).is_dir())
            .cloned()
            .collect()
    })
    .await
    .unwrap_or_default();
    let dlna_running = st.dlna.status(Vec::new()).running;
    let need_restart = dlna_running && !items.is_empty();
    match st.settings.patch(&json!({"dlna_roots": roots})) {
        Ok(_) => ok(json!({
            "ok": true,
            "roots": roots,
            "missing": missing,
            "need_restart": need_restart,
            "restart_hint": if need_restart {
                "DLNA 正在运行：新加的目录要重启 DLNA 后才会出现在设备列表里".to_string()
            } else {
                String::new()
            },
        })),
        Err(e) => bad(format!("保存失败：{e}")),
    }
}


// ---------------------------------------------------------------- 媒体库

pub async fn library_state(State(st): St) -> Response {
    let (scanning, progress, last_scan, count) = st.library.state();
    let roots = st
        .settings
        .get_path("library_roots")
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default();
    ok(json!({
        "ok": true,
        "roots": roots,
        "scanning": scanning,
        "progress": progress,
        "last_scan": last_scan,
        "count": count,
    }))
}

pub async fn library_rescan(State(st): St, Json(body): Json<Value>) -> Response {
    let roots: Vec<String> = body
        .get("roots")
        .and_then(|v| v.as_array().cloned())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
        .unwrap_or_else(|| {
            st.settings
                .get_path("library_roots")
                .and_then(|v| v.as_array().cloned())
                .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
                .unwrap_or_default()
        });
    if roots.is_empty() {
        return bad("没有配置媒体库根目录");
    }
    let lib = Arc::clone(&st.library);
    let logs = Arc::clone(&st.logs);
    tokio::spawn(async move {
        match lib.rescan(roots).await {
            Ok(r) => logs.info(format!(
                "媒体库扫描完成：{} 个文件 / {} 张卡 / {} 个目录（{}s）",
                r.files, r.cards, r.dirs, r.sec
            )),
            Err(e) => logs.error(format!("媒体库扫描失败：{e}")),
        }
    });
    ok(json!({"ok": true, "started": true}))
}

pub async fn library_items(State(st): St) -> Response {
    let cards = st.library.cards();
    ok(json!({"ok": true, "cards": cards, "count": cards.len()}))
}

pub async fn library_progress(State(st): St, Json(body): Json<Value>) -> Response {
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or("");
    if path.is_empty() {
        return bad("缺少 path");
    }
    let pos = body.get("pos").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let dur = body.get("dur").and_then(|v| v.as_f64()).unwrap_or(0.0);
    st.library.set_progress(path, pos, dur);
    ok(json!({"ok": true}))
}

pub async fn library_thumb(
    State(st): St,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    let name = q.get("name").cloned().unwrap_or_default();
    // 与 stream/script 同口径：名字必须命中索引里已知的缩略图，
    // 不给 thumb_path 之外的任何路径探测空间
    let known = st.library.cards().iter().any(|c| {
        c.cover == name || c.parts.iter().any(|p| p.thumb == name)
    });
    if !known {
        return (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "缩略图不存在"}))).into_response();
    }
    match st.library.thumb_path(&name) {
        Some(p) => match tokio::fs::read(&p).await {
            Ok(bytes) => (
                StatusCode::OK,
                [(axum::http::header::CONTENT_TYPE, "image/jpeg")],
                bytes,
            )
                .into_response(),
            Err(_) => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "缩略图不存在"}))).into_response(),
        },
        None => (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "缩略图不存在"}))).into_response(),
    }
}

/// 读取端口设置并做范围校验：`as u16` 静默截断会把 70000 变成 4464，
/// DLNA 起在错端口而 UI 还显示 70000（错误结果）。
fn port_setting(st: &AppState, key: &str, default: u16) -> u16 {
    let v = st.settings.get_i64(key).unwrap_or(default as i64);
    if (1..=65535).contains(&v) {
        v as u16
    } else {
        default
    }
}

/// 允许访问的媒体路径：必须落在 `library_roots` / `dlna_roots` / `video_folder` /
/// `script_folder` 之一之下。
///
/// 逐行对齐 `host_server.py` 的 `/api/library/stream`：
/// **判据是"路径在根之下"，不是"文件已被扫描进卡片"**。
/// 我原来按卡片判，导致刚添加目录、还没扫描完成的文件一律打不开。
///
/// canonicalize 是同步文件系统 IO：/api/library/stream 是播放 Range 热路径，
/// 网络盘（UNC）上一个 canonicalize 可达秒级，必须放阻塞线程池
/// （同文件 browse 端点的既有先例）。
async fn allowed_media_path(st: &AppState, p: &std::path::Path) -> bool {
    let roots: Vec<String> = {
        let mut roots: Vec<String> = Vec::new();
        for key in ["library_roots", "dlna_roots"] {
            if let Some(a) = st.settings.get_path(key).and_then(|v| v.as_array().cloned()) {
                roots.extend(a.iter().filter_map(|x| x.as_str().map(String::from)));
            }
        }
        for key in ["video_folder", "script_folder"] {
            if let Some(s) = st.settings.get_str(key) {
                if !s.is_empty() {
                    roots.push(s);
                }
            }
        }
        roots
    };
    let target = p.to_path_buf();
    tokio::task::spawn_blocking(move || {
        let Ok(abs) = target.canonicalize() else {
            return false;
        };
        roots.iter().any(|r| {
            std::path::Path::new(r)
                .canonicalize()
                .map(|rr| abs == rr || abs.starts_with(&rr))
                .unwrap_or(false)
        })
    })
    .await
    .unwrap_or(false)
}

/// /api/library/stream?path=...：内置播放器的视频源，支持 Range。
///
/// 错误一律回 **JSON**：前端对每个响应都做 JSON 解析，回纯文本会炸成
/// `SyntaxError: ... is not valid JSON`（用户实际看到的报错）。
pub async fn library_stream(
    State(st): St,
    Query(q): Query<HashMap<String, String>>,
    headers: axum::http::HeaderMap,
) -> Response {
    let path = q.get("path").cloned().unwrap_or_default();
    let p = std::path::PathBuf::from(&path);
    if path.is_empty() || !p.is_file() {
        return (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "文件不存在"}))).into_response();
    }
    if !allowed_media_path(&st, &p).await {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"ok": false, "error": "这个文件不在媒体库或 DLNA 目录里"})),
        )
            .into_response();
    }
    let total = match tokio::fs::metadata(&p).await {
        Ok(m) if m.is_file() => m.len(),
        _ => {
            return (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "文件不存在"})))
                .into_response()
        }
    };
    let rng = match nexus_dlna::library::parse_range_header(
        headers.get(axum::http::header::RANGE).and_then(|v| v.to_str().ok()),
        total,
    ) {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::RANGE_NOT_SATISFIABLE,
                [(axum::http::header::CONTENT_RANGE, format!("bytes */{total}"))],
                "",
            )
                .into_response();
        }
    };
    let (start, end) = rng.unwrap_or((0, total.saturating_sub(1)));
    let length = end.saturating_sub(start) + 1;
    let mut f = match tokio::fs::File::open(&p).await {
        Ok(f) => f,
        Err(e) => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"ok": false, "error": e.to_string()})),
            )
                .into_response()
        }
    };
    use tokio::io::AsyncSeekExt as _;
    if start > 0 && f.seek(std::io::SeekFrom::Start(start)).await.is_err() {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"ok": false, "error": "定位失败"})),
        )
            .into_response();
    }
    let status = if rng.is_some() {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    };
    let mut h = axum::http::HeaderMap::new();
    h.insert(
        axum::http::header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static(nexus_dlna::library::mime_for(&p)),
    );
    h.insert(axum::http::header::ACCEPT_RANGES, axum::http::HeaderValue::from_static("bytes"));
    h.insert(axum::http::header::CONTENT_LENGTH, axum::http::HeaderValue::from(length));
    if rng.is_some() {
        if let Ok(v) = axum::http::HeaderValue::from_str(&format!("bytes {start}-{end}/{total}")) {
            h.insert(axum::http::header::CONTENT_RANGE, v);
        }
    }
    let limited = tokio::io::AsyncReadExt::take(f, length);
    (
        status,
        h,
        axum::body::Body::from_stream(tokio_util::io::ReaderStream::new(limited)),
    )
        .into_response()
}

/// /api/library/script?path=...：取视频配套的 .funscript 并**归一化**成动作数组。
///
/// 对齐 `host_server.py`：`{ok:true, actions:[[at,pos],...]}`（按 at 排序）。
/// 两种历史格式都收（字典 `{at,pos}` 与数组 `[ms,pos]`）——
/// 我原来直接把文件原文吐出去，前端读 `r.actions` 永远是 undefined。
pub async fn library_script(
    State(st): St,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    let path = q.get("path").cloned().unwrap_or_default();
    let p = std::path::PathBuf::from(&path);
    // Path::with_suffix(".funscript")：把最后一个扩展名换掉
    let fs = p.with_extension("funscript");
    if path.is_empty() || !p.is_file() || !fs.is_file() {
        return ok(json!({"ok": false, "error": "no script"}));
    }
    // 与 /api/library/stream 同一口径：能播的视频才允许读它的脚本
    // （stream 本身有 allowed_media_path，这里补齐消除口径差；POST 手选
    // 任意脚本不在此限——那是显式的用户动作）
    if !allowed_media_path(&st, &p).await {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"ok": false, "error": "这个文件不在媒体库或 DLNA 目录里"})),
        )
            .into_response();
    }
    match read_script_actions(&fs).await {
        Ok(acts) => ok(json!({"ok": true, "actions": acts})),
        Err(e) => ok(json!({"ok": false, "error": e})),
    }
}

/// `POST /api/library/script`：手动选择任意 `.funscript`。
///
/// 既加载到 SyncEngine（设备跟随），也把动作数组回给前端画热力图。
pub async fn manual_script(State(st): St, Json(body): Json<Value>) -> Response {
    let video = body.get("video").and_then(|v| v.as_str()).unwrap_or("");
    let script = body.get("script").and_then(|v| v.as_str()).unwrap_or("");
    if script.is_empty() {
        return bad("缺少 script");
    }
    let p = std::path::PathBuf::from(script);
    let is_fs = p
        .extension()
        .map(|e| e.eq_ignore_ascii_case("funscript"))
        .unwrap_or(false);
    if !is_fs || !p.is_file() {
        return ok(json!({"ok": false, "error": "请选择一个存在的 .funscript 文件"}));
    }
    let acts = match read_script_actions(&p).await {
        Ok(a) => a,
        Err(e) => return ok(json!({"ok": false, "error": e})),
    };
    let r = st.arb.start_script_file(&p);
    if !r.ok {
        return ok(json!({"ok": false, "error": r.error.unwrap_or_default()}));
    }
    ok(json!({
        "ok": true,
        "script": script,
        "video": video,
        "actions": acts,
        "count": acts.len(),
    }))
}

async fn read_script_actions(path: &std::path::Path) -> Result<Vec<(i64, i64)>, String> {
    let text = tokio::fs::read_to_string(path)
        .await
        .map_err(|e| e.to_string())?;
    let data: Value = serde_json::from_str(&text).map_err(|e| format!("{e}"))?;
    let mut acts: Vec<(i64, i64)> = Vec::new();
    if let Some(arr) = data.get("actions").and_then(|v| v.as_array()) {
        for a in arr {
            if let Some(o) = a.as_object() {
                let at = o.get("at").and_then(|v| v.as_f64()).unwrap_or(0.0) as i64;
                let pos = o.get("pos").and_then(|v| v.as_f64()).unwrap_or(0.0) as i64;
                acts.push((at, pos));
            } else if let Some(t) = a.as_array() {
                if t.len() >= 2 {
                    let at = t[0].as_f64().unwrap_or(0.0) as i64;
                    let pos = t[1].as_f64().unwrap_or(0.0) as i64;
                    acts.push((at, pos));
                }
            }
        }
    }
    acts.sort_by_key(|a| a.0);
    Ok(acts)
}


// ---------------------------------------------------------------- 字幕


/// 流式字幕：POST 裸 PCM（16k/单声道/s16le），返回 `text/event-stream`。
///
/// `lang`/`translate`/`video_start_ms` 走查询参数，与 Python 版同形。
pub async fn subtitle_stream(
    State(st): St,
    Query(q): Query<HashMap<String, String>>,
    body: axum::body::Bytes,
) -> Response {
    let lang = q.get("lang").cloned().unwrap_or_else(|| "ja".to_string());
    let translate = q
        .get("translate")
        .map(|v| v != "false" && v != "0")
        .unwrap_or(true);
    let start_ms = q
        .get("video_start_ms")
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(0.0);

    let mut pcm = body.to_vec();
    if pcm.len() % 2 == 1 {
        pcm.pop();
    }
    // 锁内只取参数快照，真正发请求在锁外（subtitle 是全局单把锁，持锁 await 网络会冻结全部字幕接口）
    let parts = {
        let svc = st.subtitle.lock().await;
        svc.stream_parts(translate)
    };
    let stream = crate::subtitle_service::SubtitleService::run_stream(parts, pcm, lang, translate, start_ms).await;

    use axum::response::IntoResponse;
    axum::response::Response::builder()
        .header("Content-Type", "text/event-stream")
        .header("Cache-Control", "no-cache")
        .header("X-Accel-Buffering", "no")
        .body(axum::body::Body::from_stream(futures_util::StreamExt::map(stream, |s| {
            Ok::<_, std::convert::Infallible>(axum::body::Bytes::from(s))
        })))
        .unwrap()
        .into_response()
}

pub async fn subtitle_status(State(st): St) -> Response {
    // 锁内取快照，锁外 probe（HTTP），最后组装
    let parts = {
        let svc = st.subtitle.lock().await;
        svc.status_parts()
    };
    // 只有本地 llama 才有可探的常驻进程；openai/ollama/none 不去连 8082
    let (asr_up, mt_up) = if parts.translate_local {
        let (a, m) = tokio::join!(parts.asr.probe(), parts.mt.probe());
        (a, m)
    } else {
        (parts.asr.probe().await, false)
    };
    let status = crate::subtitle_service::SubtitleService::status_json(&parts, asr_up, mt_up);
    ok(json!({"ok": true, "subtitle": status}))
}

pub async fn subtitle_start(State(st): St) -> Response {
    ok(start_subtitle(&st).await)
}

/// 启动字幕服务的共用实现：HTTP handler 与启动期 `subtitle_auto_start` 都走这里
/// （Python `sub_start()`；`host_server.py:5173-5177`）。
pub async fn start_subtitle(st: &Arc<AppState>) -> Value {
    // 契约 B（对齐 Python /api/subtitle/start）：响应里要带档位建议，头显据此自适应分块。
    // 云端一律 25s（单块 6.6~15s，3s 档追不上），本地按
    // config.asr.recommended_chunk_sec（默认 3，钳 [1,25]）。
    let (parts, cloud, chunk_local) = {
        let svc = st.subtitle.lock().await;
        svc.touch(); // 启动也算活动，避免刚拉起就被空闲回收
        let backend = svc.engine.backend_kind();
        (
            svc.start_parts(),
            backend == BackendKind::OpenAi,
            svc.cfg.asr.recommended_chunk_sec.unwrap_or(3).clamp(1, 25),
        )
    };
    let recommended_chunk_sec = if cloud { 25 } else { chunk_local };
    let starting_resp = json!({
        "ok": true, "starting": true,
        "recommended_chunk_sec": recommended_chunk_sec,
    });
    // 防重入：启动中再次点击直接返回（Python `RT.sub_starting` 同款），
    // 否则两次 run_start 会互相杀掉对方刚拉起的进程。
    if st.sub_starting.swap(true, std::sync::atomic::Ordering::SeqCst) {
        return starting_resp;
    }
    // 与 Python 一致：立即返回，常驻进程的拉起放后台做。
    // ensure_server 冷启动最长 60/120s，而前端 api() 只有 15s 超时：
    // 同步等待会让"启动成功"被前端当成失败（toast 报错、状态却已就绪）。
    let gen = st.sub_gen.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
    let st2 = Arc::clone(st);
    let logs = Arc::clone(&st.logs);
    tokio::spawn(async move {
        let (asr, mt, errs) = crate::subtitle_service::SubtitleService::run_start(&parts).await;
        if st2.sub_gen.load(std::sync::atomic::Ordering::SeqCst) != gen {
            // 启动期间用户点了停止：撤下刚拉起的进程（Python 的代数作废同款）。
            // 兜底 run_stop 只在该 stop 是我之后唯一动作时执行——stop 后立刻
            // 再启动的话，start_parts 返回的是同一 backend 实例，兜底会把
            // 新启动刚拉起的进程杀掉；那种情况新启动的 Managed::spawn 会
            // take 旧实例 kill，孤儿进程仍有人管。
            let cur = st2.sub_gen.load(std::sync::atomic::Ordering::SeqCst);
            let stopped_only = cur == gen + 1
                && st2.sub_last_stop_gen.load(std::sync::atomic::Ordering::SeqCst) == gen + 1;
            if stopped_only {
                crate::subtitle_service::SubtitleService::run_stop(&parts).await;
            }
            logs.info("字幕服务启动完成前收到停止请求，已取消");
        } else if errs.is_empty() {
            if let Ok(mut g) = st2.sub_last_error.lock() {
                g.clear();
            }
            logs.info("字幕服务已启动");
        } else {
            if let Ok(mut g) = st2.sub_last_error.lock() {
                *g = errs.clone();
            }
            logs.warn(format!(
                "字幕服务启动有失败项（asr={asr}, translate={mt}）：{errs}"
            ));
        }
        st2.sub_starting
            .store(false, std::sync::atomic::Ordering::SeqCst);
    });
    starting_resp
}

pub async fn subtitle_stop(State(st): St) -> Response {
    // 作废在途启动：否则后台任务会在 stop 之后又把进程拉起来。
    // 记下本次 stop 的代数：在途启动的兜底 run_stop 据此判定
    // "stop 是它之后唯一动作"（防误杀 stop 后立刻再启动的新进程）。
    let stop_gen = st.sub_gen.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
    st.sub_last_stop_gen
        .store(stop_gen, std::sync::atomic::Ordering::SeqCst);
    st.sub_starting
        .store(false, std::sync::atomic::Ordering::SeqCst);
    let parts = {
        let svc = st.subtitle.lock().await;
        svc.start_parts()
    };
    let stopped = crate::subtitle_service::SubtitleService::run_stop(&parts).await;
    // 对齐 Python：stopped=false 表示本来就没在运行，前端会换一条文案
    if stopped {
        st.logs.info("字幕服务已停止 · 模型内存已释放");
    } else {
        st.logs.info("字幕服务本来就没在运行");
    }
    ok(json!({"ok": true, "stopped": stopped}))
}

pub async fn subtitle_config(State(st): St) -> Response {
    let svc = st.subtitle.lock().await;
    ok(json!({"ok": true, "config": svc.cfg.raw, "path": svc.config_path.to_string_lossy()}))
}

pub async fn subtitle_config_save(State(st): St, Json(body): Json<Value>) -> Response {
    // 写盘（快操作）在锁内完成；重载（停 audiocpp/llama 两个子进程，秒级）
    // 放锁外后台任务——持全局字幕锁 await 杀进程会把 8756/8791 的全部字幕
    // 接口冻结到杀完为止（历史"锁内慢操作=假死"的同型）。
    let (path, merged) = {
        let svc = st.subtitle.lock().await;
        let mut merged = svc.cfg.raw.clone();
        if merged.is_null() {
            merged = json!({});
        }
        // 深合并：只覆盖传进来的键，未知键保留
        crate::settings::deep_merge(&mut merged, &body);
        (svc.config_path.clone(), merged)
    };
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    match serde_json::to_string_pretty(&merged) {
        Ok(text) => {
            if let Err(e) = std::fs::write(&path, text) {
                return bad(format!("写入失败：{e}"));
            }
            let st2 = std::sync::Arc::clone(&st);
            tokio::spawn(async move {
                // 锁内只做"读配置 + 换新实例"（reload_swap），秒级的停旧进程
                // 必须在锁外——持全局字幕锁 await 杀进程会把全部字幕接口
                // 冻结到杀完为止（本 handler 头部注释的契约，历史假死同型）
                let (old_a, old_l) = {
                    let mut svc = st2.subtitle.lock().await;
                    svc.reload_swap()
                };
                old_a.stop().await;
                old_l.stop().await;
                st2.logs.info("字幕配置已保存并重载");
            });
            ok(json!({"ok": true, "reloading": true}))
        }
        Err(e) => bad(format!("序列化失败：{e}")),
    }
}

/// 翻译自检：给一段文本，回译文（界面上的"试试翻译"）。
pub async fn subtitle_translate(State(st): St, Json(body): Json<Value>) -> Response {
    let text = body.get("text").and_then(|v| v.as_str()).unwrap_or("");
    let lang = body
        .get("lang")
        .and_then(|v| v.as_str())
        .unwrap_or("ja")
        .to_string();
    if text.trim().is_empty() {
        return bad("缺少 text");
    }
    let (engine, _activity) = {
        let svc = st.subtitle.lock().await;
        (Arc::clone(&svc.engine), svc.activity.begin())
    };
    if engine.backend_kind() == BackendKind::None {
        return ok(json!({
            "ok": false,
            "translation": "",
            "error": "翻译已关闭（translate.backend = none，不发起任何请求）",
        }));
    }
    match engine.translate_for(text, &lang).await {
        Ok(t) => ok(json!({"ok": true, "translation": t})),
        Err(e) => ok(json!({"ok": false, "error": e})),
    }
}


// ---------------------------------------------------------------- 视觉

/// `GET /api/vision/state` —— 与 `/api/state` 里的 `vision` 同一份结构，另加 `runtime`。
///
/// **消费点**：`app.js aivPoll` 读 `engine_state / coverage_ms / n_actions /
/// warmup_pct / error / note`；`aivPollRuntime` 读 `runtime.{state,pct,error}`。
/// 我原来只回了 6 个键，缺 `warmup_pct`/`error`/`runtime`，AI 识别页读不到状态。
pub async fn vision_status(State(st): St) -> Response {
    let mut v = vision_state(&st).await;
    // Rust 版的 ONNX 运行时随安装包分发，不存在"下载安装中"这个状态，
    // 如实回 done；前端据此直接进入预热。
    v["runtime"] = json!({"state": "done", "pct": 100, "error": ""});
    ok(v)
}


pub async fn vision_open(State(st): St, Json(body): Json<Value>) -> Response {
    let video = body.get("video").and_then(|v| v.as_str()).unwrap_or("");
    if video.is_empty() {
        return bad("缺少 video");
    }
    match st.vision.open(&std::path::PathBuf::from(video)).await {
        Ok(()) => {
            st.logs.info(format!("视觉：已打开 {video}"));
            let mut resp = json!({"ok": true});
            // 对齐 Python：引擎起来后把设备同步切到 live 模式，
            // 动作由 /api/sync/tick 从视觉缓冲喂入（与脚本模式共用同一套下发路径）。
            if st.arb.ch.snapshot().connected {
                st.arb.start_live(video);
                resp["live"] = json!(true);
            }
            ok(resp)
        }
        Err(e) => {
            st.logs.error(format!("视觉打开失败：{e}"));
            ok(json!({"ok": false, "error": e}))
        }
    }
}

pub async fn vision_actions(
    State(st): St,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    let from = q.get("from_ms").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);
    let to = q
        .get("to_ms")
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(1e18);
    // 先推一拍，保证"边播边出"
    st.vision.pump(to.min(1e15)).await;
    let list = st.vision.buffer.range(from, to);
    ok(json!({
        "ok": true,
        "actions": list.iter().map(|a| json!({"at": a.at, "pos": a.pos})).collect::<Vec<_>>(),
        "coverage_ms": st.vision.buffer.snapshot().1,
    }))
}

/// 视觉自检：真的把模型会话建起来（不只是查文件在不在）。
///
/// 现场排障用：`/api/vision/state` 只能告诉你"服务在跑"，这个能告诉你
/// "ONNX 运行库能不能加载模型"——缺 DLL、DML 不可用、模型损坏都会在这里暴露。
pub async fn vision_selfcheck(State(_st): St) -> Response {
    let ort = crate::vision_service::resolve_ort_dylib()
        .map(|p| p.to_string_lossy().to_string());
    let bundle = crate::vision_service::load_bundle_for_check();
    // Decoder::load 要加载两个模型图并初始化 DML 会话（秒级）：同步 IO 放
    // blocking 线程，别占 tokio worker（同文件 937-940 的 spawn_blocking 约定）
    let (bundle_ok, pack_graphs, head_ok, err) = tokio::task::spawn_blocking(move || match &bundle {
        Some(b) => {
            let graphs = [&b.graph_head, &b.graph_mask, &b.graph_env_step]
                .iter()
                .map(|g| {
                    let p = b.pack_graph_path(g);
                    // 必须是 owned String：spawn_blocking 闭包的返回值不能借用 bundle
                    ((*g).clone(), p.is_file())
                })
                .collect::<Vec<_>>();
            match nexus_vision::onnx::Decoder::load(b) {
                Ok(_) => (true, graphs, true, String::new()),
                Err(e) => (true, graphs, false, e),
            }
        }
        None => (false, Vec::new(), false, "bundle 未加载".to_string()),
    })
    .await
    .unwrap_or((false, Vec::new(), false, "自检任务失败".to_string()));
    let all_ok = ort.is_some() && bundle_ok && head_ok;
    ok(json!({
        "ok": all_ok,
        "ort_dylib": ort,
        "bundle_loaded": bundle_ok,
        "pack_graphs": pack_graphs.iter().map(|(g, f)| json!({"graph": g, "present": f})).collect::<Vec<_>>(),
        "head_env_sessions": head_ok,
        "error": err,
    }))
}

pub async fn vision_close(State(st): St) -> Response {
    st.vision.close().await;
    // 对齐 Python：关闭识别时把 live 同步一并停掉，避免 tick 继续空转
    if st.arb.sync.is_live() {
        st.arb.sync.stop();
    }
    st.logs.info("视觉：已关闭");
    ok(json!({"ok": true}))
}

pub async fn update_stub() -> Response {
    not_ported("update")
}
pub async fn models_stub() -> Response {
    not_ported("models")
}
pub async fn quit(State(st): St) -> Response {
    st.logs.info("收到退出请求");
    let st2 = Arc::clone(&st);
    tokio::spawn(async move {
        // 等响应先回到页面，再做可能耗时的清理
        tokio::time::sleep(std::time::Duration::from_millis(120)).await;
        crate::shutdown_services_async(&st2).await;
        std::process::exit(0);
    });
    ok(json!({"ok": true}))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// AI 实时模式：视觉缓冲的整表必须原样进入同步引擎（含覆盖范围）。
    #[test]
    fn live_feed_replaces_whole_action_table() {
        let st = AppState::new("0.1.0-rust-test", 8890);
        st.vision.buffer.replace_all(
            vec![
                nexus_vision::Action { at: 0.0, pos: 10.0 },
                nexus_vision::Action { at: 1000.0, pos: 90.0 },
            ],
            1000.0,
        );
        st.arb.sync.start_live("v.mp4");
        feed_vision_live(&st);
        let s = st.arb.sync.state();
        assert!(s.live, "live 标志不应被 feed 清掉");
        assert_eq!(s.actions, 2);
        assert!((s.live_coverage_ms - 1000.0).abs() < 1e-9);
    }
}
