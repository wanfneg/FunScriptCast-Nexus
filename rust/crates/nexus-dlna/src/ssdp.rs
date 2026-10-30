//! SSDP：让头显/手机能发现本机（M-SEARCH 应答 + 周期 NOTIFY）。
//!
//! 地址：239.255.255.250:1900。和 Python 版一样只做"通知 + 应答"，不做设备缓存。

use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::sync::Arc;
use std::time::Duration;

use tokio::net::UdpSocket;
use tracing::{debug, warn};

use crate::app::DlnaApp;

pub const SSDP_ADDR: Ipv4Addr = Ipv4Addr::new(239, 255, 255, 250);
pub const SSDP_PORT: u16 = 1900;

pub const ST_ROOT: &str = "upnp:rootdevice";
pub const ST_CDS: &str = "urn:schemas-upnp-org:service:ContentDirectory:1";

pub fn location(app: &DlnaApp) -> String {
    format!("{}/description.xml", app.base_url())
}

pub fn usn(kind: &str) -> String {
    let uuid = format!("uuid:{}", crate::app::DEVICE_UUID);
    // 对 root uuid 的 USN 就是 uuid 本身（Python 同款）；否则 uuid::NT。
    if kind == uuid {
        uuid
    } else {
        format!("{uuid}::{kind}")
    }
}

/// 构造一条 SSDP 应答（回 M-SEARCH）。
pub fn search_response(app: &DlnaApp, st: &str, mx: u32) -> Vec<u8> {
    let mut s = String::new();
    s.push_str("HTTP/1.1 200 OK\r\n");
    s.push_str("CACHE-CONTROL: max-age=1800\r\n");
    s.push_str(&format!("DATE: {}\r\n", http_date()));
    s.push_str("EXT:\r\n");
    s.push_str(&format!("LOCATION: {}\r\n", location(app)));
    s.push_str("SERVER: Windows/10.0 UPnP/1.0 FunScriptCast-DLNA/1.0\r\n");
    s.push_str(&format!("ST: {st}\r\n"));
    s.push_str(&format!("USN: {}\r\n", usn(st)));
    s.push_str(&format!("BOOTID.UPNP.ORG: 1\r\n"));
    s.push_str(&format!("MX: {mx}\r\n"));
    s.push_str("\r\n");
    s.into_bytes()
}

/// 构造一条 NOTIFY（alive / byebye）。
pub fn notify(app: &DlnaApp, nt: &str, alive: bool) -> Vec<u8> {
    let mut s = String::new();
    s.push_str("NOTIFY * HTTP/1.1\r\n");
    s.push_str("HOST: 239.255.255.250:1900\r\n");
    if alive {
        s.push_str("CACHE-CONTROL: max-age=1800\r\n");
        s.push_str(&format!("LOCATION: {}\r\n", location(app)));
        s.push_str("SERVER: Windows/10.0 UPnP/1.0 FunScriptCast-DLNA/1.0\r\n");
    }
    s.push_str(&format!(
        "NT: {nt}\r\nNTS: {}\r\n",
        if alive { "ssdp:alive" } else { "ssdp:byebye" }
    ));
    s.push_str(&format!("USN: {}\r\n", usn(nt)));
    s.push_str("\r\n");
    s.into_bytes()
}

/// RFC1123 日期（SSDP 要求）。不引 chrono：civil_from_days 算法（Howard Hinnant）
/// 从 UNIX 天数推年月日，DATE 头与真实时间一致（客户端确实大多不校验，
/// 但恒为 1970 的日期连日志对照都做不到）。
pub fn http_date() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (h, mi, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    // 1970-01-01 是周四
    let dow = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"][(days.rem_euclid(7)) as usize];
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as i64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mth = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if mth <= 2 { y + 1 } else { y };
    const MON: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    format!(
        "{dow}, {d:02} {} {y:04} {h:02}:{mi:02}:{s:02} GMT",
        MON[(mth - 1) as usize]
    )
}

/// 解析 M-SEARCH 的 ST 与 MX（大小写不敏感）。
pub fn parse_msearch(packet: &str) -> Option<(String, u32)> {
    if !packet.starts_with("M-SEARCH") {
        return None;
    }
    let mut st = String::new();
    let mut mx = 1u32;
    for line in packet.lines() {
        let lower = line.to_lowercase();
        if lower.starts_with("st:") {
            st = line[3..].trim().to_string();
        } else if let Some(v) = lower.strip_prefix("mx:") {
            mx = v.trim().parse().unwrap_or(1).min(5);
        }
    }
    if st.is_empty() {
        return None;
    }
    Some((st, mx))
}

/// 启动 SSDP 监听（后台任务）。返回 `(任务句柄, 停机信号发送端)`：
/// 发送端 `send(())` 后循环退出并广播 ssdp:byebye（客户端立即移除设备，
/// 不必等 max-age=1800 缓存过期——abort 任务不会执行 byebye）。
pub async fn serve(
    app: Arc<DlnaApp>,
) -> std::io::Result<(
    tokio::task::JoinHandle<()>,
    tokio::sync::oneshot::Sender<()>,
)> {
    // 与 Python 一致：显式指定组播出接口。多网卡/VPN 下不指定时系统会选错网卡，
    // LOCATION 里的 IP 与实际出接口不一致，头显就发现不了。
    // SO_REUSEADDR 必须在 bind 之前设置——bind 之后设置对本 socket 已无效
    // （1900 被另一套安装/SSDPSRV 占住时还能共存，Python 版同目标）。
    let sock2 = socket2::Socket::new(
        socket2::Domain::IPV4,
        socket2::Type::DGRAM,
        Some(socket2::Protocol::UDP),
    )?;
    let _ = sock2.set_reuse_address(true);
    sock2.bind(&SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, SSDP_PORT).into())?;
    if let Ok(ip) = app.lan_ip.parse::<Ipv4Addr>() {
        let _ = sock2.set_multicast_if_v4(&ip);
    }
    let _ = sock2.set_multicast_ttl_v4(4);
    sock2.set_nonblocking(true)?;
    let std_sock: std::net::UdpSocket = sock2.into();
    let socket = Arc::new(UdpSocket::from_std(std_sock)?);
    let group = SocketAddrV4::new(SSDP_ADDR, SSDP_PORT);
    // Windows 上 join_multicast_v4 需要先 bind 到 0.0.0.0；失败时只降级为"只能应答单播"
    if let Err(e) = socket.join_multicast_v4(SSDP_ADDR, Ipv4Addr::UNSPECIFIED) {
        warn!("SSDP 加入组播失败（{e}）：仅能响应直接单播");
    }

    let (stop_tx, mut stop_rx) = tokio::sync::oneshot::channel::<()>();
    let handle = tokio::spawn(async move {
        let mut buf = [0u8; 2048];
        let uuid_nt = format!("uuid:{}", crate::app::DEVICE_UUID);
        let kinds = [
            ST_ROOT,
            ST_CDS,
            "urn:schemas-upnp-org:device:MediaServer:1",
            uuid_nt.as_str(),
        ];
        // 启动爆发 3 次 NOTIFY（Python 同款，提高 DeoVR 发现率），此后每 60s。
        for _ in 0..3 {
            for nt in &kinds {
                let _ = socket
                    .send_to(&notify(&app, nt, true), SocketAddr::V4(group))
                    .await;
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
        let mut tick = tokio::time::interval(Duration::from_secs(60));
        tick.tick().await; // 首次立即触发：启动通告已经发过
        let mut running = true;
        while running {
            tokio::select! {
                r = socket.recv_from(&mut buf) => {
                    let Ok((n, from)) = r else {
                        // 出错（Windows 上常见 WSAECONNRESET）：稍候再试，防忙循环
                        tokio::time::sleep(Duration::from_millis(100)).await;
                        continue;
                    };
                    let text = String::from_utf8_lossy(&buf[..n]).to_string();
                    let Some((st, mx)) = parse_msearch(&text) else { continue };
                    debug!("SSDP M-SEARCH ST={st} from {from}");
                    // 只应答自己支持的 ST，其余忽略（UPnP 规范）
                    for k in &kinds {
                        if st == *k || st == "ssdp:all" {
                            let resp = search_response(&app, k, mx);
                            // 规范要求随机延迟 0..MX 秒；这里给 50ms 免得客户端超时
                            tokio::time::sleep(Duration::from_millis(50)).await;
                            let _ = socket.send_to(&resp, from).await;
                        }
                    }
                }
                _ = tick.tick() => {
                    // 周期 NOTIFY alive（60s，Python 同款）
                    for nt in &kinds {
                        let msg = notify(&app, nt, true);
                        let _ = socket.send_to(&msg, SocketAddr::V4(group)).await;
                    }
                }
                _ = &mut stop_rx => {
                    running = false;
                }
            }
        }
        // 正常退出：对全部 NT 各发一条 byebye，防止客户端把已停服务的设备
        // 按 max-age=1800 残留成僵尸 30 分钟
        for nt in &kinds {
            let msg = notify(&app, nt, false);
            let _ = socket.send_to(&msg, SocketAddr::V4(group)).await;
        }
    });
    Ok((handle, stop_tx))
}

#[cfg(test)]
mod tests {
    #[test]
    fn usn_root_uuid_is_not_doubled() {
        let uuid = format!("uuid:{}", crate::app::DEVICE_UUID);
        assert_eq!(usn(&uuid), uuid, "root uuid 的 USN 不能是 uuid::uuid");
        assert_eq!(usn(ST_ROOT), format!("{uuid}::{ST_ROOT}"));
    }

    use super::*;

    #[test]
    fn msearch_parsing() {
        let p = "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nST: ssdp:all\r\nMX: 3\r\n\r\n";
        let (st, mx) = parse_msearch(p).unwrap();
        assert_eq!(st, "ssdp:all");
        assert_eq!(mx, 3);
        // MX 上限 5
        let p2 = "M-SEARCH * HTTP/1.1\r\nST: upnp:rootdevice\r\nMX: 99\r\n";
        assert_eq!(parse_msearch(p2).unwrap().1, 5);
        // 非 M-SEARCH 忽略
        assert!(parse_msearch("NOTIFY * HTTP/1.1\r\n").is_none());
    }

    #[test]
    fn search_response_shape() {
        let app = DlnaApp::new(Arc::new(crate::library::MediaLibrary::new(vec![])), 8899, "192.168.1.5");
        let r = String::from_utf8(search_response(&app, ST_ROOT, 2)).unwrap();
        assert!(r.starts_with("HTTP/1.1 200 OK\r\n"));
        assert!(r.contains("LOCATION: http://192.168.1.5:8899/description.xml"));
        assert!(r.contains(&format!("USN: {}", usn(ST_ROOT))));
        assert!(r.ends_with("\r\n\r\n"));
    }

    #[test]
    fn notify_alive_and_byebye() {
        let app = DlnaApp::new(Arc::new(crate::library::MediaLibrary::new(vec![])), 8899, "10.0.0.2");
        let alive = String::from_utf8(notify(&app, ST_CDS, true)).unwrap();
        assert!(alive.contains("NTS: ssdp:alive"));
        assert!(alive.contains("LOCATION:"));
        let bye = String::from_utf8(notify(&app, ST_CDS, false)).unwrap();
        assert!(bye.contains("NTS: ssdp:byebye"));
        assert!(!bye.contains("LOCATION:"), "byebye 不带 LOCATION");
    }
}
