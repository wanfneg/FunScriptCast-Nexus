# FunScriptCast Central 调研报告

- 日期：2026-09-29
- 范围：原桌面端 ServeU Central（`D:\ServeU Central`）、手机端 FunScriptCast（`E:\Development\FunScriptCast`）、Nexus 与 VR 端（`E:\Development\FunScriptCast-Nexus`、`E:\Development\VRFunScriptCast`）、网页端（https://serveu.fun/player/#/media）
- 用途：为《[开发方案](开发方案.md)》提供证据基础。所有结论标注来源路径；取证失败的项明确标注"未取证"，不做推测。
- 本报告中的相对路径以各自工程根为基准；四端目录均已在 2026-09-29 实机确认存在（`E:/Development`、`D:/ServeU Central`、`%APPDATA%/ServeUCentral` 目录列表核对）。

---

## 0. 结论摘要

| 端 | 形态 | 技术栈 | 内置播放器 | 设备联动 | 预设系统 | 对新桌面端的价值 |
|---|---|---|---|---|---|---|
| 原桌面端 ServeU Central | Windows 桌面（.NET 10 + Avalonia 11.3，73.5MB 单 exe） | 见 §1.1 | **无**（六套外部播放器 IPC） | WinRT BLE 直连 + buttplug_ffi 内嵌服务器（WS :12345）+ TCode UDP :8000 | **无** | 配置/端口契约/热键/崩溃报告格式的迁移来源 |
| 手机端 FunScriptCast | Android App（Kotlin + Compose + media3） | 见 §2.1 | 有（ExoPlayer，`PlayerLink` 接口同构抽象） | BLE 私有协议直连（唯一通道） | **24 预设完整系统** | 预设系统权威实现、同步引擎算法、AI 字幕客户端契约、BLE 协议参考实现 |
| Nexus / VR 端 | PC 服务中枢 + Quest 头显 | 见 §3.1/§3.2 | Nexus 无；VR 端有（ExoPlayer 解码 → GL 纹理 → Unity） | 仅 VR 端有（BLE）；**Nexus 无任何设备能力** | VR 端有（与手机端同源） | AI 字幕服务端契约（8756/8791）、DLNA /scripts 索引、模型中心 |
| 网页端 serveu.fun/player | PWA（Vue3 + Vuetify） | 见 §4.1 | 原生 `<video>` 标签 | Web Bluetooth（仅 Chromium 系） | 无 | funscript-utils 语义、同步数学、OTA 编排、云端更新契约 |

跨端五条硬结论（详细证据见各章）：

1. **四端没有任何一端同时具备"桌面 + 内置播放器 + 设备联动"**——这正是 FunScriptCast Central 的定位空白，也是本次重构的立项依据。
2. 自有设备 BLE 协议（GATT UUID、握手流程、帧格式、速度压缩表）在网页端 / 手机端 / VR 端三处独立实现且逐字节一致（§5.1），桌面端可照第四遍移植。
3. 视频时间轴 → funscript 查表 → `moveTo` 的同步数学三端一致：`speed = |Δvalue| × 100 / dt`、lastIndex 游标去重、skipGap 跳无动作段（§5.2）。
4. AI 字幕是"客户端 PCM 推流 + Nexus :8756 推理 + :8791 就绪协议"的既有三端契约（§3.3），桌面端按契约接入即可，服务端零改动。
5. 原端**无**预设系统（§1.4，有取证过程）；预设系统唯一权威实现是手机端 `PresetDefs.kt`（VR 端持有同源副本），纯数据 + 纯算法、零 Android 依赖，可直接移植（§2.4）。

---

## 1. 原桌面端 ServeU Central（`D:\ServeU Central`）

### 1.1 概况与技术栈

安装版 Windows 桌面应用，Inno Setup 分发，`ServeUCentral.exe` 2.7.2.0，自包含单文件发布 73.5MB。作者为杭州团队（私有 git `code.starxcjy.com:1000/HangZhouCollection/软件组`，错误报告头部与 `unins000.dat` 字符串）。同机存在生态内其他应用（FunscriptFlow、VRFunScriptCast、ai.serveu.funworkshop）。

| 层 | 技术 | 证据 |
|---|---|---|
| 语言/运行时 | C# / .NET 10（10.0.2） | ErrorReports 日志 "Framework: .NET 10.0.2"；`unins000.dat` 安装清单 |
| UI 框架 | Avalonia 11.3.11 + FluentAvalonia 2.5.0 + DialogHost.Avalonia 0.8.1 + MsBox.Avalonia + Lucide.Avalonia 0.1.57 + NHotkey.Avalonia | exe strings 程序集清单；错误报告 "Avalonia Version: 11.3.11" |
| 渲染栈 | libSkiaSharp + libHarfBuzzSharp + av_libglesv2.dll（ANGLE，OpenGL ES→DirectX）——Avalonia Skia 标准配套，**非视频播放用** | 根目录三个 DLL + strings 验证（本次目录列表复核：`libSkiaSharp.dll`、`libHarfBuzzSharp.dll`、`av_libglesv2.dll` 均在） |
| 内嵌浏览器 | WebView2（WebView2Aot 1.5.0）加载 https://serveu.fun 云端页面，独立用户数据目录 `ServeUCentral.exe.WebView2` | `WebView2Loader.dll`、EBWebView Preferences 中 serveu.fun:443 白名单 |
| 设备控制 | buttplug_ffi.dll（Rust buttplug 的 FFI，x64）内嵌 Buttplug 服务器（WS 127.0.0.1:12345）；自有设备另走 WinRT BLE GATT（Microsoft.Windows.SDK.NET 投影） | PE 导出表 9 个导出 + strings `buttplug_ffi,_deviceCommandCallback` + 日志 "ButtplugServer] Server started on 127.0.0.1:12345" |
| 周边 | CommunityToolkit.Mvvm、NHotkey 3.1.5、NLog 6.0.70、NetSparkleUpdater 3.1.0-preview、Bugsnag、Newtonsoft.Json + System.Text.Json、DirectN、Microsoft.Extensions.DependencyInjection、PhoenixTools.Window 1.0.2、Starxcjy.Avalonia.HtmlRenderer、Chaos.NaCl | exe strings 版本字符串 |

取证限制：程序集经自定义打包压缩，无标准 .NET bundle 签名，`FunscriptSyncGUI\publish` 为空目录，**未能取得 deps.json/runtimeconfig.json 与中/英文 UI 明文**；依赖清单靠内嵌清单字符串反推。

### 1.2 功能清单

- 设备管理：WinRT BLE GATT 扫描/连接自有设备（`BluetoothService`，读 motorPower/signalStrength/statusText 特征），支持解锁速度上限选项。
- 固件 OTA：WiFi 局域网推送（配置存 WiFi 名称/密码；UI 有 latestVersion/changelog/forceUpdate/downloadProgress）。
- 双协议服务端（IntegrationMode 页）：内嵌 Buttplug WebSocket 服务器（默认 127.0.0.1:12345，外部 ScriptPlayer 等客户端可连入控制设备）+ TCode UDP 服务器（默认端口 8000，接收 `L0(\d+)` / `L0\d+S(\d+)` / `L0\d+I(\d+)` 指令）。
- 脚本模式：加载 .funscript 与外部播放器画面同步驱动设备；支持脚本偏移、跳过片头/间隔（SkipGap + GapDuration 60.3s）、Normal/Fixed 更新模式（固定间隔 15ms）、插值。
- 六种媒体源：PotPlayer / MPV / DeoVR(VR 播放器) / OpenFunscripter / WindowsMedia / 纯脚本（无播放器独立播放）。
- 脚本可视化：关键帧热力图（ScriptKeyframesHeatmap + Tooltip，HasHeat/bucketSize 按速度分色）、章节/书签标记预览。
- 动作栏（ActionBar）：普通/高潮/慢速三种动作模式，行程 0-100%、速度（上限 500）、反向、百分比/速度联动、空闲检测 5 秒。
- 全局热键（NHotkey）：Ctrl+Alt+M 启停动作、Ctrl+Alt+O 高潮动作、F19 慢速；遥控器按键 F13-F18（启停/行程±/速度±，步进 1%/5）。
- 崩溃报告：本地 ErrorReports/*.log（匿名用户 ID、App 版本、.NET/Avalonia 版本）+ Bugsnag + CrashReportWindow HTML 渲染。
- 自动更新：NetSparkle appcast；许可系统：`%LOCALAPPDATA%\ServeU\Licenses\serveu-ecosystem.v1.license`（SUL1 魔数加密 blob）+ activator.lock。

### 1.3 播放与联动链路——重构动机的核心证据

本程序自身不播视频（Script 独立模式除外），靠 6 个 MediaSource ViewModel 跟踪/控制外部播放器取播放进度 → 驱动设备：

| 媒体源 | 接入方式 | 证据与问题 |
|---|---|---|
| PotPlayer（config 当前选中源） | 不启动播放器；正则 `(?i)(?>potplayer)` 找窗口/进程；Win32 `SendMessageA` + WM_COPYDATA(WM_USER) 轮询官方消息 API：GetTotalTime/GetCurrentTime/SetCurrentTime/GetPlayState/SetPlayState/GetFilename/GetSpeed（COPYDATASTRUCT/lpData/cbData、POSITION_OFFSET）；定时器轮询 + lastTicks | 窗口查找脆弱、时序依赖强、位置靠轮询有延迟 |
| MPV | 用户配置 PlayerPath+Arguments 由本程序拉起 mpv.exe；硬编码命名管道 `PIPE_NAME="ServeUCentral-MPV"` JSON IPC（SendCommand/ReadAsync/Seek/PlayPause；MediaPathChanged/MediaDurationChanged/MediaSpeedChanged→变速补偿） | 默认参数模板在压缩代码内**未能取证** |
| DeoVR | TCP 客户端连 Host:Port（默认 127.0.0.1:23554，config.bak 曾用 192.168.2.129 即 Quest 头显跨局域网）；JSON 协议（playerState/commandJson/shouldBePlaying）；KeepAliveAsync 心跳 | `2026-08-14.log` 大量 "Error occurred while connecting to tcpclient"/"sending keep alive packet" 断连刷屏 |
| OpenFunscripter | 配置 PlayerPath+Port 由本程序拉起后对接 | 协议细节在压缩代码内**未能取证** |
| WindowsMedia | 视图存在；exe 内含 WMP COM DISPIDs（DISPID_WMPCONTROLS_CURRENTPOSITION/PLAY 等） | 疑似 WMP COM 自动化，证据较弱（位于 SDK 元数据区） |
| Script | 无播放器，多脚本列表 + SelectedIndex 独立播放 | — |

设备侧输出：自有设备走 WinRT BLE GATT 直写（motorPower 等特征）；同时内嵌 Buttplug WS 服务器 127.0.0.1:12345（日志实证）与 TCode UDP :8000，外部发送端连入即控。

端口契约（均可配置）：Buttplug 12345、TCode UDP 8000、DeoVR 23554、MPV 管道 ServeUCentral-MPV。

配置持久化：`%APPDATA%\ServeUCentral\config.json`（按 ViewModel 分节，本次列表复核确认存在 config.json 与 config.json.bak）；日志同目录 Logs/（NLog）。

日志实证的稳定性缺陷：

- 热键注册崩溃：NHotkey.HotkeyAlreadyRegisteredException + RegisterRemoteControlHotkey NullReferenceException，`2026-09-29.log` 连续两次启动复现；
- StartOrStopSlowMove 未观察任务异常（`ErrorReport_2026-09-28_01-01-12.log`）；
- config.json 明文存储 WiFi 密码（WiFiName/WiFiPassword），安全隐患。

**重构结论**：无内置视频播放、无媒体库；六种媒体源就是六套异构 IPC（窗口消息轮询/命名管道/TCP/外部 exe 启动），每接入一种播放器都要单独适配——这是新桌面端"内置播放器直接驱动"的直接动机。

### 1.4 预设系统：不存在

exe 明文与 config.json 中均无 preset/profile/预设 类结构（唯一命中是 NLog 的 MessageTemplates 和 Avalonia 的 ControlTheme/TemplatedParent，与预设无关）。动作参数（行程/速度/联动开关）直接平铺保存在 ActionBarViewModel 配置节，无命名预设、无场景模板、无导出导入。→ 新桌面端的预设系统只能以手机端（§2.4）为蓝本。

### 1.5 可复用资产

- **buttplug_ffi.dll 及其 9 个导出的 FFI 契约**（server_create/start/stop/destroy、server_add_device、server_set_command/connect/disconnect_callback + `__host_native/__port_native/__deviceName_native/__callback_native` 回调签名）——新桌面端可直接 P/Invoke 内嵌 Buttplug 服务器。
- config.json 的按 ViewModel 分节持久化模式（结构清晰可直接沿用为迁移映射的输入）。
- 六种媒体源的接入协议知识：PotPlayer WM_COPYDATA API 名称表 + 窗口匹配正则；MPV 命名管道 JSON IPC 管道名约定；DeoVR TCP 23554 + keep-alive；TCode UDP 8000 指令解析正则。
- funscript 热力图/章节标记的 UI 概念与数据模型（HasHeat/Heat/HasRange/bucketSize/keyframes、chapter/bookmark 悬停事件）。
- NLog 结构化 JSON 异常日志与 ErrorReport 格式（App Version/Framework/Avalonia Version/Anonymous User ID）——诊断数据格式可延续。
- 许可文件格式（SUL1 头）与 .userid 匿名标识机制；WebView2Aot 内嵌 serveu.fun 的白名单配置方式。
- WinRT BLE GATT 直连经验（GattSession/GattCharacteristic 事件源、InitializeServiceCharacteristics 模式）。

### 1.6 局限与未解事项

- `keystore.jks`（别名 serveuremot）的确切用途无代码级证据，仅知安装后新增；
- `FunscriptSyncGUI\publish` 为空目录且不属于安装器清单，疑为旧版姊妹工程遗留，无法进一步判定；
- MPV 默认参数模板、OFS 协议细节、本地化 UI 文本未能取证——配置迁移时相关字段只能按"存在即迁移、缺失即默认"处理。

---

## 2. 手机端 FunScriptCast（`E:\Development\FunScriptCast`）

### 2.1 技术栈

- Kotlin + Jetpack Compose（Material3，单 Activity + AppViewModel MVVM）：`app/build.gradle.kts:4-6`、`app/src/main/java/com/funscriptcast/MainActivity.kt`、`ui/AppViewModel.kt`（4134 行）。
- compileSdk 36 / minSdk 26 / targetSdk 35；versionCode 100 / versionName 0.18.1（`app/build.gradle.kts:23-33`）。
- Compose BOM 2025.09.00；kotlinx-coroutines-android 1.9.0（`app/build.gradle.kts:86-98`）。
- 内置播放器：androidx.media3 **ExoPlayer 1.5.1**（+ media3-datasource-okhttp）+ MD360Player4Android 2.5.0（VR 360/鱼眼渲染）（`app/build.gradle.kts:100-104, 115-116`）。
- 网络：OkHttp 4.12.0（WebDAV 源、DLNA、AI 字幕 PCM 上传）；SMB 用 smbj 0.13.0 自实现 media3 DataSource（`data/SmbDataSource.kt`）。
- 设备控制：原生 Android BluetoothGatt 自研协议（无第三方库），前台服务 `ble/BleDeviceService.kt:60`；另有蓝牙 HID 遥控 `ble/HidRemoteController.kt`。
- 持久化：SharedPreferences + 自研 SecurePrefs（Android Keystore 加密 SMB/WebDAV/AI 字幕令牌，`util/SecurePrefs.kt`）；Manifest 声明 MANAGE_EXTERNAL_STORAGE / usesCleartextTraffic=true（`AndroidManifest.xml:33, 52`）。

### 2.2 功能清单

六大页面（连接/视频/联动/预设/遥控/设置）：

- 「连接」：BLE 扫描连接 ServeU / VorzePiston（缓存 MAC 直连 + 统一扫描兜底）、外部播放器（HereSphere/DeoVR）IP:23554、远程脚本源（WebDAV/SMB/DLNA）配置与浏览（`README.md:24`，`BleDeviceService.kt:115-124`）。
- 「视频」内置播放器：本地/WebDAV/DLNA/SMB 四种源；外挂字幕（srt/ssa/ass/vtt）与内嵌轨道；播放列表/连播/循环；续播记忆（SHA-256+LRU）；倍速/画面比例/亮度音量；**VR 模式（球面/180°/鱼眼/平面/3D 左右/上下，陀螺仪+触摸视角）**；锁定与控件自动隐藏（`README.md:96-119`）。
- 「联动」外部播放器控制台：脚本热力图（点击/拖动发跳转）、播放/暂停、快捷动作（待机缓动/一键急停/一键爆发）、设备行程与速度调节（`README.md:86-90`）。
- 「预设」：24 种中文命名动作模式 + 速度滑杆（1-500 绝对速度）+ BOOST/RANDOM/播放三圆钮 + 设备行程双滑轨（`README.md:142-157`）。
- 待机缓动：空闲达设定秒数自动轻柔往返，外部动作让位；一键急停（allowMove=false）/一键爆发（暂停脚本同步接管设备）（`QuickMoves.kt:17-31`，`SyncEngine.kt:121-131`）。
- 「遥控」：手机变蓝牙 HID 键盘（Android 9+），F13-F19 固定键位遥控电脑端 ServeUCentral（急停/爆发/行程±/速度±/待机缓动），自动回连（`HidRemoteController.kt:53-59`，`README.md:174-183`）。
- 脚本加载三级规则：同目录同名 → 本地脚本文件夹（多层子目录）→ 远程脚本源兜底（`AppViewModel.kt:1031-1096, 3852-3905, 4109-4133`）。
- AI 实时字幕：PCM 采集推流 PC 端 Nexus 识别+翻译，实时显示中文字幕，模型冷启动自动暂停画面等待（最多 90s）就绪续播，与文件字幕互斥（`AiSubtitleEngine.kt:41-56`）。
- 设备设置：狂暴模式（MotorMaxPower 100）、反转方向、伪装 Vorze、固件 OTA、延迟调整 ±10/±100ms、手动控制位置、跳过无动作段（默认 60s 阈值自动快进）（`README.md:39-47`，`SyncEngine.kt:139-201`）。
- 应用内自动更新（GitHub Releases + 系统下载管理器）；「其他关联软件」页指向 FunScriptCast-Nexus 与 VRFunScriptCast（`版本更新说明.md:142-157, 188-205`）。

### 2.3 联动链路（内置播放器同构设计 = 新桌面端蓝本）

**内置播放器**：`data/VideoPlayerController.kt:40` 封装 ExoPlayer 并实现 `PlayerLink` 接口（`data/PlayerLink.kt:9-22`：state/connected StateFlow + sendPlayPause/sendSeek/disconnect）；20ms 主线程 tick 把 position/isPlaying/speed 映射为 PlayerState（`VideoPlayerController.kt:33-38, 191-212`）——**同步引擎、热力图、脚本自动加载对内置/外部播放器完全同构**。

**外部播放器（HereSphere/DeoVR）**：TCP 客户端连头显 Timestamp Server/Remote Control 端口（默认 23554，`AppViewModel.kt:120`）；线协议 = 4 字节小端长度前缀 + UTF-8 JSON（`PlayerProtocol.kt:14-21`）；下行 `{path,playerState,currentTime,duration,playbackSpeed}`；上行单字段且**值必须是字符串**（数字会让播放器断开会话）：播放 `{"playerState":"0|1"}`、跳转 `{"currentTime":"26.0"}`（`PlayerProtocol.kt:214-229`）；双向 4 零字节心跳，空帧 = 媒体复位；写走独立 writer 线程。

**funscript 解析**：`data/Funscript.kt:61-79` 取 actions[].at(ms)/pos(0-100)，防 NaN/排序；同步数学 `speed=|Δvalue|×100/dt`（`Funscript.kt:11-15, 42-54`）。

**同步引擎**：`sync/SyncEngine.kt:28`，20ms 循环（LOOP_INTERVAL_MS=20，`SyncEngine.kt:343`）；媒体时钟跟随播放器（漂移>0.5s 硬吸附、否则 0.1 比例校正，`SyncEngine.kt:226-231`）；段索引变化才发帧（lastIndex 去重，`SyncEngine.kt:296-320`）；写失败保留索引重试；支持延迟偏移、跳过无动作段（预计算 idleGaps，进入即 seek 到段尾并换算回媒体时间坐标，`SyncEngine.kt:139-201`）、一键爆发接管（externalControl）。

**BLE 协议**（三端一致，帧级细节见 §5.1）：前台服务 `ble/BleDeviceService.kt:60` 按名称扫描（ServeU=服务 31bb1111、VorzePiston=40ee1111，`DeviceProtocols.kt:26-36`）；握手 = GATT 连接 → D0 读设备信息 → `[0x42,min,max,spdHi,spdLo]` 临时限位（硬件≥150）→ `S{"isA10mode":0|1}` 模式切换（`BleDeviceService.kt:508-533`）；运动帧 `[0x01, percent, convertSpeed(speed)]` 20 字节分片 write-without-response；convertSpeed 分段压缩 v≤50→v/2、≤750→(v-50)/4+25、≤2000→(v-750)/25+200、>2000→250（`DeviceProtocols.kt:75-80`）；moveTo 内部完成行程重映射 min..max、速度按行程比例缩放、上限钳制、反转（`BleDeviceService.kt:711-725`）；连接用代际号+gattLock+写回调单槽位 continuation 等并发加固。

**手机遥控电脑端**：蓝牙 HID 固定键位 F13 急停/F14 爆发/F15-16 行程±/F17-18 速度±/F19 缓动（HID usage 0x68-0x6E，`ble/HidRemoteController.kt:53-59`）。

**DLNA**：App 侧仅客户端——SSDP M-SEARCH（239.255.255.250:1900，按 ST 含 MediaServer 过滤）+ SOAP ContentDirectory:1#Browse（`data/DlnaClient.kt:35-114, 252-278`）；源码中无任何 8899 端口引用（grep 全部 .kt 零命中）——App 通过服务发现拿完整 URL，对端口无感知。脚本索引走 Nexus 扩展端点 `GET {DLNA源站根}/scripts?base=<目录>` → `{"scripts":[{name,url}]}`（`AppViewModel.kt:4109-4133`）。

**AI 字幕客户端链路**（新桌面端按此契约对接，重点）：

1. **PCM 采集**：每次 load() 在 ExoPlayer AudioSink 链常驻透传 PcmTapProcessor（BaseAudioProcessor，`VideoPlayerController.kt:112-126` 注入），透传不改音质；开启后混单 + 线性插值重采样 16kHz int16（`AiSubtitleEngine.kt:793-921`）。
2. **分块**：Chunker 默认 3s 块 + 1s 重叠（全片评测最优，中位时序偏差 -102ms）；重叠保留块尾样本作为下块开头防时间轴漂移（`AiSubtitleEngine.kt:926-981`）。
3. **上传**：`POST {服务地址}/transcribe?lang=ja|en&video_start_ms=…&keep_from_ms=块起点+重叠/2&translate=true&partial=1`，body 为裸 LE PCM（application/octet-stream），OkHttp readTimeout 180s（`AiSubtitleEngine.kt:645-657`）；有界队列 8 块、落后播放 6s 判过期丢弃、5s 心跳日志。
4. **鉴权**：authToken 非空时对 8756 全部请求（探测/拉起/轮询/上传）附 `X-FSC-Subtitle-Token` 头（`AiSubtitleEngine.kt:341-344`，常量 :1029）。
5. **档位协商**：/api/subtitle/start 与 /transcribe 响应均可携带 recommended_chunk_sec，clamp [3,30] 运行中切换 Chunker（重叠 sec<3 取 min(1,sec/3)，`AiSubtitleEngine.kt:631-643`）；每个会话从 3s 重新起步。
6. **模型就绪状态机**：8756 主机推导出 `http://host:8791`（不是仅绑环回的 8790，`AiSubtitleEngine.kt:153-158`）：GET /api/headset/status 探活（4s）→ POST /api/subtitle/start（8s）→ 按住画面轮询 1.5s/次、上限 90s → 就绪 force 续播 / 失败只恢复自己按的暂停；连续 3 块上传失败判定模型被回收，自动重拉（120s 冷却）。
7. **渲染**：响应 `segments[{start_ms,end_ms,text,translation}]`（视频绝对时间戳）；时间重叠 + LCS 去重（≥6 字且 ≥30%）后入送达顺序显示队列（dwell=1.2+0.1×字数，夹 [1.8,5.5]s，上限 12 条，seek>2s 清队，`AiSubtitleEngine.kt:1063-1124`）；currentLine StateFlow 供 UI 渲染；会话代次（session++）守卫所有异步回调。

配置项：aiSubtitleUrl（默认空，形如 `http://192.168.2.100:8756`）、aiSubtitleToken（SecurePrefs 加密落盘，界面名「服务器访问密码」对应服务端 subtitle_config.json server.auth_token）、aiSubtitleLang（仅 ja/en）、aiSubtitleEnabled（`AppViewModel.kt:273-352`）。

### 2.4 预设系统（完整规格，新桌面端的移植蓝本）

数据结构：`app/src/main/java/com/funscriptcast/sync/PresetDefs.kt`（467 行，纯 Kotlin 无 Android 依赖，与根目录《预设系统代码导出.md》逐字一致）。

- `PresetSegment(start:Int, end:Int, speed:Int, durationMs:Long?=null)`——一段运动；durationMs 非空为显式时长（波形转换产物），null 时 = 距离/速度（`PresetDefs.kt:19-24`）。
- `PresetKeyframe(pos:Int, atMs:Long)`——波形关键帧（`PresetDefs.kt:26`）。
- `PresetDef(id, name, segments, keyframes, previewFrom, previewTo)`（`PresetDefs.kt:45-83`），派生属性：isWaveform（有 keyframes）、hasFeltWindow（previewTo>previewFrom）、playSegments（波形式在构造时经 `toSegments()` 无损转段式：相邻关键帧 = 一段，speed = 距离/时长、durationMs 记原始时间轴，durMs≤0 除零给 1 兜底，`PresetDefs.kt:29-43`）、loopTravel（一个循环总行程 = Σ|end-start|）、`loopDurationMs(speed) = loopTravel×1000/speed`、loopMs（源时间轴自然时长，仅对照）。
- 内置 24 个预设硬编码于 `object PresetDefs.all`（`PresetDefs.kt:85` 起）：段式 2 个（normal 标准往复 2 段 200u、mw 峰谷连环 M+W 合并 6 段 400u）+ 原版 Haptics 波形 5 个（classic/deep/tease/edge/rhythm，9-19 关键帧）+ 真实脚本语料挖掘 17 个（zipper/risingheat/angelbounce/psytrance/girlwants/cpr/stepclimb/tide/slopburst/gimmemore/tikthots/sugardaddy/underground/cyberdream/swallowwave/premiumwave/takeit，9-65 关键帧，每个注释标明来源影片与动作特点）；byId 映射 + DEFAULT_ID="normal"。全部参数表见《预设系统代码导出.md》第 19-48 行。

**核心语义——绝对速度**：速度滑杆值（1-500）= 下发的 Units/s（`PresetPlayer.effectiveSpeed`，`PresetPlayer.kt:115-120`：BOOST 取 500，钳到 [1,500] 再钳到设备 maxSpeedProvider）。两条推论：(1) 每段耗时 ∝ 行程：`waitMs = naturalMs × 段原速 ÷ 实际速度`，源时间轴不参与实际节奏（`PresetPlayer.kt:132-135`）；(2) 循环时长@速度 = loopTravel ÷ 速度（卡片「循环 Ns」配速标签，拖滑块实时变，BOOST 按 500 算）。

**执行流程**（`sync/PresetPlayer.kt`，158 行）：

1. 自建 `CoroutineScope(SupervisorJob+Dispatchers.Default)`——循环不受主线程限速（`PresetPlayer.kt:33`）；
2. 两个 provider 注入实时参数：maxSpeedProvider（设备速度上限）、speedControlProvider（滑杆值）（`PresetPlayer.kt:36-39`）；对外 4 个 StateFlow：playing/currentPresetId/randomMode/boostMode；
3. start() 起 while 循环逐预设播 playOneLoop，循环结束若 randomMode 则 `randomOtherThan`（排除当前不连续重复）切下一个（`PresetPlayer.kt:67-85`）；
4. playOneLoop 逐段：`dist>0` 才 `sendMove(seg.end, speed)`（dist=0 保持段只等待不发送），以 20ms tick 轮询单调时钟（SystemClock.elapsedRealtime，防深睡/NTP 跳变）等满 waitMs，期间 canContinue 校验（切预设/停止立即中断）（`PresetPlayer.kt:126-147`）；
5. sendMove 对 serveu/vorze 两通道**盲发**（`PresetPlayer.kt:149-152`），行程范围/速度上限/反转/急停全部由 DeviceChannel.moveTo 内部处理（`BleDeviceService.kt:711-725`）——预设层零设备知识，最值得复用的分层；
6. BOOST 只覆盖速度不动预设；未选预设点播放兜底 normal；RANDOM 点开时播放中立即跳一个。

**UI 形态**（`ui/Screens.kt`）三卡片（≥640dp 权重 1.9/7.4/3.5，矮屏回退整页滚动）：①设备行程卡（0-100% 双滑轨）；②预设网格卡（两列滚动，选中自动 scrollTo，`PresetGridCard:3773`）；③速度卡（1-500 滑杆 + 恢复默认 100 + BOOST 红/播放蓝/RANDOM 绿三个正圆钮，`PresetSpeedCard:3848`）。PresetTile（:3937）= 名称 + ▶标记 + 「循环 Ns」+ Canvas 波形：顶点序列 remember(def) 缓存；`buildPresetPoints`（:4083）——hasFeltWindow 预设 x=累计行程（实际节奏，任何速度下形状一致），否则 x=源时间轴整循环；previewFrom/previewTo 为行程千分比显示窗口（两端落在 pos=0 零点上，卡片左右天然齐平，to>1000 表示跨循环终点）；窗口内逐段线性裁剪、包络渐变填充 + 圆头描线 + 循环接缝点线；`presetLoopLabel`（:4115）= loopDurationMs(speed) 格式化。窗口只影响显示、永不改播放数据。

**模式互斥**：`AppViewModel.kt:3507-3539`——脚本已加载或内置视频播放中先弹确认再进预设；反向亦然（:3542-3576）；爆发/缓动接管时先停预设（:3463, 3472）；presetSpeed 持久化（拖动节流落盘，BOOST 的 500 不持久化，:206-219, 3489-3505）。

**值得复用的设计**：①数据定义纯 Kotlin 零依赖；②绝对速度语义自洽（UI 标签/波形/播放三方同源公式）；③波形式→段式统一表示（播放引擎只有一条段式路径）；④保持段表达停顿（不发 0 速帧）；⑤预设层不碰设备细节；⑥单调时钟 + 20ms tick 轮询而非裸 delay；⑦独立线程池 scope；⑧千分比窗口 + 零点对齐波形画法；⑨RANDOM 不连续重复、BOOST 记忆前值恢复。

### 2.5 可复用资产

- `PresetDefs.kt` 整文件（24 预设完整数据 + 模型 + toSegments）——纯数据可原样拷走或转 JSON；
- `PresetPlayer.kt` 播放引擎骨架——仅需替换 SystemClock 与 BLE 通道；
- 绝对速度语义公式组（loopTravel / loopDurationMs / 配速标签）；
- 波形卡片绘制算法（`Screens.kt:4083-4119` 与《预设系统代码导出.md》第三节）；
- `PlayerProtocol.kt`：DeoVR/HereSphere TCP 线协议完整实现——新桌面端如需联动 VR 头显可直接照抄帧格式；
- `Funscript.kt` 解析 + 同步数学 + `SyncEngine` 20ms 时钟跟随算法（漂移校正/lastIndex 去重/idleGaps 预计算）；
- `DeviceProtocols.kt` 全部帧构造器与速度压缩表（convertSpeed、buildMoveFrame、buildSetTemporaryLimit、buildA10Mode、buildOCMode、OTA JSON 转义）；
- AiSubtitleEngine 的 Nexus 客户端契约（**不含 PCM 抽头**）——/transcribe 参数、档位协商、令牌头、8791 状态机、会话代次守卫、LCS 去重、显示队列；
- DLNA 脚本索引端点约定与远程脚本三级加载架构；
- 《预设系统代码导出.md》：24 预设参数总表（行程/循环时长/显示窗口/数据来源）。

### 2.6 局限

- 外部播放器联动读不到头显音频：**AI 字幕仅内置播放器提供**（`README.md:138` 明示；引擎经 Bridge 绑定 _video，`AppViewModel.kt:321-333`）；
- 三模式互斥靠弹窗确认手工切换（`AppViewModel.kt:3507-3539`），停止预设后脚本同步不自动恢复；
- AI 字幕滞后画面约 4-9s（块永远刚播完才产生，`AiSubtitleEngine.kt:53`）；云端翻译后端切 25s 大块时更长；
- PcmTapProcessor 深度绑定 media3 BaseAudioProcessor/ExoPlayer AudioSink（`VideoPlayerController.kt:112-126`）——**新桌面端不能复用采集层，须自做音频抽头（VLC/MPV/ffmpeg 均可），但分块/上传/去重逻辑可整体移植**；
- 预设卡片 UI 与 Compose 深度耦合（Canvas/remember/dp）——只能复用算法思路与数据；
- BLE 是唯一设备通道（无 USB/串口），只支持 ServeU/VorzePiston 两种（`Toys.all`，`DeviceProtocols.kt:49-54`）；BleDeviceService 980 行大量并发修复，是宝贵参考也是坑清单；
- usesCleartextTraffic=true 全局放行明文（`AndroidManifest.xml:52`）；
- 预设播放 sendMove 对两通道盲发（`PresetPlayer.kt:149-152`），未连接通道静默丢弃，无设备独立预设；
- 版本说明自述：v0.17.11 大改后未做真机回归；矮屏布局曾因 Dp.Infinity 闪退（已修）——新端复刻注意同类陷阱（`版本更新说明.md:93-94, 59`）。

---

## 3. Nexus 与 VR 端

### 3.1 Nexus（PC 服务中枢，`E:\Development\FunScriptCast-Nexus`）

- 宿主：Python 纯 stdlib http.server（无 Web 框架），单进程 + 托盘 + 前端静态托管（`host_server.py:1-17`）。
- 字幕服务：独立子进程 FastAPI + uvicorn + numpy（`vendor/subtitle/run_server.py:14`），由宿主 spawn/回收（`host_server.py:132-140`）。
- ASR 引擎：audio.cpp（audiocpp_server.exe 常驻 :8081，Qwen3-ASR，CPU/CUDA 双路径）（`vendor/subtitle/audiocpp_backend.py`、`data/subtitle_config.json:15-25`）。
- 翻译引擎：llama.cpp（:8082，Sakura/Hy-MT2 GGUF），另有 Ollama/OpenAI 兼容云端后端（`vendor/subtitle/llama_backend.py`、`translate_engine.py`、`data/subtitle_config.json:38-80`）。
- 字幕管线：混合切句（RMS 静音定界 + VAD 赋时）、partial 渐进出字、短叹词幻觉抑制、行长合并门控（`vendor/subtitle/server_app.py:720-1124`）。
- 模型中心：MODELS_CATALOG 七条目，ModelScope/hf-mirror/GitHub Releases 三源、断点续传、分片自动合并、完整性复核（`host_server.py:1410-1694`）。
- DLNA：自写 SSDP + UPnP ContentDirectory（:8899，设备名 FunScriptCast-DLNA），Range 流式点播 + 外挂字幕 + .strm 代理（SSRF 防护）（`vendor/dlna/vr_dlna.py:1027-1296`）。
- funscript 索引：`GET /scripts?base=<key>` → `{base,count,scripts:[{key,url,name,basename,rel}]}`（`vr_dlna.py:621-697, 1207-1227`）。
- 设备同步：adb 把 .funscript 与视频增量同步到 Quest/安卓（`funscript_sync.py`/`video_sync.py`，无线 IP/USB/Android 11 配对、zip 打包推送、大小校验）。
- 管理 UI（:8790，仅环回）；空闲回收 server.idle_release_min 默认 15 分钟自动释放显存，可经 8791 重新拉起（`host_server.py:1346-1386`）。

**关键局限**：Nexus **无任何设备联动执行能力**（全仓库 grep buttplug/intiface 零命中），不做 funscript 内容解析；8756 `_claim_session` 进程级单客户端独占，多设备同时转写会被拒（`server_app.py:843-847`）；无字幕持久化（"二刷秒出"不存在，每次重新识别）；`data/subtitle_config.json:48` 存有明文云端 key（fix-report-2026-09-22.md F25，需用户自行轮换）。

### 3.2 VR 端（`E:\Development\VRFunScriptCast`）

- Unity 2022.3.62（Tuanjie 团结引擎）双工程：unity-prototype（Pico 基准）与 unity-prototype-meta（Quest 3 实测）（`README.md:18`、`docs/HANDOFF.md` §1）。
- XR 层：com.unity.xr.oculus 4.5.2 + com.unity.xr.openxr 1.14.4-t2 + com.bytedance.pico.xr；uGUI + TextMeshPro（`unity-prototype/Packages/manifest.json:3-10`）。
- Android 引擎：funscriptcore Kotlin 库（kotlinx-coroutines 1.9.0、okhttp 4.12.0、smbj 0.13.0、androidx.media3 1.5.1）（`funscriptcore/build.gradle.kts:8-33`）。
- 播放链路：Unity 不直接解码——funscriptcore 的 `VideoPlayerBridge.kt`（Media3 ExoPlayer，复用实例）解码到 SurfaceTexture，C++ 插件 libvideobridge.so 建 GL 纹理，Unity 每帧 GL.IssuePluginEvent 驱动 updateTexImage 后贴到 VR 屏幕（`VideoPlayerBridge.kt:1-52`、`native_plugins/videobridge`）。数据源 OkHttpDataSource + SmbDataSourceFactory + 本地文件。
- 引擎服务：回环 HTTP/JSON `EngineHttpServer` 127.0.0.1:23560（`EngineService.kt:852`），Unity 以 ~5Hz POST /api/player 上报，引擎回推 seek/playPause/path 命令走 GET /api/commands 队列；路由 /api/script、/api/preset、/api/quick、/api/device、/api/connect、/api/net/*、/api/heatmap、/api/firmware/*（`EngineService.kt:255-290`）。
- 功能：本地/SMB/WebDAV/DLNA 四类源；投影 7 种/立体 4 种格式；MR 色键抠像透传；多虚拟场景；进度条 funscript 热力图；BLE 直连双通道并发（延迟调整、跳过无动作、行程/限速/反转/狂暴、WiFi OTA）；24 预设 + RANDOM/BOOST；一键急停/爆发/待机缓动；AI 字幕（AudioSink 抽 PCM→16kHz→3s 块推 8756，头锁定 Canvas 逐句显示、临时稿先行定稿覆盖）；脚本四级自动匹配（同目录同名→本地文件夹索引→网络源同目录→远程脚本源索引，`ScriptAutoLoader.cs:13-14`）。
- 局限：EngineHttpServer(23560) 无鉴权（仅环回 + Host 白名单 + 体积/连接上限，fix-report F21 已知决策）；Unity JsonUtility 不支持 long，字幕时间戳按 int 传输（`AiSubtitleController.cs:37`，超长片溢出隐患被 90s 等待上限掩盖）；/transcribe/stream 保留但 v1.6.11 起不再使用；设备清单硬编码 `DeviceProtocols.kt Toys.all`。

### 3.3 三端契约 A/B/C（新桌面端必须保持不变）

| 契约 | 内容 | 服务端证据 |
|---|---|---|
| A · 8756 /transcribe | `POST /transcribe?lang=ja|en&video_start_ms=&keep_from_ms=&translate=true&partial=1`，裸 LE PCM body，`X-FSC-Subtitle-Token` 鉴权（`_TokenGuard`），响应 segments + recommended_chunk_sec（clamp，云端 25s/本地 3s 钳 [1,25]） | `server_app.py:398-425, 482-497` |
| B · 8791 头显协议 | GET /api/headset/status / POST /api/subtitle/start；独立端口 + 正向白名单路由 + 只回标识字段（version/code_sig/host_sig/translate_backend/recommended_chunk_sec）+ scrub_paths 脱敏 + 服务未跑回落配置推断 | `host_server.py:1271-1386, 2599-2696`（8791 兼容桩仍在，README 说已移除与代码不符——以代码为准） |
| C · DLNA :8899 /scripts | SSDP 发现 + UPnP Browse + `GET /scripts?base=` → `{scripts:[{key,url,name,basename,rel}]}` | `vr_dlna.py:621-697, 1207-1227` |

### 3.4 预设系统

Nexus 端：**无**（PC 侧不驱动设备）。VR 端：有完整预设系统，全部在 `funscriptcore/src/main/java/com/funscriptcast/sync/`，与手机版同源同名单（`PresetDefs.kt:19-79` 数据结构、`PresetDefs.kt:81-463` 24 预设、`PresetPlayer.kt` 执行引擎），配套 `QuickMoves.kt`。可复用要点：预设定义纯数据 + Kotlin 单文件，与 BLE 通道解耦，移植到 PC 只需替换发送层。

### 3.5 可复用资产

- `vendor/subtitle` 整个字幕管线（自包含快照，宿主只 spawn/回收，契约清晰）；
- 契约 A/B/C 的服务端实现与 8791 安全设计模式（独立端口 + 白名单 + 脱敏）；
- 模型下载基础设施（双通道断点续传 + .part 不扶正 + 416 处理 + 分片合并 + HEAD 复核，`host_server.py:1555-1694`）；
- DLNA /scripts 端点契约（头显 `NetBridge.kt:438-452` 已按此消费）；
- adb 增量同步模块；
- VR 端头显侧字幕客户端全套（AiSubtitleEngine.kt + AiSubtitleController.cs/SubtitleOverlay.cs）——新中枢实现 8756/8791 契约即可零改动复用；
- VR 端设备驱动栈（DeviceProtocols.kt + BleDeviceService.kt + Funscript.kt + SyncEngine/PresetPlayer/QuickMoves）——PC 上需换 BLE 栈，但帧级协议逐字节可复用；
- serveu.fun 官方同步数学：`t=mediaTime+offset → searchForIndexAfter → speed=|Δvalue|*100/Δt → moveTo(round(value*100), round(speed*rate))`（`Funscript.kt:5-15`）；
- 两份跨仓审查文档（`E:\Development\code-review-2026-09-22.md` 与 `fix-report-2026-09-22.md`，34 条发现与契约落地验证）——可直接作为契约规格书。

---

## 4. 网页端 serveu.fun/player

### 4.1 技术栈

- Vue 3 + Vite：入口 `/player/assets/index-B9hA1uqe.js`（465KB）+ CSS（707KB，内嵌 Material Design Icons 与 Roboto 字体）；vue-router（路由表 /media、/device）；Pinia（deviceStore）；Vuetify；PWA（manifest standalone，scope=/player/，含 ServiceWorker 注册）。
- Web Bluetooth API（navigator.bluetooth.requestDevice + GATT 读写）直连硬件。
- Cloudflare（static.cloudflareinsights.com beacon；站点经 CF 代理）。
- **无第三方播放器内核**：artplayer/dplayer/videojs/hls.js/flv.js/mpegts/three.js 全部 0 命中，视频用原生 `<video>` 标签。

### 4.2 功能与联动链路

- 脚本模式播放器（#/media）：本地视频/音频播放；文件/文件夹双导入与连播；外挂字幕 .vtt/.srt/.ssa/.ass（createObjectURL → track）；互动脚本导入 .funscript/.json/.csv（内置 funscript-utils：getFunscriptFromString/addFunscriptMetadata/convertFunscriptToCsv）。
- 脚本执行参数：`scriptSetting={scriptOffset:0, skipGap:!1, gapDuration:5}`（偏移/跳无动作段/判定时长）。
- 自动缩略图：隐藏 video seek 到 min(1, duration/10) → 160×120 canvas → toDataURL('image/jpeg')。
- 倍速与联动：`Math.round(speed*this.playbackRate)` 同步到设备动作速度。
- 设备管理（#/device）：Web Bluetooth 三种设备——ServeU（31bb1111/2222/3333）、伪装为 "Xiaomi 15 Pro"（FFF0 服务/FFF2 写/FFF1 读）、VorzePiston（40ee1111/2222/3333）；"伪装设备" 切 `S{"isA10mode":1}` 模拟 Vorze A10 协议以兼容第三方软件。
- 运动控制：moveTo/forceMoveTo 0-100%、一键急停、一键爆发（含爆发设置）、待机缓动、反转、运动范围/最大速度/行程速度设置（setTemporaryLimit）；狂暴模式（硬件≥150，风险告知弹窗）；高潮动作（orgasm move 独立参数，localStorage `orgasm_move`）；电压低/过热/异常告警。
- 固件升级：OWIFI 读 → O{ssid,pw,url} 写 → 连接WiFi中→下载固件→升级→自动重启重连；发现新固件/更新日志/立即更新，检查频率 7/14/30 天/每次连接/永不；测试固件通道（https://serveu.fun/test-update + 访问代码）。
- 通知：定期拉 https://serveu.fun/notification/ 按版本号弹公告；开源致谢与备案号（沪ICP备2025143603号-1）。

**通信协议**：GATT 串口透传；≤20 字节单包、>20 字节按 20 字节分片 writeValueWithoutResponse；命令队列串行、RX 特征 startNotifications 收应答、sendWithResult 默认 5s 超时；命令：`D0`（读设备信息，应答首字节 ASCII 'S'）、`S{"isA10mode":0|1}`（切换后整页 reload）、`OWIFI`（15s 超时返回 JSON）、`O{"ssid":…,"pw":…,"url":…}`、setTemporaryLimit(最大行程%,最小行程%,最大速度)。**同步机制**：播放会话单例持有 mediaUrl/subtitleUrl/script/mediaTime/lastIndex，onMediaChanged/onSubtitleChanged/onScriptChanged/onMediaEnded 事件驱动；currentTime 在 actions 时间轴查表（lastIndex 游标）→ 位置%×范围、速度×playbackRate → moveTo 写蓝牙。

与服务器仅 4 处 HTTP fetch、无 WebSocket：notification/、check-update?d=ServeU&v=硬件版本&fv=软件版本、test-update?d=ServeU&c=访问代码；域硬编码 https://serveu.fun。

### 4.3 预设系统

无传统预设系统。最接近的可复用设计是三层参数结构：scriptSetting（脚本执行参数）/ orgasmMoveSetting（高潮动作独立参数 + Linked 联动开关 + 防抖 200ms + 临时改限流再恢复）/ localStorage 持久化。"事件总线 + 会话单例 + 参数联动限流"模式可直接搬进新桌面端。

### 4.4 可复用资产

- funscript-utils 纯 JS 模块（语义可移植）；
- 字幕管线（.vtt/.srt/.ssa/.ass 校验 → track 挂载）；
- Web Bluetooth 通信封装与设备 UUID 表、命令协议（协议层原样用于桌面蓝牙栈）；
- 视频-脚本同步算法（currentTime 查表 + lastIndex 游标 + playbackRate 换算 + skipGap）；
- 缩略图生成；云端契约（notification/check-update/test-update 的请求响应结构）；固件 OTA 状态机与中文文案。

### 4.5 局限

- 零流媒体能力（无 hls/dash/flv/mpegts），只能播本地文件；
- **无 VR/全景支持**（three.js/WebGL/WebXR/equirect/panorama/stereo 全 0 命中）——VR 需求在桌面端需从零实现；
- 强依赖 Web Bluetooth（仅 Chromium 系，需 HTTPS）；
- 服务器地址硬编码，无环境配置；无云端媒体库/历史同步；
- bundle 压缩混淆无 source map，逆向维护成本高；
- 设备生态单一（三种内置配置，无 buttplug.io 通用适配层——buttplug 0 命中）。

---

## 5. 跨端交叉结论

### 5.1 BLE 协议三端一致（桌面端移植的唯一权威规格）

| 项 | 值 | 证据 |
|---|---|---|
| 服务 UUID | ServeU `31bb1111-…`（网页端并列 2222/3333 特征）；VorzePiston `40ee1111-…` | 网页端 deviceConfigs；手机端 `DeviceProtocols.kt:26-36`；VR 端同 |
| 握手 | GATT 连接 → 文本 `D0` 读设备信息（应答首字节 'S'，含硬件/软件版本）→ `[0x42,min,max,spdHi,spdLo]` 临时限位（硬件≥150）→ `S{"isA10mode":0|1}` 协议模式 | `BleDeviceService.kt:508-533`；网页端 D0 解析 |
| 运动帧 | `[0x01, percent, convertSpeed(speed)]`，20 字节分片 write-without-response | `DeviceProtocols.kt` |
| convertSpeed | v≤50→v/2；≤750→(v-50)/4+25；≤2000→(v-750)/25+200；>2000→250（输出 0-250） | `DeviceProtocols.kt:75-80` |
| 其他命令 | `S{"MotorMaxPower":75|100}` 狂暴(OC)；`OWIFI` 读 WiFi；`O{ssid,pw,url}` 写 WiFi+固件 URL | 三端一致 |
| 通道层职责 | moveTo 内部完成行程重映射 min..max、速度按行程比例缩放、上限钳制、反转 | `BleDeviceService.kt:711-725` |

### 5.2 同步数学三端一致

`speed = |Δvalue| × 100 / dt`（dt 为相邻 action 时间差；网页端再乘 playbackRate，`Funscript.kt:11-15`、`Funscript.kt:42-54`、网页端 bundle）；段索引变化才发帧（lastIndex 游标）；skipGap 预计算无动作段并整段跳过（手机端 idleGaps 预计算 + 坐标换算，原端 SkipGap+GapDuration，网页端 skipGap+gapDuration 默认 5s）。原端独有 Normal/Fixed 更新模式（固定间隔 15ms 输出节流）。

### 5.3 端口与契约一览

| 端口/管道 | 归属 | 用途 | 新桌面端取舍 |
|---|---|---|---|
| WS 127.0.0.1:12345 | 原端内嵌 Buttplug 服务器 | 外部 ScriptPlayer 等连入控制设备 | **保留**（兼容出口，复用 buttplug_ffi.dll） |
| UDP 8000 | 原端 TCode 服务器 | L0/S/I 指令 | 可选保留 |
| TCP 23554 | DeoVR/HereSphere 协议 | 头显播放器联动（手机/VR 端实现） | backlog（协议可照抄 PlayerProtocol.kt） |
| HTTP 8756 | Nexus /transcribe | AI 字幕推流 | **按契约接入** |
| HTTP 8791 | Nexus 头显协议 | 模型就绪状态机 | **按契约接入** |
| HTTP 8790 | Nexus 管理 UI（仅环回） | 自身前端 | 不对接（仅参考） |
| HTTP 8899 | Nexus DLNA + /scripts | 媒体源与脚本索引 | backlog（做 SSDP/Browse 客户端，不硬编码端口） |
| 命名管道 ServeUCentral-MPV | 原端 ↔ 外部 mpv 进程 | 外部 MPV 控制 | **废弃**（内置 libmpv 取代） |

### 5.4 生态空白点 → 本项目定位

- 四端均无"桌面 + 内置播放器 + 设备联动"组合：原端有桌面与设备但无播放器；手机/VR 端有播放器但不在桌面；网页端有播放器但受浏览器能力限制且无 VR；Nexus 有算力但无设备能力。
- 原端无预设系统、网页端无预设系统；预设唯一实现在手机/VR 端。
- AI 字幕的采集端（PCM 抽头）在所有端都深度绑定各自播放内核（media3 AudioSink / Unity 侧），桌面端必须自研——这是本项目唯一没有现成代码的核心件（分块/上传/去重/显示逻辑可整体移植，见 §2.3）。
