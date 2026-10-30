//! 真实 BLE 传输（`btleplug`），仅在 `--features ble` 下编译。
//!
//! 逐条对应手机端 `BleDeviceService` 与 Python 版 `channel.py` 的 BLE 部分：
//! 扫描（按广播名匹配档案）→ 连接 → 订阅通知 → **握手顺序**
//! ① D0 读设备信息（最多 3 次、每次等 1s、间隔 250ms）
//! ② 写临时限位 0x42
//! ③ 写 A10 模式
//! ④ **最后才置 ready**（ready 之前不写运动帧）
//!
//! > ⚠ **本模块尚未在真实硬件上验证**（开发机当前无设备）。
//! > 语义与 Python 版逐条对齐，但首次上机必须按 `docs/DEVICE-PORT.md` 的清单逐项核对。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use btleplug::api::{
    Central, CharPropFlags, Characteristic, Manager as _, Peripheral as _, ScanFilter, WriteType,
};
use btleplug::platform::{Adapter, Manager, Peripheral};
use futures_util::StreamExt;
use uuid::Uuid;

use crate::channel::{DeviceChannel, Transport};
use crate::protocols::{cmd_info, cmd_mode, match_toy, toy_by_id, ToyDevice, CHUNK, TOYS};

fn uuid_of(s: &str) -> Uuid {
    Uuid::parse_str(s).expect("写死的 GATT UUID 必须合法")
}

/// 扫描到的一台设备。
#[derive(Debug, Clone, serde::Serialize)]
pub struct Discovered {
    pub address: String,
    pub name: String,
    pub rssi: i16,
    pub supported: bool,
    pub toy: String,
}

/// 连接重入保护：同一时刻只允许一个 connect 流程在跑（扫描+连接+握手是长事务，
/// 并发进入会把系统 BLE 栈搞乱）。
static CONNECTING: AtomicBool = AtomicBool::new(false);

/// BLE 阻塞调用的超时（Python 基准：BleakClient timeout=20.0 + submit 30s 双保险）。
/// btleplug 在 WinRT 栈上挂起时没有超时就会冻结整条 tick 链与全部下发循环。
const CONNECT_TIMEOUT: Duration = Duration::from_secs(20);
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);

/// 连接重入保护的 RAII 复位：connect_inner panic / 任务被 abort 时
/// 也不能把 CONNECTING 永久卡在 true（否则之后所有连接请求都被拒绝直到重启，
/// Python 版有 finally 复位，这里对齐）。
struct ConnectingGuard;

impl ConnectingGuard {
    fn acquire() -> Result<Self, String> {
        CONNECTING
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map(|_| Self)
            .map_err(|_| "正在连接中，请勿重复点击".to_string())
    }
}

impl Drop for ConnectingGuard {
    fn drop(&mut self) {
        CONNECTING.store(false, Ordering::SeqCst);
    }
}

/// 一条已连接的 BLE 链路。
pub struct BleTransport {
    periph: Peripheral,
    tx_char: Characteristic,
    alive: Arc<AtomicBool>,
}

#[async_trait]
impl Transport for BleTransport {
    async fn write(&self, payload: &[u8]) -> bool {
        if !self.alive.load(Ordering::SeqCst) {
            return false;
        }
        for chunk in payload.chunks(CHUNK) {
            // 手机端同款：write-without-response；带超时——WinRT 栈挂起时
            // 裸 await 会冻结全部下发循环（Python submit 有 30s 超时同款防线）
            let w = tokio::time::timeout(
                WRITE_TIMEOUT,
                self.periph
                    .write(&self.tx_char, chunk, WriteType::WithoutResponse),
            )
            .await;
            match w {
                Ok(Ok(())) => {}
                Ok(Err(e)) => {
                    tracing::warn!(error = %e, "BLE 写入失败（视为断连窗口）");
                    self.alive.store(false, Ordering::SeqCst);
                    // WinRT 栈可能连 disconnect 都挂起：包超时，别在错误分支里
                    // 复现本文件要防的"冻结下发循环"
                    let _ = tokio::time::timeout(WRITE_TIMEOUT, self.periph.disconnect()).await;
                    return false;
                }
                Err(_) => {
                    tracing::warn!("BLE 写入超时（{}s，视为断连窗口）", WRITE_TIMEOUT.as_secs());
                    self.alive.store(false, Ordering::SeqCst);
                    let _ = tokio::time::timeout(WRITE_TIMEOUT, self.periph.disconnect()).await;
                    return false;
                }
            }
        }
        true
    }

    fn is_connected(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    async fn disconnect(&self) {
        // WinRT 栈可能挂起：包超时放弃，别把调用方（cleanup/下发循环）冻住
        let _ = tokio::time::timeout(WRITE_TIMEOUT, self.periph.disconnect()).await;
    }
}

impl BleTransport {
    pub fn address(&self) -> String {
        self.periph.id().to_string()
    }

    /// 扫描：**扫到受支持的设备就立刻返回**（手机端也是扫到即连）。
    ///
    /// 适配器/启动扫描失败走 Err——蓝牙故障和"范围内没有设备"在界面上
    /// 必须可区分，否则用户只会对着"未找到设备"反复重试。
    pub async fn scan(
        timeout: Duration,
        stop_on_supported: bool,
    ) -> Result<Vec<Discovered>, String> {
        let Ok(Some(adapter)) = first_adapter().await else {
            return Err("没有可用的蓝牙适配器".to_string());
        };
        if let Err(e) = adapter.start_scan(ScanFilter::default()).await {
            return Err(format!("蓝牙扫描启动失败：{e}"));
        }
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            let mut out = collect(&adapter).await;
            out.sort_by_key(|d| (!d.supported, -d.rssi));
            if (stop_on_supported && out.iter().any(|d| d.supported))
                || tokio::time::Instant::now() >= deadline
            {
                if let Err(e) = adapter.stop_scan().await {
                    tracing::warn!(error = %e, "BLE 停止扫描失败（不影响结果）");
                }
                return Ok(out);
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
    }

    /// 连接并走完握手。成功后通道即可写运动帧。
    ///
    /// 重入保护：CAS 抢 `CONNECTING`，失败直接拒绝；无论成功失败结束都复位。
    pub async fn connect(
        address: &str,
        toy_id: Option<&str>,
        ch: &Arc<DeviceChannel>,
    ) -> Result<Arc<Self>, String> {
        // RAII 守卫：panic / abort 也会复位 CONNECTING
        let _guard = ConnectingGuard::acquire()?;
        // 连接进行中标志要真实上报（否则状态里恒为 false，前端只能本地乐观）。
        // 与 CONNECTING 的 RAII 同款：panic/abort 时也要复位，不能永久卡 true
        struct ConnectingFlagGuard<'a>(&'a crate::channel::DeviceChannel);
        impl Drop for ConnectingFlagGuard<'_> {
            fn drop(&mut self) {
                self.0.set_connecting(false);
            }
        }
        ch.set_connecting(true);
        let _flag = ConnectingFlagGuard(ch);
        Self::connect_inner(address, toy_id, ch).await
    }

    async fn connect_inner(
        address: &str,
        toy_id: Option<&str>,
        ch: &Arc<DeviceChannel>,
    ) -> Result<Arc<Self>, String> {
        let adapter = first_adapter()
            .await
            .map_err(|e| e.to_string())?
            .ok_or("没有可用的蓝牙适配器")?;

        // Windows 必须先扫一次（把设备放进缓存），否则按地址直连常失败。
        // 同时拿到**设备当前广播的身份** —— 同一台 A10 硬件在不同模式下广播名不同
        // （ServeU / VorzePiston），档案必须以它为准。
        let _ = adapter.start_scan(ScanFilter::default()).await;
        tokio::time::sleep(Duration::from_secs(1)).await;
        let seen = collect(&adapter).await;
        let _ = adapter.stop_scan().await;

        let periph = adapter
            .peripherals()
            .await
            .map_err(|e| e.to_string())?
            .into_iter()
            .find(|p| p.id().to_string().eq_ignore_ascii_case(address))
            .ok_or_else(|| format!("未找到设备 {address}"))?;
        // 连接带超时：WinRT 栈偶发挂起时裸 connect().await 会永久卡死连接流程
        tokio::time::timeout(CONNECT_TIMEOUT, periph.connect())
            .await
            .map_err(|_| format!("连接超时（{:?}）：设备可能不在范围内或已被占用", CONNECT_TIMEOUT))?
            .map_err(|e| e.to_string())?;
        // discover 在 WinRT 上同样有挂起记录：包同款超时，挂起时 Guard 正常
        // Drop 复位 CONNECTING，不会永久卡死连接流程。
        // btleplug 的 discover_services 不返回 Result，超时是唯一的失败信号。
        if tokio::time::timeout(CONNECT_TIMEOUT, periph.discover_services())
            .await
            .is_err()
        {
            // 已建立链路：失败也要断开，别占住系统 BLE 缓存
            let _ = tokio::time::timeout(WRITE_TIMEOUT, periph.disconnect()).await;
            return Err(format!("服务发现超时（{:?}）：设备可能已断开", CONNECT_TIMEOUT));
        }

        // 候选档案顺序：强制档案 > 透传型号 > 广播名匹配 > 其余
        let mut cands: Vec<ToyDevice> = Vec::new();
        if let Some(t) = ch.forced_toy() {
            cands.push(t);
        }
        if let Some(id) = toy_id.and_then(toy_by_id) {
            cands.push(*id);
        }
        if let Some(d) = seen.iter().find(|d| d.address.eq_ignore_ascii_case(address)) {
            if let Some(t) = match_toy(Some(&d.name)) {
                cands.push(*t);
            }
        }
        for t in TOYS {
            if !cands.iter().any(|x| x.id == t.id) {
                cands.push(t);
            }
        }

        let mut last_err = String::from("没有可用的设备档案");
        for t in cands {
            let chars = periph.characteristics();
            let tx_char = chars.iter().find(|c| c.uuid == uuid_of(t.tx)).cloned();
            let rx_char = chars.iter().find(|c| c.uuid == uuid_of(t.rx)).cloned();
            let (Some(tx_char), Some(rx_char)) = (tx_char, rx_char) else {
                last_err = format!("{}: GATT 档案不匹配", t.name);
                continue;
            };
            if !tx_char.properties.contains(CharPropFlags::WRITE_WITHOUT_RESPONSE)
                && !tx_char.properties.contains(CharPropFlags::WRITE)
            {
                last_err = format!("{}: TX 不可写", t.name);
                continue;
            }
            if let Err(e) = periph.subscribe(&rx_char).await {
                last_err = format!("{}: 订阅通知失败 {e}", t.name);
                continue;
            }

            let alive = Arc::new(AtomicBool::new(true));
            let transport = Arc::new(Self {
                periph: periph.clone(),
                tx_char,
                alive: alive.clone(),
            });

            // 通知泵：解析后写回通道状态
            let ch_for_notes = Arc::clone(ch);
            let rx_uuid = rx_char.uuid;
            let mut stream = match periph.notifications().await {
                Ok(s) => s,
                Err(e) => {
                    // 该档案握手失败：退订已订阅的通知再试下一个（Python 有 stop_notify）
                    let _ = periph.unsubscribe(&rx_char).await;
                    last_err = format!("{}: 无法读取通知流 {e}", t.name);
                    continue;
                }
            };
            let alive_for_notes = alive.clone();
            tokio::spawn(async move {
                while let Some(n) = stream.next().await {
                    if n.uuid == rx_uuid {
                        ch_for_notes.on_notify(&n.value);
                    }
                }
                alive_for_notes.store(false, Ordering::SeqCst);
            });

            ch.attach_transport(transport.clone() as Arc<dyn Transport>);
            ch.set_current_toy(t);
            ch.mark_connected(address, Some(t));

            // 握手 ①②③④
            read_info_with_retry(ch, &transport).await;
            ch.apply_limits().await;
            let mode = ch.mode_override().unwrap_or(t.a10_mode);
            transport.write(&cmd_mode(mode)).await;
            ch.set_ready(true);
            return Ok(transport);
        }
        // 所有候选档案都握手失败：显式断开链路，别占住系统 BLE 缓存
        let _ = periph.disconnect().await;
        Err(last_err)
    }
}

async fn first_adapter() -> Result<Option<Adapter>, btleplug::Error> {
    let manager = Manager::new().await?;
    Ok(manager.adapters().await?.into_iter().next())
}

async fn collect(adapter: &Adapter) -> Vec<Discovered> {
    let mut out = Vec::new();
    let Ok(periphs) = adapter.peripherals().await else {
        return out;
    };
    for p in periphs {
        let props = p.properties().await.ok().flatten();
        let name = props
            .as_ref()
            .and_then(|x| x.local_name.clone())
            .unwrap_or_default();
        let rssi = props.as_ref().and_then(|x| x.rssi).unwrap_or(0);
        let toy = match_toy(Some(&name));
        out.push(Discovered {
            address: p.id().to_string(),
            name,
            rssi,
            supported: toy.is_some(),
            toy: toy.map(|t| t.id.to_string()).unwrap_or_default(),
        });
    }
    out
}

/// D0 设备信息：最多 3 次、每次等 1 秒、重试间隔 250ms。
async fn read_info_with_retry(ch: &DeviceChannel, t: &BleTransport) {
    const RETRIES: usize = 3;
    for attempt in 1..=RETRIES {
        if ch.snapshot().info.is_some() {
            return;
        }
        t.write(&cmd_info()).await;
        for _ in 0..10 {
            tokio::time::sleep(Duration::from_millis(100)).await;
            if ch.snapshot().info.is_some() || !t.is_connected() {
                return;
            }
        }
        if attempt < RETRIES {
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    }
}
