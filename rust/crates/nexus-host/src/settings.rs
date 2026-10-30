//! 设置读写（对应 Python `DEFAULT_SETTINGS` / `load_settings` / `save_settings`）。
//!
//! 用 `serde_json::Value` 承载，未知键原样保留（前端/旧版本写入的键不能被吃掉）；
//! 读的时候与默认值**深合并**，缺的补上，多的一律留着。

use std::path::PathBuf;
use std::sync::Mutex;

use serde_json::{json, Map, Value};

/// 默认设置（逐键对应 Python `DEFAULT_SETTINGS`）。
pub fn defaults() -> Value {
    json!({
        "dlna_port": 8899,
        "dlna_roots": [],
        "dlna_auto_start": true,
        "start_minimized": false,
        "close_to_tray": true,
        "launch_on_boot": false,
        "theme": "light",
        "motion": "full",
        "subtitle_auto_start": false,
        "library_roots": [],
        "library_card_aspect": "16:9",
        "video_link": {},
        "mini_heatmap": true,
        "script_folder": "",
        "video_folder": "",
        "device_folder": "/sdcard/Movies",
        "device_folder_script": "/sdcard/Funscript",
        "device_folder_video": "/sdcard/Movies",
        "sync_force_full": false,
        "device": {
            "script_sync": true,
            "preset_speed": 100,
            "a10_mode": null,
            "oc_mode": false,
            "orgasm": {
                "min_percent": 0, "max_percent": 100, "max_speed": 500,
                "link_percent": false, "link_speed": false
            },
            "slow": {
                "min_percent": 0, "max_percent": 100, "max_speed": 100,
                "idle_detect_seconds": 5, "link_percent": false
            }
        },
        "sync_delete_extra": false
    })
}

/// 深合并：`base` 打底，`over` 覆盖；对象逐键递归，其它类型直接替换。
pub fn deep_merge(base: &mut Value, over: &Value) {
    match (base, over) {
        (Value::Object(b), Value::Object(o)) => {
            for (k, v) in o {
                match b.get_mut(k) {
                    Some(slot) => deep_merge(slot, v),
                    None => {
                        b.insert(k.clone(), v.clone());
                    }
                }
            }
        }
        (b, o) => *b = o.clone(),
    }
}

pub struct Settings {
    path: PathBuf,
    value: Mutex<Value>,
    /// 文件存在但读/解析失败的原因。此时 value 是默认值，**绝不能写盘**
    /// （否则一次保存就把用户配置抹成默认值；Python 同款保护）。
    read_error: Mutex<Option<String>>,
}

impl Settings {
    /// 从磁盘读；文件缺失/损坏都退回默认值（与 Python 一致，绝不因设置炸启动）。
    pub fn load(path: PathBuf) -> Self {
        let mut v = defaults();
        let mut read_error = None;
        if path.exists() {
            match std::fs::read_to_string(&path) {
                Ok(text) => match serde_json::from_str::<Value>(&text) {
                    Ok(parsed) if parsed.is_object() => deep_merge(&mut v, &parsed),
                    Ok(_) => read_error = Some("顶层不是 JSON 对象".to_string()),
                    Err(e) => read_error = Some(format!("解析失败：{e}")),
                },
                Err(e) => read_error = Some(format!("读取失败：{e}")),
            }
        }
        normalize_paths(&mut v);
        Self {
            path,
            value: Mutex::new(v),
            read_error: Mutex::new(read_error),
        }
    }

    /// 读文件失败的原因（None = 正常，或文件不存在走默认值）。
    pub fn read_error(&self) -> Option<String> {
        self.read_error.lock().unwrap().clone()
    }

    pub fn get(&self) -> Value {
        self.value.lock().unwrap().clone()
    }

    /// 点路径取值，如 `device.orgasm.max_speed`。
    pub fn get_path(&self, path: &str) -> Option<Value> {
        let g = self.value.lock().unwrap();
        let mut cur = &*g;
        for part in path.split('.') {
            cur = cur.get(part)?;
        }
        Some(cur.clone())
    }

    pub fn get_i64(&self, path: &str) -> Option<i64> {
        self.get_path(path).and_then(|v| v.as_i64())
    }

    pub fn get_f64(&self, path: &str) -> Option<f64> {
        self.get_path(path).and_then(|v| {
            v.as_f64()
                .or_else(|| v.as_i64().map(|x| x as f64))
        })
    }

    pub fn get_bool(&self, path: &str) -> Option<bool> {
        self.get_path(path).and_then(|v| v.as_bool())
    }

    pub fn get_str(&self, path: &str) -> Option<String> {
        self.get_path(path).and_then(|v| v.as_str().map(|s| s.to_string()))
    }

    /// 只改内存里的顶层键，不落盘。
    ///
    /// 启动时用注册表真值回填 `launch_on_boot` 用它：设置文件可能损坏/只读，
    /// 仅仅因为回填就整文件重写会把用户配置抹成默认值（Python `load_settings`
    /// 也只是返回真值，不动磁盘）。
    pub fn set_top_in_memory(&self, key: &str, value: Value) {
        if let Some(obj) = self.value.lock().unwrap().as_object_mut() {
            obj.insert(key.to_string(), value);
        }
    }

    /// 合并一份补丁并落盘（对象深合并；其它类型整体替换）。
    ///
    /// merge 与 write 必须同一临界区：锁外写盘时，"B 基于 A 之后的状态合并，
    /// A 的旧快照后落地"会覆盖掉 B 的改动且 B 已收到成功返回（并发保存丢更新）。
    /// write 是 tmp+rename 的小同步写，锁内做的代价可接受。
    pub fn patch(&self, patch: &Value) -> std::io::Result<Value> {
        self.guard_read_error()?;
        let mut g = self.value.lock().unwrap();
        deep_merge(&mut g, patch);
        normalize_paths(&mut g);
        let snapshot = g.clone();
        self.write(&snapshot)?;
        Ok(snapshot)
    }

    /// 读失败时拒绝写盘，并尽量把原文件备份一份（与 Python `save_settings` 同款）。
    fn guard_read_error(&self) -> std::io::Result<()> {
        let err = self.read_error.lock().unwrap().clone();
        let Some(err) = err else { return Ok(()) };
        let bak = self.path.with_extension("json.bak");
        // 备份可能失败（权限/磁盘满）：如实告知，不能谎称"已备份"让用户放心处理
        let bak_msg = match std::fs::copy(&self.path, &bak) {
            Ok(_) => format!("原文件已备份为 {}", bak.display()),
            Err(e) => format!("原文件备份失败（{e}），请先手动备份再处理"),
        };
        Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("设置文件无法解析（{err}），已拒绝覆盖以免清空配置；{bak_msg}"),
        ))
    }

    fn write(&self, v: &Value) -> std::io::Result<()> {
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let text = serde_json::to_string_pretty(v).unwrap_or_else(|_| "{}".into());
        // 先写临时文件再改名：断电不会留下半个 JSON
        let tmp = self.path.with_extension("json.tmp");
        std::fs::write(&tmp, text)?;
        std::fs::rename(&tmp, &self.path)
    }

    /// 设备段（`device` 子对象）。
    pub fn device_section(&self) -> Map<String, Value> {
        self.get_path("device")
            .and_then(|v| v.as_object().cloned())
            .unwrap_or_default()
    }
}

/// 与 Python 读设置时同款：路径类字段去引号/空白，空项丢弃。
fn normalize_paths(v: &mut Value) {
    const PATH_KEYS: [&str; 5] = [
        "script_folder",
        "video_folder",
        "device_folder",
        "device_folder_script",
        "device_folder_video",
    ];
    let Some(o) = v.as_object_mut() else { return };
    for k in PATH_KEYS {
        if let Some(s) = o.get(k).and_then(|x| x.as_str()) {
            o.insert(k.to_string(), json!(crate::extras::norm_path(s)));
        }
    }
    for k in ["dlna_roots", "library_roots"] {
        if let Some(arr) = o.get(k).and_then(|x| x.as_array()) {
            let list: Vec<Value> = arr
                .iter()
                .filter_map(|x| x.as_str())
                .map(crate::extras::norm_path)
                .filter(|s| !s.is_empty())
                .map(|s| json!(s))
                .collect();
            o.insert(k.to_string(), json!(list));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deep_merge_keeps_unknown_and_fills_missing() {
        let mut base = json!({"a": 1, "b": {"c": 2, "d": 3}});
        deep_merge(&mut base, &json!({"b": {"c": 9}, "extra": true}));
        assert_eq!(base["a"], 1, "未提到的键必须保留");
        assert_eq!(base["b"]["c"], 9, "对象逐键递归覆盖");
        assert_eq!(base["b"]["d"], 3, "对象内未提到的键保留");
        assert_eq!(base["extra"], true, "新键加入");
    }

    #[test]
    fn defaults_match_python_shape() {
        let d = defaults();
        assert_eq!(d["dlna_port"], 8899);
        assert_eq!(d["device"]["preset_speed"], 100);
        assert_eq!(d["device"]["orgasm"]["max_speed"], 500);
        assert_eq!(d["device"]["slow"]["idle_detect_seconds"], 5);
        assert_eq!(d["theme"], "light");
    }

    #[test]
    fn patch_roundtrip_and_path_get() {
        let dir = std::env::temp_dir().join(format!("nexus-settings-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.json");
        let _ = std::fs::remove_file(&p);
        let s = Settings::load(p.clone());
        s.patch(&json!({"theme": "dark", "device": {"preset_speed": 150}}))
            .unwrap();
        assert_eq!(s.get_str("theme").as_deref(), Some("dark"));
        assert_eq!(s.get_i64("device.preset_speed"), Some(150));
        // 重读一遍确认落盘
        let s2 = Settings::load(p);
        assert_eq!(s2.get_str("theme").as_deref(), Some("dark"));
        assert_eq!(s2.get_i64("device.orgasm.max_speed"), Some(500), "默认值仍补全");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 注册表真值回填只改内存：不能因为读注册表就重写（可能损坏的）设置文件。
    /// 设置文件损坏时必须拒绝保存（否则默认值 + 本次补丁会覆盖用户配置）。
    #[test]
    fn corrupt_settings_are_not_overwritten() {
        let dir = std::env::temp_dir().join(format!("nexus-settings-bad-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.json");
        std::fs::write(&p, "{not json").unwrap();
        let s = Settings::load(p.clone());
        assert!(s.read_error().is_some());
        let err = s.patch(&json!({"theme": "dark"})).unwrap_err();
        assert!(err.to_string().contains("拒绝覆盖"), "实际：{err}");
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "{not json");
        assert!(p.with_extension("json.bak").exists(), "应保留原文件备份");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 历史配置里带引号的路径在读取时就规整（Python load_settings 同款）。
    #[test]
    fn paths_are_normalized_on_load() {
        let dir = std::env::temp_dir().join(format!("nexus-settings-norm-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.json");
        std::fs::write(&p, r#"{"script_folder":"  \"C:/media\"  "}"#).unwrap();
        let s = Settings::load(p);
        assert_eq!(s.get_str("script_folder").as_deref(), Some("C:/media"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn in_memory_override_does_not_write_disk() {
        let dir = std::env::temp_dir().join(format!("nexus-settings-mem-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.json");
        let _ = std::fs::remove_file(&p);
        let s = Settings::load(p.clone());
        s.set_top_in_memory("launch_on_boot", json!(true));
        assert_eq!(s.get_bool("launch_on_boot"), Some(true));
        assert!(!p.exists(), "纯内存回填不应创建设置文件");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
