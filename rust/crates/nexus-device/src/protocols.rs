//! 设备 GATT 标识与线协议。
//!
//! 逐条对应手机端 `ble/DeviceProtocols.kt` 与 Python 版 `vendor/device/protocols.py`。
//!
//! 设备档案（名称 → service / TX / RX）：
//! - ServeU      31bb1111 / 31bb2222 / 31bb3333
//! - VorzePiston 40ee1111 / 40ee2222 / 40ee3333
//!
//! TX 命令（write-without-response，按 20 字节分片）：
//! - 文本 `"D0"`                          设备信息请求
//! - 文本 `S{"isA10mode": 0|1}`           ServeU / VorzePiston 模式切换
//! - 二进制 `[0x42, min, max, speedHi, speedLo]`   设置临时限位（硬件 >= 150）
//! - 二进制 `[0x01, percent, convertSpeed(speed)]` 移动到位置
//!
//! RX 通知：
//! - `[0x42, 1, u16BE]`  当前 minMovePercent
//! - `[0x42, 2, u16BE]`  当前 maxMoveSpeed
//! - `'S' + 10..11 字节` 设备信息

use serde::{Deserialize, Serialize};

pub const SERVEU_SERVICE: &str = "31bb1111-33e3-4f3c-a7fb-104288e7cb77";
pub const SERVEU_TX: &str = "31bb2222-33e3-4f3c-a7fb-104288e7cb77";
pub const SERVEU_RX: &str = "31bb3333-33e3-4f3c-a7fb-104288e7cb77";

pub const VORZE_SERVICE: &str = "40ee1111-63ec-4b7f-8ce7-712efd55b90e";
pub const VORZE_TX: &str = "40ee2222-63ec-4b7f-8ce7-712efd55b90e";
pub const VORZE_RX: &str = "40ee3333-63ec-4b7f-8ce7-712efd55b90e";

/// TX 分片大小（手机端同款）。
pub const CHUNK: usize = 20;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ToyDevice {
    pub id: &'static str,
    pub name: &'static str,
    pub service: &'static str,
    pub tx: &'static str,
    pub rx: &'static str,
    /// ServeU = 0，VorzePiston = 1
    pub a10_mode: i32,
}

pub const SERVEU: ToyDevice = ToyDevice {
    id: "serveu",
    name: "ServeU",
    service: SERVEU_SERVICE,
    tx: SERVEU_TX,
    rx: SERVEU_RX,
    a10_mode: 0,
};

pub const VORZE: ToyDevice = ToyDevice {
    id: "vorze",
    name: "VorzePiston",
    service: VORZE_SERVICE,
    tx: VORZE_TX,
    rx: VORZE_RX,
    a10_mode: 1,
};

pub const TOYS: [ToyDevice; 2] = [SERVEU, VORZE];

/// 按 id 找档案。
pub fn toy_by_id(id: &str) -> Option<&'static ToyDevice> {
    TOYS.iter().find(|t| t.id == id)
}

/// 扫描时按广播名匹配（手机端同款：名称包含设备名）。
pub fn match_toy(name: Option<&str>) -> Option<&'static ToyDevice> {
    let name = name?;
    let low = name.to_lowercase();
    TOYS.iter().find(|t| low.contains(&t.name.to_lowercase()))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
pub struct DeviceInfo {
    pub hardware_version: i32,
    pub software_version: i32,
    pub max_position: i32,
    pub min_position: i32,
    pub max_speed: i32,
    pub motor_power: i32,
}

impl DeviceInfo {
    /// 硬件为 0（未知）或 >= 150 才支持临时限位。
    pub fn supports_temporary_limit(&self) -> bool {
        self.hardware_version == 0 || self.hardware_version >= 150
    }
}

/// 与 Python `DeviceInfo.as_dict()` **同形**：前端读 `info.hardware` /
/// `info.software` / `info.max_pos`（app.js:3343），不能暴露 Rust 字段名。
impl Serialize for DeviceInfo {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeStruct;
        let mut st = s.serialize_struct("DeviceInfo", 7)?;
        st.serialize_field("hardware", &self.hardware_version)?;
        st.serialize_field("software", &self.software_version)?;
        st.serialize_field("max_pos", &self.max_position)?;
        st.serialize_field("min_pos", &self.min_position)?;
        st.serialize_field("max_speed", &self.max_speed)?;
        st.serialize_field("motor_power", &self.motor_power)?;
        st.serialize_field("supports_limit", &self.supports_temporary_limit())?;
        st.end()
    }
}

/// 速度压缩（上线格式）。
///
/// `<=50 → /2`；`<=750 → (v-50)/4+25`；`<=2000 → (v-750)/25+200`；`>2000 → 250`。
pub fn convert_speed(v: i32) -> i32 {
    if v <= 50 {
        return 0.max(floor_div(v, 2));
    }
    if v <= 750 {
        return floor_div(v - 50, 4) + 25;
    }
    if v <= 2000 {
        return floor_div(v - 750, 25) + 200;
    }
    250
}

#[inline]
fn floor_div(a: i32, b: i32) -> i32 {
    let q = a / b;
    if (a % b != 0) && ((a < 0) != (b < 0)) {
        q - 1
    } else {
        q
    }
}

/// Kotlin `Math.round` 语义（= `floor(x + 0.5)`，.5 向正无穷）。
///
/// Python 内建 `round` 是"银行家舍入"（.5 取偶），在 .5 边界会与手机端差 1 ——
/// 位置/速度都要逐值一致，这里统一使用 Kotlin 口径。
pub fn kotlin_round(x: f64) -> i32 {
    (x + 0.5).floor() as i32
}

pub fn cmd_info() -> Vec<u8> {
    b"D0".to_vec()
}

pub fn cmd_mode(a10_mode: i32) -> Vec<u8> {
    format!("S{{\"isA10mode\": {}}}", if a10_mode != 0 { 1 } else { 0 }).into_bytes()
}

pub fn cmd_limit(min_percent: i32, max_percent: i32, speed: i32) -> Vec<u8> {
    let m = min_percent.clamp(0, 100);
    let x = max_percent.clamp(0, 100);
    let s = speed.clamp(0, 65535);
    vec![0x42, m as u8, x as u8, ((s >> 8) & 0xFF) as u8, (s & 0xFF) as u8]
}

pub fn cmd_move(percent: f64, speed: i32) -> Vec<u8> {
    let p = kotlin_round(percent).clamp(0, 100);
    vec![0x01, p as u8, convert_speed(speed).clamp(0, 255) as u8]
}

/// 狂暴模式：默认 MotorMaxPower=75，开启=100。
pub fn cmd_oc_mode(enabled: bool) -> Vec<u8> {
    format!("S{{\"MotorMaxPower\":{}}}", if enabled { 100 } else { 75 }).into_bytes()
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Notify {
    Info(DeviceInfo),
    LimitMin(u16),
    LimitSpeed(u16),
    /// 同为 0x42 前缀但子类型未知，携带解析出的值。
    Other(u16),
    Unknown,
}

/// 通知解析。
pub fn parse_notify(data: &[u8]) -> Notify {
    if data.len() >= 4 && data[0] == 0x42 {
        let val = ((data[2] as u16) << 8) | data[3] as u16;
        return match data[1] {
            1 => Notify::LimitMin(val),
            2 => Notify::LimitSpeed(val),
            _ => Notify::Other(val),
        };
    }
    if data.first() == Some(&b'S') && data.len() >= 11 {
        let u16_at = |i: usize| -> i32 { ((data[i] as i32) << 8) | data[i + 1] as i32 };
        let info = DeviceInfo {
            hardware_version: u16_at(1),
            software_version: u16_at(3),
            max_position: u16_at(5) / 100,
            min_position: u16_at(7) / 100,
            max_speed: u16_at(9),
            motor_power: if data.len() > 11 { data[11] as i32 } else { 0 },
        };
        return Notify::Info(info);
    }
    Notify::Unknown
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kotlin_round_boundaries() {
        assert_eq!(kotlin_round(0.5), 1);
        assert_eq!(kotlin_round(1.5), 2);
        assert_eq!(kotlin_round(2.5), 3);
        assert_eq!(kotlin_round(-0.5), 0);
    }

    #[test]
    fn convert_speed_segments() {
        assert_eq!(convert_speed(0), 0);
        assert_eq!(convert_speed(50), 25);
        assert_eq!(convert_speed(100), 37);
        assert_eq!(convert_speed(750), 200);
        assert_eq!(convert_speed(775), 201);
        assert_eq!(convert_speed(2000), 250);
        assert_eq!(convert_speed(2001), 250);
        assert_eq!(convert_speed(-10), 0);
    }

    #[test]
    fn cmd_shapes() {
        assert_eq!(cmd_info(), b"D0");
        assert_eq!(cmd_mode(0), "S{\"isA10mode\": 0}".as_bytes());
        assert_eq!(cmd_mode(1), "S{\"isA10mode\": 1}".as_bytes());
        assert_eq!(cmd_limit(10, 90, 0x1234), vec![0x42, 10, 90, 0x12, 0x34]);
        assert_eq!(cmd_move(50.0, 100), vec![0x01, 50, 37]);
        assert_eq!(cmd_oc_mode(true), "S{\"MotorMaxPower\":100}".as_bytes());
        assert_eq!(cmd_oc_mode(false), "S{\"MotorMaxPower\":75}".as_bytes());
    }

    #[test]
    fn parse_notify_kinds() {
        assert_eq!(parse_notify(&[0x42, 1, 0x00, 0x05]), Notify::LimitMin(5));
        assert_eq!(parse_notify(&[0x42, 2, 0x01, 0x00]), Notify::LimitSpeed(256));
        assert_eq!(parse_notify(&[0x42, 9, 0x00, 0x01]), Notify::Other(1));
        assert_eq!(parse_notify(&[0x00]), Notify::Unknown);

        let mut b = vec![b'S'];
        b.extend_from_slice(&[0x00, 0x96]); // hw 150
        b.extend_from_slice(&[0x00, 0x02]); // sw 2
        b.extend_from_slice(&[0x27, 0x10]); // maxPos 10000 → 100
        b.extend_from_slice(&[0x00, 0x00]); // minPos 0
        b.extend_from_slice(&[0x01, 0x5E]); // maxSpeed 350
        b.push(80);
        match parse_notify(&b) {
            Notify::Info(i) => {
                assert_eq!(i.hardware_version, 150);
                assert_eq!(i.software_version, 2);
                assert_eq!(i.max_position, 100);
                assert_eq!(i.min_position, 0);
                assert_eq!(i.max_speed, 350);
                assert_eq!(i.motor_power, 80);
                assert!(i.supports_temporary_limit());
            }
            other => panic!("期望 Info，得到 {other:?}"),
        }
    }

    #[test]
    fn supports_limit_by_hardware() {
        let mut i = DeviceInfo::default();
        assert!(i.supports_temporary_limit(), "hw=0（未知）应放行");
        i.hardware_version = 149;
        assert!(!i.supports_temporary_limit());
        i.hardware_version = 150;
        assert!(i.supports_temporary_limit());
    }

    #[test]
    fn match_by_advertised_name() {
        assert_eq!(match_toy(Some("ServeU-1234")).map(|t| t.id), Some("serveu"));
        assert_eq!(match_toy(Some("vorzepiston")).map(|t| t.id), Some("vorze"));
        assert_eq!(match_toy(Some("Xiaomi Band")), None);
        assert_eq!(match_toy(None), None);
    }

    /// `/api/device/state` 的 info 必须与 Python `DeviceInfo.as_dict()` 同形：
    /// 前端读 `info.hardware` / `info.software`（app.js:3343）。
    #[test]
    fn info_serializes_with_python_field_names() {
        let info = DeviceInfo {
            hardware_version: 150,
            software_version: 2,
            max_position: 100,
            min_position: 0,
            max_speed: 350,
            motor_power: 80,
        };
        let v = serde_json::to_value(info).unwrap();
        assert_eq!(v["hardware"], 150);
        assert_eq!(v["software"], 2);
        assert_eq!(v["max_pos"], 100);
        assert_eq!(v["min_pos"], 0);
        assert_eq!(v["max_speed"], 350);
        assert_eq!(v["motor_power"], 80);
        assert_eq!(v["supports_limit"], true);
        assert!(v.get("hardware_version").is_none(), "不能输出 Rust 内部字段名");
    }
}
