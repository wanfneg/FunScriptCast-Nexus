//! 动作缓冲：发布侧整体替换，播放侧只读快照。
//!
//! 流式语义的关键（Python `onnx_stream.ActionBuffer` 同款）：
//! 引擎每次前缀重算后**整体替换**动作表，并用 `gen_until_ms` 告诉消费方
//! "这些动作可靠到哪一刻"。播放侧只取快照，绝不在引擎写的时候读到半张表。

use std::sync::RwLock;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Action {
    pub at: f64,
    pub pos: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EngineState {
    Idle,
    Warming,
    Running,
    Done,
    Error,
}

impl Default for EngineState {
    fn default() -> Self {
        EngineState::Idle
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct BufferState {
    pub state: EngineState,
    pub actions: usize,
    pub gen_until_ms: f64,
    pub note: String,
}

#[derive(Default)]
struct Inner {
    actions: Vec<Action>,
    gen_until_ms: f64,
    state: EngineState,
    note: String,
}

pub struct ActionBuffer {
    inner: RwLock<Inner>,
}

impl Default for ActionBuffer {
    fn default() -> Self {
        Self::new()
    }
}

impl ActionBuffer {
    pub fn new() -> Self {
        Self {
            inner: RwLock::new(Inner::default()),
        }
    }

    /// 引擎侧：整体替换动作表（内部按时间排序）。
    pub fn replace_all(&self, mut actions: Vec<Action>, gen_until_ms: f64) {
        actions.sort_by(|a, b| a.at.partial_cmp(&b.at).unwrap_or(std::cmp::Ordering::Equal));
        let mut g = self.inner.write().unwrap();
        g.actions = actions;
        g.gen_until_ms = gen_until_ms;
    }

    /// 播放侧：取快照（拷贝，不持锁返回引用）。
    pub fn snapshot(&self) -> (Vec<Action>, f64) {
        let g = self.inner.read().unwrap();
        (g.actions.clone(), g.gen_until_ms)
    }

    /// 取 `[from_ms, to_ms]` 区间（宿主 `/actions` 用）。
    pub fn range(&self, from_ms: f64, to_ms: f64) -> Vec<Action> {
        let g = self.inner.read().unwrap();
        g.actions
            .iter()
            .filter(|a| a.at >= from_ms && a.at <= to_ms)
            .copied()
            .collect()
    }

    pub fn state(&self) -> EngineState {
        self.inner.read().unwrap().state
    }

    pub fn set_state(&self, s: EngineState) {
        self.inner.write().unwrap().state = s;
    }

    pub fn set_note(&self, n: impl Into<String>) {
        self.inner.write().unwrap().note = n.into();
    }

    pub fn info(&self) -> BufferState {
        let g = self.inner.read().unwrap();
        BufferState {
            state: g.state,
            actions: g.actions.len(),
            gen_until_ms: g.gen_until_ms,
            note: g.note.clone(),
        }
    }

    pub fn clear(&self) {
        let mut g = self.inner.write().unwrap();
        g.actions.clear();
        g.gen_until_ms = 0.0;
        g.state = EngineState::Idle;
        g.note.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a(at: f64, pos: f64) -> Action {
        Action { at, pos }
    }

    #[test]
    fn replace_all_sorts_and_publishes() {
        let b = ActionBuffer::new();
        b.replace_all(vec![a(2000.0, 10.0), a(0.0, 90.0), a(1000.0, 50.0)], 3000.0);
        let (acts, until) = b.snapshot();
        assert_eq!(until, 3000.0);
        assert_eq!(acts.iter().map(|x| x.at).collect::<Vec<_>>(), vec![0.0, 1000.0, 2000.0]);
    }

    #[test]
    fn replace_all_is_whole_table_replacement() {
        // 流式语义：每次都是"整张表替换"，不是追加
        let b = ActionBuffer::new();
        b.replace_all(vec![a(0.0, 10.0), a(1000.0, 20.0)], 1000.0);
        b.replace_all(vec![a(0.0, 11.0)], 500.0);
        let (acts, until) = b.snapshot();
        assert_eq!(acts.len(), 1, "旧动作必须被整体替换掉");
        assert_eq!(acts[0].pos, 11.0);
        assert_eq!(until, 500.0);
    }

    #[test]
    fn range_filters_inclusive() {
        let b = ActionBuffer::new();
        b.replace_all(vec![a(0.0, 1.0), a(1000.0, 2.0), a(2000.0, 3.0)], 2000.0);
        assert_eq!(b.range(0.0, 1000.0).len(), 2, "区间是闭区间");
        assert_eq!(b.range(1500.0, 2500.0).len(), 1);
        assert_eq!(b.range(9999.0, 99999.0).len(), 0);
    }

    #[test]
    fn state_and_note_roundtrip() {
        let b = ActionBuffer::new();
        assert_eq!(b.state(), EngineState::Idle);
        b.set_state(EngineState::Warming);
        b.set_note("预热中");
        let i = b.info();
        assert_eq!(i.state, EngineState::Warming);
        assert_eq!(i.note, "预热中");
        b.clear();
        assert_eq!(b.state(), EngineState::Idle);
        assert_eq!(b.snapshot().0.len(), 0);
    }

    #[test]
    fn concurrent_readers_never_see_partial_table() {
        use std::sync::Arc;
        let b = Arc::new(ActionBuffer::new());
        let w = Arc::clone(&b);
        let t = std::thread::spawn(move || {
            for i in 0..200 {
                let n = (i % 10) + 1;
                w.replace_all((0..n).map(|k| a(k as f64 * 100.0, 1.0)).collect(), i as f64);
            }
        });
        for _ in 0..500 {
            let (acts, _) = b.snapshot();
            // 任意时刻读到的都必须是"完整的一张表"：时间严格递增且长度合法
            let mut prev = f64::NEG_INFINITY;
            for x in &acts {
                assert!(x.at > prev, "读到了未排序/半张表");
                prev = x.at;
            }
            assert!(acts.len() <= 10);
        }
        t.join().unwrap();
    }
}
