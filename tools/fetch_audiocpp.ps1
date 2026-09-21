# 下载 audiocpp（audio.cpp，ASR 运行时，CPU 版）到 vendor\audiocpp
#
#   powershell -ExecutionPolicy Bypass -File tools\fetch_audiocpp.ps1
#   powershell -ExecutionPolicy Bypass -File tools\fetch_audiocpp.ps1 -Force   # 强制重下
#
# 背景（R69 打通全新安装）：audio.cpp 运行时自 v1.0.18 的 llama 同款思路起不再进
# git，构建机用本脚本拉取；安装包组装（build_exe.ps1）会把 vendor\audiocpp 原样
# 打进安装包（区别于 vendor\llama：audiocpp CPU 版只有 ~13MB，直接内置，用户零下载）。
# zip 内容 = audio.cpp 官方 CPU 发布物（cpu\ + assets\ + LICENSE），来自公开发行仓库。
param(
    [switch]$Force,
    [string]$Proxy
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dst = Join-Path $root 'vendor\audiocpp'
$marker = Join-Path $dst '.fetched'
$Url = 'https://github.com/wanfneg/FunScriptCast-Nexus/releases/latest/download/audiocpp-runtime-windows-cpu.zip'

if ((Test-Path $marker) -and -not $Force) {
    Write-Host "已完成（标记 $marker 内容 $((Get-Content $marker -Raw))）。-Force 可重下。"
    return
}

$tmp = Join-Path $env:TEMP ('audiocpp-fetch-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
try {
    Write-Host "[1/3] 下载 $Url" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $out = Join-Path $tmp 'audiocpp-runtime-windows-cpu.zip'
    $curlArgs = @('-L', '-sS', '--retry', '3', '--retry-all-errors', '-o', $out)
    if ($Proxy) { $curlArgs += @('--proxy', $Proxy) }
    $curlArgs += $Url
    & curl @curlArgs
    if ($LASTEXITCODE -ne 0) { throw "下载失败（curl exit $LASTEXITCODE）" }
    Write-Host ("      OK {0:N1} MB" -f ((Get-Item $out).Length / 1MB))

    Write-Host "[2/3] 解压到 vendor\audiocpp" -ForegroundColor Cyan
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Expand-Archive -LiteralPath $out -DestinationPath $dst -Force

    Write-Host "[3/6] 自检" -ForegroundColor Cyan
    foreach ($f in 'cpu\audiocpp_server.exe', 'cpu\audiocpp_cli.exe', 'assets\framework\models\silero_vad', 'LICENSE') {
        if (-not (Test-Path (Join-Path $dst $f))) { throw "解压后缺 $f（安装包内容不符）" }
    }
    # 自检通过才写标记
    Set-Content -Path $marker -Value 'cpu-1' -Encoding ASCII
    Write-Host "[完成] vendor\audiocpp 就绪（字幕服务经 config asr.audiocpp 按需拉起）" -ForegroundColor Green
} finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
