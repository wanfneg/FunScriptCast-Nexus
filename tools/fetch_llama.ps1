# 下载并解压 llama.cpp（Windows x64 + CUDA 12.4）到 vendor\llama
#
# 为什么用脚本而不是直接入库：
#   llama-server.exe + CUDA 运行时合计约 1.1 GB，入 git 会让仓库历史膨胀
#   （models\ 同理，已在 .gitignore 里）。构建机/新机器执行本脚本即可复现。
#
#   powershell -ExecutionPolicy Bypass -File tools\fetch_llama.ps1
#   powershell -ExecutionPolicy Bypass -File tools\fetch_llama.ps1 -Tag b11000 -Proxy http://127.0.0.1:7897
param(
    [string]$Tag   = 'b11000',   # llama.cpp 发布 tag（CUDA 12.4 版资产从 b109xx 起都有）
    [string]$Proxy = '',         # 形如 http://127.0.0.1:7897；直连 GitHub 正常时留空
    [switch]$Force               # 目标已存在时也重新下载解压
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dst  = Join-Path $root 'vendor\llama'
$tmp  = Join-Path $env:TEMP "llama-fetch-$Tag"
$files = @(
    "llama-$Tag-bin-win-cuda-12.4-x64.zip",
    "cudart-llama-bin-win-cuda-12.4-x64.zip"
)
$exe = Join-Path $dst 'llama-server.exe'
# 两个 zip 各一个代表文件。旧判据只查 llama-server.exe：主包解开、cudart 包下载/
# 解压失败或中断时，下次运行照样报"已存在"并 exit 0，机器上永远缺 CUDA 运行时 DLL。
$needFiles = @('llama-server.exe', 'cudart64_12.dll')
# 完成标记：解压 + 自检全部通过才写，内容为本次 $Tag（跨 tag 升级即自然失效）。
$marker = Join-Path $dst '.fetch-ok'

Write-Host "[1/6] 环境校验" -ForegroundColor Cyan
$curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
if (-not $curl) { throw "找不到 curl.exe（Windows 10+ 自带，检查 PATH）" }
$complete = $false
if (Test-Path $marker) {
    $markerTag = ''
    try { $markerTag = (Get-Content $marker -Raw -ErrorAction Stop).Trim() } catch { }
    if ($markerTag -eq $Tag) {
        $complete = $true
        foreach ($n in $needFiles) {
            if (-not (Test-Path (Join-Path $dst $n))) {
                Write-Host "      完成标记写着 $Tag，但缺 $n —— 判定为不完整，重新下载解压" -ForegroundColor Yellow
                $complete = $false
            }
        }
    } else {
        Write-Host "      完成标记是 '$markerTag'（本次 $Tag），按未完成处理" -ForegroundColor Yellow
    }
}
if ($complete -and -not $Force) {
    Write-Host "      vendor\llama 已完整（标记 $Tag；$($needFiles -join ' / ') 齐全）；要重下请加 -Force" -ForegroundColor Yellow
    exit 0
}
if ((Test-Path $exe) -and -not $Force -and -not $complete) {
    Write-Host "      vendor\llama 无有效完成标记（上次多半只解开了主包就中断），继续走完整下载解压" -ForegroundColor Yellow
}
Write-Host "      curl : $curl"
Write-Host "      tag  : $Tag"
Write-Host "      目标 : $dst"

# 全部下载+解压步骤包进 try，%TEMP% 清理放 finally：失败路径此前会留下
# %TEMP%\llama-fetch-*（含两个 zip，约 1.1 GB），只在成功路径的清尾才删。
try {
    Write-Host "[2/6] 读取发布资产的精确字节数（用于下载后比对）" -ForegroundColor Cyan
    $apiArgs = @{
        Uri = "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/$Tag"
        Headers = @{ 'User-Agent' = 'nexus-fetch'; 'Accept' = 'application/vnd.github+json' }
        TimeoutSec = 60
    }
    if ($Proxy) { $apiArgs['Proxy'] = $Proxy }
    try { $rel = Invoke-RestMethod @apiArgs }
    catch { throw "取 release 信息失败（tag=$Tag）：$($_.Exception.Message)" }
    $size = @{}
    foreach ($a in $rel.assets) { $size[$a.name] = $a.size }
    foreach ($n in $files) {
        if (-not $size.ContainsKey($n)) { throw "该 tag 没有资产 $n（换 -Tag）" }
        Write-Host ("      {0,-46} {1,12:N0} 字节" -f $n, $size[$n])
    }

    Write-Host "[3/6] 下载" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $dst, $tmp | Out-Null
    foreach ($n in $files) {
        $out = Join-Path $tmp $n
        if (Test-Path $out) { Remove-Item $out -Force }
        $url = "https://github.com/ggml-org/llama.cpp/releases/download/$Tag/$n"
        $cmd = @($curl, '-L', '-sS', '--retry', '3', '--retry-all-errors', '-o', $out)
        if ($Proxy) { $cmd += @('--proxy', $Proxy) }
        $cmd += $url
        & $cmd[0] $cmd[1..($cmd.Count - 1)]
        if ($LASTEXITCODE -ne 0) { throw "下载失败 $n（curl exit $LASTEXITCODE）" }
        $got = (Get-Item $out).Length
        $exp = $size[$n]
        if ([Math]::Abs($got - $exp) -gt ($exp * 0.01)) { throw "字节数不符 $n：$got vs $exp" }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z = [IO.Compression.ZipFile]::OpenRead($out)
        $cnt = $z.Entries.Count
        $z.Dispose()
        if ($cnt -lt 1) { throw "zip 打不开或无条目：$n" }
        Write-Host ("      OK {0,-46} {1,8:N1} MB  条目 {2}" -f $n, ($got / 1MB), $cnt)
    }

    Write-Host "[4/6] 解压到 vendor\llama" -ForegroundColor Cyan
    if ((Test-Path $dst) -and $Force) {
        # 跨 tag 升级必须先清空：Expand-Archive 只覆盖同名文件，旧版 dll/exe 会
        # 与新版混存（"升级了但行为诡异"的温床）。
        Remove-Item $dst -Recurse -Force
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
    }
    foreach ($n in $files) {
        Expand-Archive -LiteralPath (Join-Path $tmp $n) -DestinationPath $dst -Force
        Write-Host "      $n"
    }

    Write-Host "[5/6] 自检" -ForegroundColor Cyan
    if (-not (Test-Path $exe)) { throw "解压后仍缺 llama-server.exe：$exe" }
    foreach ($n in $needFiles) {
        if (-not (Test-Path (Join-Path $dst $n))) {
            throw "解压后仍缺 $n：$dst（多半只解开了一个 zip，重跑本脚本）"
        }
    }
    $total = (Get-ChildItem $dst -Recurse -File | Measure-Object Length -Sum).Sum
    Write-Host ("      vendor\llama 共 {0:N1} MB（{1} 齐全）" -f ($total / 1MB), ($needFiles -join ' / '))
    # 自检通过才写标记：这是"两个 zip 都解开且内容齐"的唯一凭据，下次运行靠它早退。
    Set-Content -Path $marker -Value $Tag -Encoding ASCII
    Write-Host "      已写完成标记 $marker → $Tag"
} finally {
    # 失败路径同样要清（%TEMP% 里是两个 zip，约 1.1 GB）
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host "[6/6] 完成" -ForegroundColor Green
Write-Host "      翻译后端由字幕服务按需拉起（config.json → translate.backend = local）"
Write-Host "      手动验证：$exe -m ..\..\models\Sakura-7B-Qwen2.5-v1.0\*.gguf --port 8082"
