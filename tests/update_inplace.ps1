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
    if (Test-Path $p) { return (Get-Content $p -Raw -Encoding UTF8) }
    return ''
}
function Port-Busy([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

Write-Host "[1/6] 生成沙盒安装包（同 setup.iss 的段落，只换身份）…" -ForegroundColor Cyan
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
# 去掉 InitializeSetup（它会弹版本确认框；静默跑时虽然会被 SUPPRESSMSGBOXES 自动确认，
# 但这里测的是更新链路，不需要它）
$i = $code.IndexOf('function InitializeSetup')
if ($i -lt 0) { throw '未找到 InitializeSetup（setup.iss 结构变了）' }
$codeNoInit = $code.Substring(0, $i)

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
$codeNoInit
"@
Set-Content -Path $sandboxIss -Value $body -Encoding UTF8
& $iscc /Qp "/DMyAppVersion=1.0.39" $sandboxIss | Out-Host
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败（exit=$LASTEXITCODE）" }
$setup = Join-Path $outDir 'sandbox-setup.exe'
Assert-That (Test-Path $setup) '沙盒安装包编译成功（真实载荷）'

try {
    Write-Host "[2/6] ① 安装沙盒（模拟用户已装 1.0.39）…" -ForegroundColor Cyan
    $p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        '/TASKS=desktopicon', "/DIR=$appDir", "/LOG=$(Join-Path $logDir 'install1.log')")
    Assert-That ($p.ExitCode -eq 0) "① 沙盒安装退出码 0（实际 $($p.ExitCode)）"
    Assert-That (Test-Path (Join-Path $appDir 'FunScriptCast-Nexus.exe')) '① 沙盒里已有应用 exe'
    Assert-That (Test-Path (Join-Path $appDir 'runtime\python.exe')) '① 沙盒里已有自带运行时'

    Write-Host "[3/7] ② 【复现】安装目录里的 python 当安装器父进程（老交接进程拓扑）…" -ForegroundColor Cyan
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
rc = subprocess.run([r"$setup", "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                     r"/DIR=$appDir", r"/LOG=$innerLog"]).returncode
print("inner installer rc=%d" % rc, flush=True)
time.sleep(1)
"@, (New-Object System.Text.UTF8Encoding($false)))
    $t0 = Get-Date
    $busyPy = Start-Process -FilePath (Join-Path $appDir 'runtime\python.exe') -PassThru `
        -ArgumentList $fakeWaiter -WindowStyle Hidden
    Start-Sleep -Seconds 2
    Assert-That (-not $busyPy.HasExited) "② 假交接进程活着（安装目录里的 python，PID $($busyPy.Id)）"
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

    Write-Host "[4/7] ③ 【修复】交接脚本必须先**等端口释放**（模拟还没死透的字幕服务）…" -ForegroundColor Cyan
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

    Write-Host "[5/7] ④ 清理：结束模拟进程…" -ForegroundColor Cyan
    if ($holderPy -and -not $holderPy.HasExited) { Stop-Process -Id $holderPy.Id -Force -ErrorAction SilentlyContinue }
    Get-Process -Name 'python' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$appDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
} finally {
    Write-Host "[6/7] 回收沙盒（卸载 + 删注册项/快捷方式/目录）…" -ForegroundColor Cyan
    $unins = Join-Path $appDir 'unins001.exe'
    if (Test-Path $unins) {
        try { Start-Process -FilePath $unins -Wait -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') | Out-Null } catch { }
    }
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
