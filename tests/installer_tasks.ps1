# F30 行为验证：附加任务"取消勾选"必须真的回删已装项
#
#   用法：pwsh -File tests\installer_tasks.ps1        （纯 PowerShell，与 .venv 无关）
#
# 为什么要有这个脚本：Inno 的 Tasks 门控语义是"未勾选 = 跳过安装"，不是"删除已装的"。
# 评审 F30 指出的正是这一点：升级安装时取消勾选「开机自启 / 桌面快捷方式」，上一版
# 写下的 Run 值与桌面图标会原样留着（用户的选择被静默无视）。修法是在 [Code] 的
# CurStepChanged(ssPostInstall) 里按向导勾选状态主动回删。
# 这类逻辑用肉眼 review 不可靠（WizardIsTaskSelected 在静默模式下的语义、.lnk 名字
# 里的 .lnk 后缀、RegValueExists 的行为都要真跑一遍才知道），所以这里**真跑安装器**：
#
#   ① /TASKS="autostart,desktopicon"  → 应创建 Run 值 + 桌面快捷方式
#   ② /TASKS=            （全不勾）    → 应把上面两项回删（F30 的修复点）
#   ③ /TASKS=            （再跑一次）  → 幂等，不报错
#
# 全程用**测试身份**：独立 AppId / 独立名称 / 独立安装目录 / 独立桌面链接名 /
# 独立卸载注册项，并在结束时全部回收。**不碰**用户的真实安装（D:\FunScriptCast-Nexus）
# 与真实的 `FunScriptCast-Nexus` Run 值名。
#
# 测的是 installer\setup.iss 里**原文的** [Tasks]/[Icons]/[Registry]/[Code] 段
# （脚本从真文件里抽段，不复制一份，避免日后脚本与安装脚本各说各话）。
# 抽段后会剔除 InitializeSetup（它会弹版本确认框，静默安装时会挂住等人点），
# 只留 CurStepChanged —— F30 的逻辑全在那里。

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repo = Split-Path -Parent $PSScriptRoot
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
if (-not (Test-Path $iscc)) { throw "找不到 Inno Setup 编译器：$iscc" }

$testName = 'FunScriptCast-Nexus-F30Test'
$testGuid = [guid]::NewGuid().ToString().ToUpper()
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$uninsKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{$testGuid}_is1"
$desktop = [Environment]::GetFolderPath('Desktop')
$deskLink = Join-Path $desktop "$testName.lnk"

$tmp = Join-Path $env:TEMP ("f30-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$stub = Join-Path $tmp 'stub'
$outDir = Join-Path $tmp 'out'
$appDir = Join-Path $tmp 'app'
New-Item -ItemType Directory -Force -Path $stub, $outDir, $appDir | Out-Null

$failed = New-Object System.Collections.Generic.List[string]
function Assert-That([bool]$cond, [string]$what) {
    if ($cond) { Write-Host "  OK   $what" }
    else { Write-Host "  FAIL $what"; $script:failed.Add($what) }
}
function Get-Section([string]$text, [string]$name) {
    $m = [regex]::Match($text, "(?ms)^\[$name\][^\r\n]*\r?\n(.*?)(?=^\[|\Z)")
    if (-not $m.Success) { throw "installer\setup.iss 里找不到 [$name] 段（结构变了？）" }
    return "[$name]`r`n" + $m.Groups[1].Value.TrimEnd() + "`r`n"
}
function Get-RunValue([string]$name) {
    # ⚠ 必须走 PSObject.Properties：Set-StrictMode -Version Latest 下
    # `(Get-ItemProperty ...).$name` 在属性不存在时**抛异常**（而不是返回 $null），
    # 而"属性不存在"正是本脚本要断言的成功状态（第一次跑就踩到了）。
    $item = Get-ItemProperty -Path $runKey -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    $prop = $item.PSObject.Properties[$name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

try {
    # ── 1. 从真实 setup.iss 抽段（含 F30 逻辑原文）─────────────────────────────
    $issText = Get-Content -Raw -Encoding UTF8 (Join-Path $repo 'installer\setup.iss')
    $tasks = Get-Section $issText 'Tasks'
    $icons = Get-Section $issText 'Icons'
    $registry = Get-Section $issText 'Registry'
    $code = Get-Section $issText 'Code'

    $i = $code.IndexOf('function InitializeSetup')
    if ($i -lt 0) { throw '未找到 InitializeSetup（setup.iss 结构变了？抽段逻辑要跟着改）' }
    # F30 的回删入口。**缺失不抛异常，只记一条 FAIL 并继续**：这样对着旧代码跑时
    # 既能看到"结构里没有它"，又能看到"取消勾选后 Run 值/快捷方式确实还在"
    # （行为级证据）—— 抛异常会把后面更有说服力的断言全掩盖掉。
    $j = $code.IndexOf('procedure CurStepChanged', $i)
    $hasHook = $j -ge 0
    Assert-That $hasHook 'setup.iss 的 [Code] 有 CurStepChanged（F30 的回删入口）'
    if ($hasHook) {
        $codeUnderTest = $code.Substring(0, $i) + $code.Substring($j)
    } else {
        $codeUnderTest = $code.Substring(0, $i)      # 砍掉 InitializeSetup，留其余原文
    }
    Assert-That ($codeUnderTest -match 'ssPostInstall') '回删逻辑挂在 ssPostInstall（装完再删）'
    Assert-That ($codeUnderTest -match 'autostart') '按 autostart 勾选状态回删自启值'
    Assert-That ($codeUnderTest -match 'desktopicon') '按 desktopicon 勾选状态回删快捷方式'

    # ── 2. 生成测试用 .iss（同段原文 + 测试身份 + 1KB 假载荷）──────────────────
    Set-Content -Path (Join-Path $stub 'f30stub.exe') -Value 'stub payload' -Encoding ascii
    $testIss = Join-Path $tmp 'f30test.iss'
    $body = @"
#define MyAppName "$testName"
#define MyAppExeName "f30stub.exe"
#define MyAppPublisher "F30Test"
#define MyAppVersion "1.0.0"

[Setup]
AppId={{$testGuid}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName=$appDir
PrivilegesRequired=lowest
OutputDir=$outDir
OutputBaseFilename=f30setup
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableProgramGroupPage=yes
SetupLogging=yes
UsePreviousAppDir=yes

[Files]
Source: "$stub\*"; DestDir: "{app}"; Flags: ignoreversion

$tasks
$icons
$registry
$codeUnderTest
"@
    Set-Content -Path $testIss -Value $body -Encoding UTF8

    & $iscc /Qp "/DMyAppVersion=1.0.0" $testIss | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败（exit=$LASTEXITCODE）" }
    $setup = Join-Path $outDir 'f30setup.exe'
    Assert-That (Test-Path $setup) '测试安装器编译成功'

    # ── 3. ① 勾选两项安装 ────────────────────────────────────────────────────
    $p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        '/TASKS=autostart,desktopicon', "/DIR=$appDir", "/LOG=$(Join-Path $tmp 'run1.log')")
    Assert-That ($p.ExitCode -eq 0) "① 勾选安装退出码 0（实际 $($p.ExitCode)）"
    $rv1 = Get-RunValue $testName
    Assert-That ([bool]$rv1) "① 勾选后 Run 值已写入（$rv1）"
    Assert-That (Test-Path $deskLink) '① 勾选后桌面快捷方式已创建'

    # ── 4. ② 全不勾再装一次：两项都必须被回删 ────────────────────────────────
    $p2 = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        '/TASKS=', "/DIR=$appDir", "/LOG=$(Join-Path $tmp 'run2.log')")
    Assert-That ($p2.ExitCode -eq 0) "② 取消勾选安装退出码 0（实际 $($p2.ExitCode)）"
    Assert-That (-not (Get-RunValue $testName)) '② 取消勾选后 Run 值被回删（F30 修复点）'
    Assert-That (-not (Test-Path $deskLink)) '② 取消勾选后桌面快捷方式被回删（F30 修复点）'

    # ── 5. ③ 再来一次：没有可删的东西也不能报错（幂等）───────────────────────
    $p3 = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        '/TASKS=', "/DIR=$appDir", "/LOG=$(Join-Path $tmp 'run3.log')")
    Assert-That ($p3.ExitCode -eq 0) "③ 重复取消勾选安装退出码 0（实际 $($p3.ExitCode)）"
}
finally {
    # ── 回收：卸载 + 删注册项 + 删临时目录（绝不把测试痕迹留给用户）──────────
    $unins = Join-Path $appDir 'unins001.exe'
    if (Test-Path $unins) {
        try {
            Start-Process -FilePath $unins -Wait -ArgumentList @(
                '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') | Out-Null
        } catch { Write-Host "  （卸载器执行失败，继续手工清理：$_）" }
    }
    if (Test-Path $uninsKey) { Remove-Item -Path $uninsKey -Recurse -Force -ErrorAction SilentlyContinue }
    if (Get-RunValue $testName) {
        Remove-ItemProperty -Path $runKey -Name $testName -ErrorAction SilentlyContinue
        Write-Host "  （已强制清掉残留 Run 值 $testName）"
    }
    if (Test-Path $deskLink) {
        Remove-Item -Path $deskLink -Force -ErrorAction SilentlyContinue
        Write-Host "  （已强制清掉残留桌面快捷方式）"
    }
    if (Test-Path $tmp) { Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    Assert-That (-not (Get-RunValue $testName)) '清理：测试 Run 值不残留'
    Assert-That (-not (Test-Path $deskLink)) '清理：测试桌面快捷方式不残留'
    Assert-That (-not (Test-Path $uninsKey)) '清理：测试卸载注册项不残留'
}

if ($failed.Count -gt 0) {
    Write-Host "`n$($failed.Count) 项失败：$($failed -join ' / ')"
    exit 1
}
Write-Host "`n全部通过（F30：取消勾选会回删自启值与桌面快捷方式）"
