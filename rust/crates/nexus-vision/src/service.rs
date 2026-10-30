//! 视觉 HTTP 服务（与 Python `run_server.py` 同形）。
//!
//! 端点：`/health` `/state` `/open` `/actions` `/close`。
//! 静止门在这里统一落地 —— 引擎吐出原始动作，服务按配置过滤后再给宿主，
//! 这样"用户今天看到的成品"这条语义只有一处实现。

use std::path::PathBuf;
use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};
use tokio::sync::Mutex;

use crate::buffer::{Action, ActionBuffer, EngineState};
use crate::inference::{EngineStatus, Inference};
use crate::still_gate::{gate_actions, signals, GateConfig, GateMode};

pub struct VisionService {
    pub buffer: Arc<ActionBuffer>,
    pub engine: Arc<dyn Inference>,
    pub bundle: Option<crate::bundle::Bundle>,
    pub gate: GateConfig,
    /// 当前会话：视频路径 + 最近一次的轨道信号（供静止门用）
    session: Mutex<Session>,
    /// pump 脏检查：上次处理的（引擎发布代数, 播放头前动作数, 播放头）。
    /// 引擎没新发布且播放头前进未跨过新动作点时，门控结果不变，跳过
    /// 信号全量 clone + 全表门控重算 + 动作表整体重排（长片后期秒级浪费）。
    pump_state: std::sync::Mutex<(u64, usize, f64)>,
}

#[derive(Default)]
struct Session {
    video: PathBuf,
    /// 逐行速度（pos/s）与置信度；引擎给得出才有，给不出就走"关门控"
    vm: Vec<f64>,
    conf: Vec<f64>,
    /// 行时间轴（秒）
    times_sec: Vec<f64>,
}

impl VisionService {
    pub fn new(engine: Arc<dyn Inference>, bundle: Option<crate::bundle::Bundle>, gate: GateConfig) -> Arc<Self> {
        Arc::new(Self {
            buffer: Arc::new(ActionBuffer::new()),
            engine,
            bundle,
            gate,
            session: Mutex::new(Session::default()),
            pump_state: std::sync::Mutex::new((u64::MAX, 0, f64::NEG_INFINITY)),
        })
    }

    pub fn mode_name(&self) -> &'static str {
        match self.gate.mode {
            GateMode::Off => "off",
            GateMode::And => "and",
            GateMode::Or => "or",
            GateMode::Combo => "combo",
        }
    }

    /// 应用静止门（没有轨道信号时原样返回，并如实标注）。
    fn apply_gate(&self, actions: Vec<Action>, sess: &Session) -> (Vec<Action>, Option<crate::still_gate::GateStats>) {
        let (kept, stats) = if self.gate.mode.is_off() || sess.vm.is_empty() || sess.conf.is_empty() {
            (actions, None)
        } else {
            let (vm, conf) = signals(
                &sess.vm,
                &sess.conf,
                self.bundle.as_ref().map(|b| b.pack.v_std).unwrap_or(1.0),
            );
            let pairs: Vec<(f64, f64)> = actions.iter().map(|a| (a.at, a.pos)).collect();
            let (kept, stats) =
                gate_actions(&pairs, &sess.times_sec, &vm, &conf, &self.gate);
            (
                kept.into_iter().map(|(at, pos)| Action { at, pos }).collect(),
                Some(stats),
            )
        };
        // Python `_style`：门控后对整表做 clamp_speed(600)，只改深度不改时间
        (clamp_speed_actions(kept, crate::compose::MAX_POS_RATE), stats)
    }

    /// 一拍：向引擎要动作 → 过静止门 → 发布到 buffer。
    pub async fn pump(&self, playhead_ms: f64) -> (usize, f64) {
        let epoch = self.engine.publish_epoch();
        let dirty_epoch = {
            let last = self.pump_state.lock().unwrap();
            epoch != last.0
        };
        // 引擎有新发布才重新收轨道信号（全长三条 clone，长片后期很贵）
        if dirty_epoch {
            let (vm, conf, times) = self.engine.signals();
            if !vm.is_empty() && !conf.is_empty() && !times.is_empty() {
                let mut s = self.session.lock().await;
                s.vm = vm;
                s.conf = conf;
                s.times_sec = times;
            }
        }
        let (raw, until) = self.engine.poll(playhead_ms).await;
        let head_n = raw.len();
        {
            let mut last = self.pump_state.lock().unwrap();
            if epoch == last.0 && head_n == last.1 && playhead_ms > last.2 {
                // 播放头前进但没跨过新动作点：门控结果与 buffer 内容不变，
                // 跳过全表门控重算与 replace_all 重排（playhead 回退不跳过：
                // 回退后 head 收缩，buffer 必须跟着收缩）
                return (0, until);
            }
            *last = (epoch, head_n, playhead_ms);
        }
        let sess = self.session.lock().await;
        let (gated, _stats) = self.apply_gate(raw, &sess);
        let n = gated.len();
        self.buffer.replace_all(gated, until);
        // 预热态也要能毕业：只从 Idle 转 Running 会让 /api/state 的 starting 永远为真
        if n > 0
            && matches!(
                self.buffer.state(),
                EngineState::Idle | EngineState::Warming
            )
        {
            self.buffer.set_state(EngineState::Running);
        }
        (n, until)
    }

    pub async fn open(&self, video: &PathBuf) -> Result<(), String> {
        {
            let mut s = self.session.lock().await;
            s.video = video.clone();
            // 换视频后旧轨道的信号必须清掉，否则新视频前几拍会拿旧窗口做门控
            s.vm.clear();
            s.conf.clear();
            s.times_sec.clear();
        }
        self.buffer.clear();
        self.buffer.set_state(EngineState::Warming);
        if let Err(e) = self.engine.open(video).await {
            // 失败早退必须恢复 Idle 并清掉会话，否则 /state 显示 warming 却没有任务在跑
            self.buffer.set_state(EngineState::Idle);
            let mut s = self.session.lock().await;
            s.video = PathBuf::new();
            return Err(e);
        }
        Ok(())
    }

    pub async fn close(&self) {
        self.engine.close().await;
        self.buffer.clear();
        let mut s = self.session.lock().await;
        *s = Session::default();
    }

    /// 当前识别会话的视频路径（未打开时为空）。设备补连 live 时用。
    pub async fn current_video(&self) -> PathBuf {
        self.session.lock().await.video.clone()
    }

    pub async fn status(&self) -> Value {
        let st: EngineStatus = self.engine.status();
        let info = self.buffer.info();
        // 与 Python `service.py` 一致：n_actions 报**门控后**的 buffer 长度，
        // 不能拿引擎原始动作数把它抬高（会让人误以为门控没生效）。
        let n_total = info.actions.max(0);
        let cov_total = info.gen_until_ms.max(st.coverage_ms);
        // error 字段必须透出引擎错误：前端错误分支依赖 state=='error' 或 error 非空，
        // 恒空会让识别失败永远卡在预热等待（错误传播链的最后一环）
        let engine_error = if st.state == "error" { st.note.clone() } else { String::new() };
        json!({
            "ok": true,
            "state": info.state,
            "engine_state": st.state,
            "n_actions": n_total,
            "coverage_ms": cov_total,
            "warmup_pct": st.pct,
            "note": if st.note.is_empty() { info.note } else { st.note },
            "error": engine_error,
            "still_gate": self.mode_name(),
            "bundle": self.bundle.as_ref().map(|b| b.default_pack.clone()),
        })
    }
}

/// 对齐 Python `common.clamp_speed`：超速行程只截深度，时间不动。
fn clamp_speed_actions(actions: Vec<Action>, max_speed: f64) -> Vec<Action> {
    if actions.len() < 2 || max_speed <= 0.0 {
        return actions;
    }
    let mut out: Vec<Action> = Vec::with_capacity(actions.len());
    out.push(actions[0]);
    for a in actions.iter().skip(1) {
        let prev = *out.last().unwrap();
        let lim = max_speed * (a.at - prev.at) / 1000.0;
        let d = a.pos - prev.pos;
        let pos = if d.abs() > lim {
            let step = lim.trunc();
            (prev.pos + if d > 0.0 { step } else { -step }).clamp(0.0, 100.0)
        } else {
            a.pos
        };
        out.push(Action { at: a.at, pos });
    }
    out
}

type St = State<Arc<VisionService>>;

pub async fn health(State(svc): St) -> Response {
    Json(json!({
        "status": "ok",
        "backend": "onnx",
        "still_gate": svc.mode_name(),
        "bundle": svc.bundle.as_ref().map(|b| b.default_pack.clone()),
        "engine_state": svc.engine.status().state,
    }))
    .into_response()
}

pub async fn state(State(svc): St) -> Response {
    Json(svc.status().await).into_response()
}

pub async fn open(State(svc): St, Json(body): Json<Value>) -> Response {
    let video = body.get("video").and_then(|v| v.as_str()).unwrap_or("");
    if video.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "error": "缺少 video"}))).into_response();
    }
    match svc.open(&PathBuf::from(video)).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(e) => Json(json!({"ok": false, "error": e})).into_response(),
    }
}

/// `/actions?from_ms=&to_ms=`：宿主每拍来拉全量动作表。
pub async fn actions(
    State(svc): St,
    Query(q): Query<std::collections::HashMap<String, String>>,
) -> Response {
    let from = q.get("from_ms").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);
    let to = q.get("to_ms").and_then(|v| v.parse::<f64>().ok()).unwrap_or(f64::MAX);
    // 拉取前先推一拍，保证"边播边出"
    let (_, until) = svc.pump(to.min(1e15)).await;
    let list = svc.buffer.range(from, to);
    Json(json!({
        "ok": true,
        "actions": list.iter().map(|a| json!({"at": a.at, "pos": a.pos})).collect::<Vec<_>>(),
        "coverage_ms": until,
        "still_gate": svc.mode_name(),
    }))
    .into_response()
}

pub async fn close(State(svc): St) -> Response {
    svc.close().await;
    Json(json!({"ok": true})).into_response()
}

pub fn router(svc: Arc<VisionService>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/state", get(state))
        .route("/open", post(open))
        .route("/actions", get(actions))
        .route("/close", post(close))
        .with_state(svc)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inference::Mock;

    fn svc_with(actions: Vec<Action>, gate: GateConfig) -> Arc<VisionService> {
        VisionService::new(Mock::new(actions), None, gate)
    }

    #[test]
    fn clamp_speed_limits_depth_but_not_time() {
        let out = clamp_speed_actions(
            vec![
                Action { at: 0.0, pos: 0.0 },
                Action { at: 100.0, pos: 100.0 },
            ],
            600.0,
        );
        assert_eq!(out[1].at, 100.0, "限速只改深度，不改时间");
        assert_eq!(out[1].pos, 60.0, "100ms 内最多 60 pos");
    }

    #[tokio::test]
    async fn pump_publishes_and_status_reflects_it() {
        let svc = svc_with(
            vec![Action { at: 0.0, pos: 10.0 }, Action { at: 1000.0, pos: 90.0 }],
            GateConfig { mode: GateMode::Off, ..Default::default() },
        );
        svc.open(&PathBuf::from("v.mp4")).await.unwrap();
        assert_eq!(svc.buffer.state(), EngineState::Warming);
        let (n, until) = svc.pump(1500.0).await;
        assert_eq!(n, 2);
        assert_eq!(until, 1000.0);
        assert_eq!(
            svc.buffer.state(),
            EngineState::Running,
            "预热态收到动作后必须毕业，否则 starting 永远为真"
        );
        let st = svc.status().await;
        assert_eq!(st["n_actions"], 2);
        assert_eq!(st["still_gate"], "off");
    }

    #[tokio::test]
    async fn gate_drops_actions_when_signals_present() {
        let svc = svc_with(
            vec![Action { at: 0.0, pos: 10.0 }, Action { at: 5000.0, pos: 90.0 }],
            GateConfig { mode: GateMode::Combo, ..Default::default() },
        );
        // 造一条"前段静止、后段在动"的轨道
        // 注意：没有 bundle 时 v_std = 1.0，所以这里的 vm 直接按 pos/s 给值
        let times: Vec<f64> = (0..=100).map(|i| i as f64 * 0.1).collect();
        let vm: Vec<f64> = (0..=100).map(|i| if i < 30 { 5.0 } else { 200.0 }).collect();
        let conf: Vec<f64> = (0..=100).map(|i| if i < 30 { 0.2 } else { 0.95 }).collect();
        // open 会清空上一个会话的信号；真实流程里信号由引擎在 open 之后发布
        svc.open(&PathBuf::from("v.mp4")).await.unwrap();
        {
            let mut s = svc.session.lock().await;
            s.vm = vm;
            s.conf = conf;
            s.times_sec = times;
        }
        let (n, _) = svc.pump(6000.0).await;
        assert_eq!(n, 1, "静止段的动作点应被门控掉");
        let kept = svc.buffer.snapshot().0;
        assert_eq!(kept[0].pos, 90.0);
    }

    #[tokio::test]
    async fn close_clears_everything() {
        let svc = svc_with(vec![Action { at: 0.0, pos: 10.0 }], GateConfig::default());
        svc.open(&PathBuf::from("v.mp4")).await.unwrap();
        svc.pump(1000.0).await;
        svc.close().await;
        assert_eq!(svc.buffer.snapshot().0.len(), 0);
        assert_eq!(svc.status().await["n_actions"], 0);
    }

    #[tokio::test]
    async fn actions_endpoint_slices_by_span() {
        use axum::body::Body;
        use axum::http::Request;
        use tower::ServiceExt;

        let svc = svc_with(
            vec![
                Action { at: 0.0, pos: 1.0 },
                Action { at: 1000.0, pos: 2.0 },
                Action { at: 2000.0, pos: 3.0 },
            ],
            GateConfig { mode: GateMode::Off, ..Default::default() },
        );
        svc.open(&PathBuf::from("v.mp4")).await.unwrap();
        let app = router(svc);
        let res = app
            .oneshot(
                Request::builder()
                    .uri("/actions?from_ms=500&to_ms=1500")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = axum::body::to_bytes(res.into_body(), 1 << 20).await.unwrap();
        let v: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["ok"], true);
        let arr = v["actions"].as_array().unwrap();
        assert_eq!(arr.len(), 1, "只返回区间内的动作");
        assert_eq!(arr[0]["pos"], 2.0);
    }
}
