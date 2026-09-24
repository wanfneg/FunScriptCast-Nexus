# 就地更新（应用内「立即安装」）的沙盒验证 —— 复现 1.0.39 的失败 + 验证修复
#
#   pwsh -NoProfile -File tests\update_inplace.ps1
#
# 背景（用户实测 1.0.39）：应用内下载安装包 → 立即安装 → 卡在"正在关闭应用程序…"
# 30 秒 → "正在撤销修改…" → 升级失败；而手动双击同一个安装包正常。
# Inno 日志给出根因：
#   RestartManager found an application using one of our files: Python
#   Shutting down applications using our files.   ← 等了 30 秒
#   Some applications could not be shut down.     ← /SUPPRESSMSGBOXES 默认 Abort
#   User canceled the installation process. Rolling back changes.
# 那个 "Python" 就是老实现的交接进程 —— 它自己跑在 `<app>\runtime\python.exe`，
# 于是被 RestartManager 当成"占着待覆盖文件的应用"，而它又是安装器的父进程，
# 关不掉 ⇒ 静默安装只能中止。
#
# 本脚本用**独立身份**（独立 AppId / AppName / 目录 / 快捷方式名）装一份沙盒，
# 全程不碰用户真实安装（D:\FunScriptCast-Nexus）：
#   ① 装沙盒（真实载荷，128MB，行为与真实安装一致）
#   ② 【复现】起一个"住在安装目录里的 python"（模拟老交接进程/字幕服务），
#      直接静默跑安装包 → 期望失败（RestartManager 中止）
#   ③ 【修复】用新的 cmd.exe 交接脚本（host_server._build_update_waiter 生成）跑：
#      该 python 还活着但没占端口时……先让它占着 8756，交接脚本应当**等端口释放**
#      再安装，最终 rc=0（而不是撞上 RestartManager）
#   ④ 交接脚本的收尾步：把应用拉起来（这里指向一个写标记文件的 .cmd，避免真的
#      启动一个应用实例）
#
# 用法要点：`_build_update_waiter()` / `_update_wait_ports()` 是纯函数，测试直接调，
# 不需要 frozen 的应用。

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repo = Split-Path -Parent $PSScriptRoot
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
if (-not (Test-Path $iscc)) { throw "找不到 Inno Setup 编译器：$iscc" }

$sandboxName = 'FunScriptCast-Nexus-Sandbox'
$sandboxGuid = [guid]::NewGuid().ToString().ToUpper()
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$uninsKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{$sandboxGuid}_is1"
$desktop = [Environment]::GetFolderPath('Desktop')
$deskLink = Join-Path $desktop "$sandboxName.lnk"

$root = Join-Path $repo 'tests\_sandbox-update'
$appDir = Join-Path $root 'app'
$outDir = Join-Path $root 'out'
$logDir = Join-Path $root 'logs'
Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $appDir, $outDir, $logDir | Out-Null

$failed = New-Object System.Collections.Generic.List[string]
function Assert-That([bool]$cond, [string]$what) {
    if ($cond) { Write-Host "  OK   $what" }
    else { Write-Host "  FAIL $what"; $script:failed.Add($what) }
}
function Get-RunValue([string]$name) {
    $item = Get-ItemProperty -Path $runKey -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    $prop = $item.PSObject.Properties[$name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}
function Read-Log([string]$p) {
    # ⚠ 必须显式 [string]：`-match` 作用在数组上会返回"匹配到的元素数组"，
    # 而 Assert-That([bool]) 收到 Object[] 就直接抛类型转换错（整轮测试中止，
    # 报错行还指向断言那一行，看起来像断言写错了）。实测踩过一次。
    if (Test-Path $p) { return [string](Get-Content $p -Raw -Encoding UTF8) }
    return ''
}
function Port-Busy([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

Write-Host "[1/8] 生成沙盒安装包（同 setup.iss 的段落，只换身份）…" -ForegroundColor Cyan
# 前置：清掉上一轮可能残留的沙盒安装器（连内层 setup.tmp 一起），否则它握着的日志
# 文件会让本次 ① 直接 exit 1，看起来像"安装器坏了"，实则是测试自己没收干净。
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -like 'sandbox-setup*' } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500
$issText = Get-Content -Raw -Encoding UTF8 (Join-Path $repo 'installer\setup.iss')
function Get-Section([string]$text, [string]$name) {
    $m = [regex]::Match($text, "(?ms)^\[$name\][^\r\n]*\r?\n(.*?)(?=^\[|\Z)")
    if (-not $m.Success) { throw "setup.iss 里找不到 [$name] 段" }
    return "[$name]`r`n" + $m.Groups[1].Value.TrimEnd() + "`r`n"
}
$tasks = Get-Section $issText 'Tasks'
$icons = Get-Section $issText 'Icons'
$registry = Get-Section $issText 'Registry'
$files = Get-Section $issText 'Files'
# [Files] 的 Source 是相对 installer\ 的（..\dist-app\…）。本脚本把 .iss 生成在
# tests\_sandbox-update\ 下，相对基准变了 ⇒ 换成绝对路径，否则报"源文件不存在"。
$files = $files.Replace('..\dist-app', (Join-Path $repo 'dist-app'))
$code = Get-Section $issText 'Code'
# 只砍掉 `InitializeSetup`，**保留 CurStepChanged（F30 回删）与 PrepareToInstall（安装前
# 清残留进程）** —— ④ 步要验的正是后者。
#   · 必须砍 InitializeSetup 的原因：它查的是**写死的真实 AppId** 卸载项，于是沙盒安装器
#     会读到用户真实安装（D:\FunScriptCast-Nexus）并弹出"检测到同版本，要重新安装吗？"
#     的 Yes/No 对话框；`/VERYSILENT /SUPPRESSMSGBOXES` **不会**自动回答 [Code] 的 MsgBox
#     （实测：安装器就停在那儿，桌面上真弹了个框，只能手工杀掉）。
#   · 砍法是"从 function InitializeSetup( 到它自己的 end; 行"，而不是像早先那样把后面
#     所有函数一起砍掉（那样 ④ 步就测了个空气）。
$codeUnderTest = [regex]::Replace(
    $code, "(?ms)^function InitializeSetup\(.*?\r?\nend;\r?\n", "")
if ($codeUnderTest -match 'function InitializeSetup') { throw '未能移除 InitializeSetup' }
# ⚠ 匹配函数名就够了，别写 `procedure xxx` —— PrepareToInstall 是 **function**
# （返回 String），写成 procedure 会假报"丢了"（已经这么骗过自己一次）。
if ($codeUnderTest -notmatch 'PrepareToInstall') { throw 'PrepareToInstall 丢了（④ 步会假通过）' }
if ($codeUnderTest -notmatch 'CurStepChanged') { throw 'CurStepChanged 丢了（F30 断言会假通过）' }

# ── 另编一份"修复前"的安装器（取自上一个提交的 setup.iss：没有 PrepareToInstall）──
# 为什么要两份：新版安装器**自己就会**把安装目录里的残留 python 清掉，于是"用新版
# 复现 RM 命中"已经不可能了（那正是修复生效的证明）。② 步要复现的是**当年那个坑**，
# 就得用当年的安装器 —— 同一份载荷、同一个身份，只差 PrepareToInstall 这一段。
$issPrev = & git -C $repo show 'HEAD:installer/setup.iss' | Out-String
$codePrev = [regex]::Replace(
    (Get-Section $issPrev 'Code'), "(?ms)^function InitializeSetup\(.*?\r?\nend;\r?\n", "")
if ($codePrev -match 'PrepareToInstall') { throw '上一版 setup.iss 里居然已有 PrepareToInstall（A/B 前提不成立）' }

$sandboxIss = Join-Path $root 'sandbox.iss'
$body = @"
#define MyAppName "$sandboxName"
#define MyAppExeName "FunScriptCast-Nexus.exe"
#define MyAppPublisher "Sandbox"
#define MyAppVersion "1.0.39"

[Setup]
AppId={{$sandboxGuid}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName=$appDir
PrivilegesRequired=lowest
OutputDir=$outDir
OutputBaseFilename=sandbox-setup
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableProgramGroupPage=yes
SetupLogging=yes
UsePreviousAppDir=yes
Compression=lzma2/fast

$files
$tasks
$icons
$registry
$codeUnderTest
"@
Set-Content -Path $sandboxIss -Value $body -Encoding UTF8
& $iscc /Qp "/DMyAppVersion=1.0.39" $sandboxIss | Out-Host
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败（exit=$LASTEXITCODE）" }
$setup = Join-Path $outDir 'sandbox-setup.exe'
Assert-That (Test-Path $setup) '沙盒安装包编译成功（真实载荷，新版）'

# 第二份：修复前（无 PrepareToInstall），只用于 ② 的复现
$sandboxIssPrev = Join-Path $root 'sandbox-old.iss'
# 必须分两行：PS 里把 .Replace() 链换行接在 `)` 之后会被当成新语句（ParserError 实测）
$bodyPrev = $body.Replace('OutputBaseFilename=sandbox-setup', 'OutputBaseFilename=sandbox-setup-old')
$bodyPrev = $bodyPrev.Replace($codeUnderTest, $codePrev)
Set-Content -Path $sandboxIssPrev -Encoding UTF8 -Value $bodyPrev
& $iscc /Qp "/DMyAppVersion=1.0.39" $sandboxIssPrev | Out-Host
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败（旧版，exit=$LASTEXITCODE）" }
$setupOld = Join-Path $outDir 'sandbox-setup-old.exe'
Assert-That (Test-Path $setupOld) '沙盒安装包编译成功（同载荷的"修复前"版本，供 ② 复现）'

try {
    Write-Host "[2/8] ① 安装沙盒（模拟用户已装 1.0.39）…" -ForegroundColor Cyan
    $p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        '/TASKS=desktopicon', "/DIR=$appDir", "/LOG=$(Join-Path $logDir 'install1.log')")
    Assert-That ($p.ExitCode -eq 0) "① 沙盒安装退出码 0（实际 $($p.ExitCode)）"
    Assert-That (Test-Path (Join-Path $appDir 'FunScriptCast-Nexus.exe')) '① 沙盒里已有应用 exe'
    Assert-That (Test-Path (Join-Path $appDir 'runtime\python.exe')) '① 沙盒里已有自带运行时'

    Write-Host "[3/8] ② 【复现】安装目录里的 python 当安装器父进程（老交接进程拓扑）…" -ForegroundColor Cyan
    # 能稳定复现的是"RestartManager 发现安装目录里有我们的 python，并开始关它"这一步
    # （用户日志里的同一条）。最后是"卡 30 秒中止回滚"还是"25 秒后勉强装上"，取决于
    # RM 到底能不能关掉它 —— 用户那次关不掉（他的安装目录里同时还有字幕服务/audiocpp
    # 在退），本沙盒里 RM 能关掉。所以这一条**硬断言 RM 命中**，结局只如实打印。
    $fakeWaiter = Join-Path $root 'fake_old_waiter.py'
    $innerLog = Join-Path $logDir 'repro-inner.log'
    # 前置：清掉上一轮可能残留的沙盒进程并等端口空出来 —— 否则假交接进程 bind 失败，
    # 后面"已占住 8756"这条断言会假失败（第二次跑就踩到了）。
    Get-Process -Name 'python' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$appDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline -and (Port-Busy 8756)) { Start-Sleep -Milliseconds 300 }
    Assert-That (-not (Port-Busy 8756)) '② 前置：8756 已空（可以开始复现）'
    [IO.File]::WriteAllText($fakeWaiter, @"
import socket, subprocess, time
try:                      # 端口只是"顺手占着"：绑不上也要继续，否则这条复现会因为
    s = socket.socket()   # 环境里端口抖动变成假失败（已经踩过两次）
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 8756))
    s.listen()
    print("fake old waiter: holding 8756 and parenting the installer", flush=True)
except Exception as e:
    print("fake old waiter: bind failed (%s), continuing anyway" % e, flush=True)
# 这里故意用**修复前**的安装器（无 PrepareToInstall），否则新版会先把本进程清掉，
# 根本复现不出"RestartManager 关不掉它"这一幕。
rc = subprocess.run([r"$setupOld", "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                     r"/DIR=$appDir", r"/LOG=$innerLog"]).returncode
print("inner installer rc=%d" % rc, flush=True)
time.sleep(1)
"@, (New-Object System.Text.UTF8Encoding($false)))
    $t0 = Get-Date
    $busyPy = Start-Process -FilePath (Join-Path $appDir 'runtime\python.exe') -PassThru `
        -ArgumentList $fakeWaiter -WindowStyle Hidden
    Start-Sleep -Seconds 2
    # 注意：这里**不该**断言"假交接进程还活着" —— 修复后的安装器会在 PrepareToInstall
    # 里把它清掉（这正是要验的行为），所以活不活着取决于跑的是哪一版安装器。这里只记录。
    Write-Host "      （假交接进程 PID $($busyPy.Id) 两秒后是否还活着：$(-not $busyPy.HasExited)）"
    $busyPy.WaitForExit(240000) | Out-Null
    $stall = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $reproLog = Read-Log $innerLog
    Assert-That ($reproLog -match 'RestartManager found an application using one of our files') `
        '② 复现：Inno 报"RestartManager 发现占用我们文件的应用"（用户日志同款）'
    $outcome = if ($reproLog -match 'Rolling back changes') { '回滚（与用户实测一致）' } `
               elseif ($reproLog -match 'succeeded') { 'RM 关掉 python 后勉强装上' } else { '未知' }
    Write-Host "      （本次结局：$outcome；从启动假进程到安装结束共 $($stall)s）"
    if ($busyPy -and -not $busyPy.HasExited) { Stop-Process -Id $busyPy.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1

    Write-Host "[4/8] ③ 【修复】交接脚本必须先**等端口释放**（模拟还没死透的字幕服务）…" -ForegroundColor Cyan
    # 这一条才是修复的核心：用户那次 RM 关不掉的正是"还在退的服务"。新版交接脚本
    # 先等端口空出来，所以哪怕子进程还在死，也不会撞上 RestartManager。
    $holder = Join-Path $root 'fake_service.py'
    [IO.File]::WriteAllText($holder, @'
import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", 8756))
s.listen()
print("fake subtitle service holding 8756 for 25s", flush=True)
time.sleep(25)
'@, (New-Object System.Text.UTF8Encoding($false)))
    $holderPy = Start-Process -FilePath (Join-Path $appDir 'runtime\python.exe') -PassThru `
        -ArgumentList $holder -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and -not (Port-Busy 8756)) { Start-Sleep -Milliseconds 300 }
    Assert-That (Port-Busy 8756) '③ 假"字幕服务"已占住 8756（进程还活着）'

    # 用 host_server 里的**同一个**生成函数（纯函数，不需要 frozen 应用）
    $gen = & (Join-Path $repo '.venv\Scripts\python.exe') -c @"
import sys
sys.path.insert(0, r'$repo')
import host_server
print(host_server._build_update_waiter(
    appdir=__import__('pathlib').Path(r'$appDir'),
    setup=__import__('pathlib').Path(r'$setup'),
    app_exe=__import__('pathlib').Path(r'$appDir\FunScriptCast-Nexus.exe'),
    logfile=__import__('pathlib').Path(r'$logDir\inplace.log'),
    host_pid=999999))
"@
    $genText = (@($gen) -join "`n")
    Assert-That ($genText -match 'set "PORTS=') '③ 交接脚本已生成（含端口等待）'
    Assert-That ($genText -match 'netstat -ano') '③ 交接脚本会等端口释放（不再撞 RestartManager）'
    $waiter = Join-Path $logDir '_apply_update.cmd'
    [IO.File]::WriteAllText($waiter, $genText, (New-Object System.Text.UTF8Encoding($false)))

    # 收尾步（生产是 `start "" "%APPEXE%"`）在测试里换成"写标记文件"，理由：
    #   · 生产那行由下面的字符串断言锁住（GUI 应用 + start = 无窗口，行为正常）；
    #   · 测试里若真去 `start "" x.cmd`，cmd 会用 `cmd /K` 起批处理 ⇒ 那个子进程
    #     永不退出，而 `Start-Process -Wait` **连子进程一起等** ⇒ 测试挂死一小时
    #     （第一次就这么挂的，还在桌面上留了个控制台窗口）。
    $marker = Join-Path $root 'relaunched.marker'
    $waiterText = [IO.File]::ReadAllText($waiter)
    Assert-That ($waiterText -match [regex]::Escape('start "" "%APPEXE%"')) `
        '③ 交接脚本的收尾步是 start "" 应用（生产行为，不被测试改写）'
    $waiterText = $waiterText.Replace('start "" "%APPEXE%"',
        ('>"' + $marker + '" echo relaunched'))
    [IO.File]::WriteAllText($waiter, $waiterText, (New-Object System.Text.UTF8Encoding($false)))

    $t0 = Get-Date
    $w = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $waiter -Wait -PassThru -WindowStyle Hidden
    $dt = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $waiterLog = Read-Log (Join-Path $logDir 'inplace.log')
    Write-Host "      （交接日志）`n" + (($waiterLog -split "`n" | Where-Object { $_ -match '^\[waiter\]' }) -join "`n")
    Assert-That ($waiterLog -match 'host exited') '③ 交接脚本确认宿主已退出（PID 不存在即视为已退出）'
    Assert-That ($waiterLog -match 'ports released') '③ 交接脚本**等到端口释放**才动手（F：不再撞 RestartManager）'
    Assert-That ($dt -ge 20) "③ 确实等了那个占端口的进程（实测 $($dt)s ≥ 20s）"
    Assert-That ($waiterLog -match 'attempt 1 exit=0') '③ 静默安装一次成功（exit=0）'
    Assert-That (Test-Path $marker) '③ 装完把应用拉起来了（标记文件已生成）'
    # 本次交接脚本触发的安装到底有没有撞 RestartManager（看最新的 Setup 日志）
    $newest = Get-ChildItem $env:TEMP -Filter 'Setup Log*.txt' | Sort-Object LastWriteTime -Descending |
              Select-Object -First 1
    $newestText = Read-Log $newest.FullName
    Assert-That (-not ($newestText -match 'RestartManager found an application')) `
        "③ 本次安装**没有**出现 RestartManager 命中（$($newest.Name)）"
    Assert-That (-not ($newestText -match 'Rolling back changes')) "③ 本次安装没有回滚（$($newest.Name)）"

    Write-Host "[5/8] ④ 【兜底】1.0.39→新版这一次跑的是**旧交接进程**：新安装器必须能自己清掉它…" -ForegroundColor Cyan
    # 用户装的是 1.0.39，它的 update_install 还是老的 python 交接进程 —— 也就是说
    # "1.0.39 → 新版"这次就地更新，跑安装器的仍是那个住在安装目录里的 python。
    # 所以新安装器加了 PrepareToInstall：按"可执行文件路径在安装目录下"清掉残留进程。
    # 这里复现同一拓扑（安装目录里的 python 当安装器的父进程），跑**新版**安装器：
    # 期望"清理 → RestartManager 无事可做 → 安装成功"。
    $innerLog2 = Join-Path $logDir 'rescue-inner.log'
    $fakeOld2 = Join-Path $root 'fake_old_waiter2.py'
    [IO.File]::WriteAllText($fakeOld2, @"
import subprocess, time
print("old-style waiter (1.0.39) parenting the NEW installer", flush=True)
rc = subprocess.run([r"$setup", "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                     r"/DIR=$appDir", r"/LOG=$innerLog2"]).returncode
print("inner installer rc=%d" % rc, flush=True)
time.sleep(1)
"@, (New-Object System.Text.UTF8Encoding($false)))
    Get-Process -Name 'python' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$appDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    $rescuePy = Start-Process -FilePath (Join-Path $appDir 'runtime\python.exe') -PassThru `
        -ArgumentList $fakeOld2 -WindowStyle Hidden
    Start-Sleep -Seconds 2
    # 这里只记录、**不**断言"还活着"：新版安装器会在 PrepareToInstall 里就把它清掉 ——
    # 存活与否正是被测行为本身，拿它当断言会自相矛盾（② 步已经踩过一次）。
    Write-Host "      （旧式交接进程 PID $($rescuePy.Id) 两秒后是否还活着：$(-not $rescuePy.HasExited)）"
    # 安装器会把它杀掉 ⇒ 这里等安装器（子进程）自己跑完：轮询日志出现结论行为止
    $deadline = (Get-Date).AddSeconds(240)
    do {
        Start-Sleep -Seconds 3
        $t = Read-Log $innerLog2
    } while ((Get-Date) -lt $deadline -and
             -not ($t -match 'Rolling back changes|Installation process succeeded|Log closed'))
    $t = Read-Log $innerLog2
    if ($rescuePy -and -not $rescuePy.HasExited) { Stop-Process -Id $rescuePy.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "      （本次结局：$(if ($t -match 'Rolling back changes') { '回滚' } elseif ($t -match 'succeeded') { '装成功' } else { '未知' })）"
    Assert-That (-not ($t -match 'RestartManager found an application')) `
        '④ 新安装器的 PrepareToInstall 已把残留 python 清掉（RestartManager 无事可做）'
    Assert-That ($t -match 'Installation process succeeded') '④ 安装成功（1.0.39 → 新版 这条路径也能就地更新）'
    Assert-That (-not ($t -match 'Rolling back changes')) '④ 没有回滚'

    Write-Host "[6/8] ⑤ 清理：结束模拟进程…" -ForegroundColor Cyan
    if ($holderPy -and -not $holderPy.HasExited) { Stop-Process -Id $holderPy.Id -Force -ErrorAction SilentlyContinue }
    Get-Process -Name 'python' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$appDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
} finally {
    Write-Host "[7/8] 回收沙盒（卸载 + 删注册项/快捷方式/目录）…" -ForegroundColor Cyan
    $unins = Join-Path $appDir 'unins001.exe'
    if (Test-Path $unins) {
        try { Start-Process -FilePath $unins -Wait -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') | Out-Null } catch { }
    }
    # ⚠ 必须连**内层**安装器进程一起收：Inno 的 setup.exe 会把真正的安装器解到
    # %TEMP%\is-XXXX.tmp\setup.tmp 再跑，只杀外层的 `sandbox-setup` 会留下
    # `sandbox-setup.tmp` —— 它握着 install1.log，下一次跑 ① 时安装器写不了日志
    # 直接 exit 1（实测踩过：连着一轮测试"莫名其妙"在①失败，根因就是这个残留）。
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like 'sandbox-setup*' } |
        ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    if (Test-Path $uninsKey) { Remove-Item -Path $uninsKey -Recurse -Force -ErrorAction SilentlyContinue }
    if (Get-RunValue $sandboxName) { Remove-ItemProperty -Path $runKey -Name $sandboxName -ErrorAction SilentlyContinue }
    if (Test-Path $deskLink) { Remove-Item $deskLink -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
    Remove-Item $appDir -Recurse -Force -ErrorAction SilentlyContinue
    Assert-That (-not (Get-RunValue $sandboxName)) '清理：沙盒 Run 值不残留'
    Assert-That (-not (Test-Path $deskLink)) '清理：沙盒桌面快捷方式不残留'
    Assert-That (-not (Test-Path $uninsKey)) '清理：沙盒卸载注册项不残留'
}

if ($failed.Count -gt 0) {
    Write-Host "`n$($failed.Count) 项失败：$($failed -join ' / ')"
    Write-Host "（沙盒与日志保留在 $root 供排查）"
    exit 1
}
Write-Host "`n全部通过（就地更新：复现 RestartManager 中止 + 修复后成功）"
Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
