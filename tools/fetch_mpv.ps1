# 下载 libmpv-2.dll（桌面播放器运行时，121MB）到 vendor\mpv
#
#   powershell -ExecutionPolicy Bypass -File tools\fetch_mpv.ps1
#   powershell -ExecutionPolicy Bypass -File tools\fetch_mpv.ps1 -Force   # 强制重下
#
# R108 原型定案：桌面播放器 = 外挂 mpv 窗口 + python-mpv 控制。dll 必须就位，
# 否则「媒体库」点播放会报"找不到 libmpv-2.dll"。
param(
    [switch]$Force,
    [string]$Proxy
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dst = Join-Path $root 'vendor\mpv'
$marker = Join-Path $dst '.fetched'
$dll = Join-Path $dst 'libmpv-2.dll'

if ((Test-Path $dll) -and (Test-Path $marker) -and -not $Force) {
    Write-Host "已完成（$dll 就位）。-Force 可重下。"
    return
}

$tmp = Join-Path $env:TEMP ('mpv-fetch-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
try {
    New-Item -ItemType Directory -Force -Path $tmp, $dst | Out-Null

    # 官方 7zr.exe（单文件，无需安装）：mpv-dev 是 BCJ2 压缩的 7z，py7zr 解不动
    Write-Host "[1/3] 下载 7zr.exe 与 mpv-dev（libmpv 运行库）" -ForegroundColor Cyan
    $z7 = Join-Path $tmp '7zr.exe'
    $arc = Join-Path $tmp 'mpv-dev.7z'
    $curlArgs = @('-L', '-sS', '--retry', '3', '--retry-all-errors', '-o')
    if ($Proxy) { $curlArgs += @('--proxy', $Proxy) }
    & curl @curlArgs "$z7" 'https://www.7-zip.org/a/7zr.exe'
    if ($LASTEXITCODE -ne 0) { throw "下载 7zr.exe 失败（curl exit $LASTEXITCODE）" }
    $rel = Invoke-RestMethod 'https://api.github.com/repos/zhongfly/mpv-winbuild/releases/latest'
    $asset = $rel.assets | Where-Object { $_.name -like 'mpv-dev-x86_64-*.7z' } | Select-Object -First 1
    if (-not $asset) { throw "mpv-winbuild 最新 release 里没找到 mpv-dev-x86_64-*.7z" }
    & curl @curlArgs "$arc" $asset.browser_download_url
    if ($LASTEXITCODE -ne 0) { throw "下载 mpv-dev 失败（curl exit $LASTEXITCODE）" }
    Write-Host ("      OK {0:N1} MB" -f ((Get-Item $arc).Length / 1MB))

    Write-Host "[2/3] 解压提取 libmpv-2.dll" -ForegroundColor Cyan
    & $z7 x "$arc" -o"$tmp\dll" -y | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "解压失败（7zr exit $LASTEXITCODE）" }
    $src = Get-ChildItem "$tmp\dll" -Recurse -Filter 'libmpv-2.dll' | Select-Object -First 1
    if (-not $src) { throw "解压产物里没有 libmpv-2.dll" }
    Copy-Item $src.FullName $dll -Force

    Write-Host "[3/3] 自检" -ForegroundColor Cyan
    if ((Get-Item $dll).Length -lt 50MB) { throw "libmpv-2.dll 大小异常" }
    Set-Content -Path $marker -Value 'mpv-1' -Encoding ASCII
    Write-Host "[完成] vendor\mpv\libmpv-2.dll 就绪（桌面播放器运行时）" -ForegroundColor Green
} finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
