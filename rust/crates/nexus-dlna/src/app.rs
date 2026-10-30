//! DLNA 应用层：设备描述、SCPD、ContentDirectory Browse + DIDL 生成。

use std::path::Path;
use std::sync::Arc;

use crate::library::{
    display_title, dlna_proto, find_subtitles, is_audio, is_strm, parent_object_id, subtitle_mime,
    MediaLibrary, DLNA_FLAGS,
};

/// **必须与 Python 版一致**：UDN 变更会让 DeoVR 等已缓存客户端重新发现并重建目录树。
pub const DEVICE_UUID: &str = "a3c9e1f7-4b2d-4e5a-9c8b-1d2e3f4a5b6c";

pub const DIDL_HEAD: &str = concat!(
    r#"<?xml version="1.0" encoding="utf-8"?>"#,
    r#"<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" "#,
    r#"xmlns:dc="http://purl.org/dc/elements/1.1/" "#,
    r#"xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" "#,
    r#"xmlns:sec="http://www.sec.co.kr/" "#,
    r#"xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">"#
);

/// 格式良好的空 DIDL：DeoVR 的 contentProc 对空 Result 会「LoadXml(null)」
/// 直接崩，所以任何兜底路径都必须是这个形状——"绝不是空串"。
pub fn empty_didl() -> String {
    format!("{DIDL_HEAD}</DIDL-Lite>")
}

/// 最小 XML 文本转义（覆盖 DIDL 里会出现的五种实体）。
pub fn xml_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\u{27}' => out.push_str("&apos;"),
            _ => out.push(c),
        }
    }
    out
}

pub struct DlnaApp {
    pub library: Arc<MediaLibrary>,
    pub http_port: u16,
    pub lan_ip: String,
}

impl DlnaApp {
    pub fn new(library: Arc<MediaLibrary>, http_port: u16, lan_ip: impl Into<String>) -> Self {
        Self {
            library,
            http_port,
            lan_ip: lan_ip.into(),
        }
    }

    pub fn base_url(&self) -> String {
        format!("http://{}:{}", self.lan_ip, self.http_port)
    }

    pub fn device_description(&self) -> String {
        let uuid = DEVICE_UUID;
        format!(
            r#"<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>FunScriptCast-DLNA</friendlyName>
    <manufacturer>FunScriptCast-DLNA</manufacturer>
    <manufacturerURL>http://localhost</manufacturerURL>
    <modelDescription>Lightweight DLNA MediaServer for cloud-mounted VR video folders</modelDescription>
    <modelName>FunScriptCast-DLNA</modelName>
    <modelNumber>1.0</modelNumber>
    <UDN>uuid:{uuid}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/cds.xml</SCPDURL>
        <controlURL>/control/cds</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/cm.xml</SCPDURL>
        <controlURL>/control/cm</controlURL>
      </service>
    </serviceList>
  </device>
</root>
"#
        )
    }

    pub fn cds_scpd(&self) -> &'static str {
        CDS_SCPD
    }

    pub fn cm_scpd(&self) -> &'static str {
        CM_SCPD
    }

    /// 单个视频/音频条目（含外挂字幕的 CaptionInfo）。
    fn media_item(&self, path: &Path, key: &str, parent_id: &str, siblings: &[String]) -> String {
        let title = xml_escape(&display_title(path));
        let url = format!(
            "{}/media/{}",
            self.base_url(),
            xml_escape(&urlencoding::encode(key).to_string())
        );
        let class = if is_audio(&path.to_string_lossy()) {
            "object.item.audioItem"
        } else {
            "object.item.videoItem"
        };
        let id = xml_escape(&format!("V:{key}"));
        let pid = xml_escape(parent_id);

        if is_strm(&path.to_string_lossy()) {
            // .strm 指针：大小未知，用通用 MP4 协议信息（客户端靠流嗅探）
            let proto = xml_escape(&format!(
                "http-get:*:video/mp4:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS={DLNA_FLAGS}"
            ));
            return format!(
                "<item id=\"{id}\" parentID=\"{pid}\" restricted=\"1\"><dc:title>{title}</dc:title><upnp:class>{class}</upnp:class><res protocolInfo=\"{proto}\">{url}</res></item>"
            );
        }

        let size = std::fs::metadata(path).map(|m| m.len()).unwrap_or(0);
        let size_attr = if size > 0 {
            format!(" size=\"{size}\"")
        } else {
            String::new()
        };
        let proto = xml_escape(&dlna_proto(path));
        let mut out = format!(
            "<item id=\"{id}\" parentID=\"{pid}\" restricted=\"1\"><dc:title>{title}</dc:title><upnp:class>{class}</upnp:class><res protocolInfo=\"{proto}\"{size_attr}>{url}</res>"
        );
        for (sub_path, lang) in find_subtitles(path, siblings) {
            let Some(sub_key) = self.library.path_to_key(&sub_path) else {
                continue;
            };
            let sub_url = format!(
                "{}/subs/{}",
                self.base_url(),
                xml_escape(&urlencoding::encode(&sub_key).to_string())
            );
            let mime = xml_escape(subtitle_mime(&sub_path));
            // sec:type 按实际字幕类型标注：按 type 选解析器的客户端拿到
            // 被标成 srt 的 ass/vtt 会渲染失败（Python 版用 sub["kind"]）
            let kind = sub_path
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or("srt")
                .to_lowercase();
            let lang_attr = if lang.is_empty() {
                String::new()
            } else {
                format!(" xml:lang=\"{}\"", xml_escape(&lang))
            };
            out.push_str(&format!(
                "<res protocolInfo=\"http-get:*:{mime}:*\"{lang_attr}>{sub_url}</res><sec:CaptionInfoEx sec:type=\"{kind}\">{sub_url}</sec:CaptionInfoEx><sec:CaptionInfo sec:type=\"{kind}\">{sub_url}</sec:CaptionInfo>"
            ));
        }
        out.push_str("</item>");
        out
    }

    fn container_item(&self, id: &str, parent: &str, title: &str) -> String {
        format!(
            "<container id=\"{}\" parentID=\"{}\" restricted=\"1\"><dc:title>{}</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>",
            xml_escape(id),
            xml_escape(parent),
            xml_escape(title)
        )
    }

    fn dir_items(&self, dir: &Path, parent_id: &str, prefix_key: &str) -> Vec<String> {
        let Ok((dirs, media, all)) = self.library.list_dir(dir) else {
            return Vec::new();
        };
        let join = |name: &str| {
            if prefix_key.is_empty() {
                name.to_string()
            } else {
                format!("{prefix_key}/{name}")
            }
        };
        let mut parts = Vec::new();
        for d in dirs {
            let key = join(&d);
            parts.push(self.container_item(&format!("F:{key}"), parent_id, &d));
        }
        for m in media {
            let key = join(&m);
            let p = dir.join(&m);
            parts.push(self.media_item(&p, &key, parent_id, &all));
        }
        parts
    }

    /// Browse：返回 (DIDL, NumberReturned, TotalMatches)。
    ///
    /// 未知 ObjectID 也返回**格式良好的空 DIDL**（绝不是空串）——
    /// DeoVR 的 contentProc 对空 Result 会「LoadXml(null)」直接崩。
    pub fn browse(
        &self,
        obj_id: &str,
        flag: &str,
        start: usize,
        count: usize,
    ) -> (String, usize, usize) {
        let key = obj_id
            .strip_prefix("F:")
            .or_else(|| obj_id.strip_prefix("V:"))
            .unwrap_or(obj_id);

        let mut parts: Vec<String> = Vec::new();

        if flag == "BrowseMetadata" && !obj_id.starts_with("V:") && obj_id != "0" {
            // 先按 label 匹配根，命中即返回：防止根下同名子目录劫持 label 容器
            // （如 id="videos" 被根目录下同名 videos/ 子目录顶替，parentID 也跟着错）
            if let Some(root) = self
                .library
                .roots
                .iter()
                .find(|r| r.label.eq_ignore_ascii_case(key))
            {
                parts.push(self.container_item(&root.label, "0", &root.label));
            } else if let Some(meta) = self.library.key_to_path(key) {
                // 容器自身的元数据：必须返回**该容器自己**，而不是子项列表。
                // （返回子项会让客户端按 parentID 返回上一级时直接跳根。）
                let parent = parent_object_id(key);
                let title = meta
                    .file_name()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_default();
                parts.push(self.container_item(obj_id, &parent, &title));
            }
        } else if flag == "BrowseMetadata" && obj_id == "0" {
            // UPnP 语义：BrowseMetadata(0) 返回根容器自身这一条
            // （列子项会让自称 1 条、实发 N 条，严格按计数解析的客户端行为未定义）
            parts.push(self.container_item("0", "-1", "FunScriptCast-Nexus"));
        } else if obj_id == "0" || key.is_empty() {
            if self.library.roots.len() == 1 {
                parts = self.dir_items(&self.library.roots[0].path, "0", "");
            } else {
                for r in &self.library.roots {
                    parts.push(self.container_item(&r.label, "0", &r.label));
                }
            }
        } else if obj_id.starts_with("V:") {
            if let Some(p) = self.library.key_to_path(key) {
                if p.is_file() && flag == "BrowseMetadata" {
                    let parent = parent_object_id(key);
                    let sibling = p
                        .parent()
                        .and_then(|d| self.library.list_dir(d).ok())
                        .map(|(_, _, all)| all)
                        .unwrap_or_default();
                    parts.push(self.media_item(&p, key, &parent, &sibling));
                }
            }
        } else if let Some(p) = self.library.key_to_path(key) {
            if p.is_dir() {
                parts = self.dir_items(&p, obj_id, key);
            }
        } else if let Some(root) = self
            .library
            .roots
            .iter()
            .find(|r| r.label.eq_ignore_ascii_case(key))
        {
            parts = self.dir_items(&root.path, obj_id, key);
        }

        let total = parts.len();
        if start >= total {
            parts.clear();
        } else if start > 0 {
            parts.drain(0..start);
        }
        if count > 0 && parts.len() > count {
            parts.truncate(count);
        }
        let inner = parts.join("");
        (
            format!("{DIDL_HEAD}{inner}</DIDL-Lite>"),
            parts.len(),
            total,
        )
    }
}

const CDS_SCPD: &str = r#"<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetSearchCapabilities</name><argumentList><argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument></argumentList></action>
    <action><name>GetSortCapabilities</name><argumentList><argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument></argumentList></action>
    <action><name>GetSystemUpdateID</name><argumentList><argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument></argumentList></action>
    <action><name>Browse</name><argumentList>
      <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
      <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
      <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
      <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
      <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
      <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
      <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType><allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"#;

const CM_SCPD: &str = r#"<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name><argumentList>
      <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
      <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetCurrentConnectionIDs</name><argumentList>
      <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetCurrentConnectionInfo</name><argumentList>
      <argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
      <argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>
      <argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>
      <argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>
      <argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>
      <argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
      <argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>
      <argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"#;
#[cfg(test)]
mod tests {
    use super::*;
    use crate::library::MediaRoot;

    fn app_with(root: std::path::PathBuf) -> DlnaApp {
        let lib = Arc::new(MediaLibrary::new(vec![MediaRoot {
            path: root,
            label: "videos".into(),
        }]));
        DlnaApp::new(lib, 8899, "127.0.0.1")
    }

    #[test]
    fn browse_root_lists_dirs_and_media() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-b-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("series")).unwrap();
        std::fs::write(dir.join("movie.mp4"), vec![0u8; 128]).unwrap();
        std::fs::write(dir.join("movie.zh.srt"), b"1").unwrap();
        let app = app_with(dir.clone());

        let (didl, returned, total) = app.browse("0", "BrowseDirectChildren", 0, 0);
        assert_eq!(total, 2, "一个目录 + 一个视频");
        assert_eq!(returned, 2);
        assert!(didl.starts_with(DIDL_HEAD));
        assert!(didl.ends_with("</DIDL-Lite>"));
        assert!(didl.contains("<dc:title>series</dc:title>"));
        assert!(didl.contains("<dc:title>movie</dc:title>"));
        assert!(didl.contains("sec:CaptionInfoEx"), "外挂字幕必须挂出来");
        assert!(didl.contains("size=\"128\""), "视频要带 size");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn browse_unknown_id_is_well_formed_empty() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-u-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let app = app_with(dir.clone());
        let (didl, returned, total) = app.browse("F:nope", "BrowseDirectChildren", 0, 0);
        assert_eq!((returned, total), (0, 0));
        assert_eq!(
            didl,
            format!("{DIDL_HEAD}</DIDL-Lite>"),
            "必须是格式良好的空 DIDL"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn browse_metadata_of_child_dir_returns_itself() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-m-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("a/b")).unwrap();
        let app = app_with(dir.clone());
        let (didl, returned, _) = app.browse("F:a/b", "BrowseMetadata", 0, 0);
        assert_eq!(returned, 1);
        assert!(didl.contains("id=\"F:a/b\""), "返回容器自己");
        assert!(didl.contains("parentID=\"F:a\""), "parentID 指向父目录");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn pagination_slices_without_rescanning() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-p-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        for i in 0..5 {
            std::fs::write(dir.join(format!("v{i}.mp4")), b"x").unwrap();
        }
        let app = app_with(dir.clone());
        let (_, returned, total) = app.browse("0", "BrowseDirectChildren", 2, 2);
        assert_eq!(total, 5);
        assert_eq!(returned, 2);
        let (_, r2, _) = app.browse("0", "BrowseDirectChildren", 10, 2);
        assert_eq!(r2, 0, "起点越界返回空页");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn xml_escaping() {
        assert_eq!(xml_escape("a & b <c>"), "a &amp; b &lt;c&gt;");
        assert_eq!(xml_escape("\"q\""), "&quot;q&quot;");
    }

    #[test]
    fn description_has_dlna_doc_and_services() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-d-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let app = app_with(dir.clone());
        let d = app.device_description();
        assert!(d.contains("DMS-1.50"));
        assert!(d.contains("ContentDirectory:1"));
        assert!(d.contains("ConnectionManager:1"));
        assert!(d.contains(DEVICE_UUID));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
