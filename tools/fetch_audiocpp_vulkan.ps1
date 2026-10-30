# 下载 audio.cpp v0.7.4 Vulkan 运行时到 vendor\audiocpp\vulkan（跨 NVIDIA/AMD/Intel）
#   powershell -ExecutionPolicy Bypass -File tools\fetch_audiocpp_vulkan.ps1 [-Proxy http://127.0.0.1:7897] [-Force]
param(
    [string]$Proxy = '',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dst = Join-Path $root 'vendor\audiocpp\vulkan'
$marker = Join-Path $dst '.fetch-ok'
if ((Test-Path $marker) -and -not $Force) { Write-Host "已完成（$((Get-Content $marker -Raw))）"; return }
$url = 'https://github.com/0xShug0/audio.cpp/releases/download/v0.7.4/audio-v0.7.4-bin-windows-x64-vulkan.zip'
$tmp = Join-Path $env:TEMP ("audiocpp-vulkan-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
try {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp 'audio-vulkan.zip'
    $args = @('-L', '-sS', '--retry', '3', '--retry-all-errors', '-o', $zip)
    if ($Proxy) { $args += @('--proxy', $Proxy) }
    $args += $url
    & curl @args
    if ($LASTEXITCODE -ne 0) { throw "下载失败（curl exit $LASTEXITCODE）" }
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $dst -Force
    foreach ($f in 'audiocpp_server.exe', 'audiocpp_cli.exe') {
        if (-not (Test-Path (Join-Path $dst $f))) { throw "解压后缺 $f" }
    }
    Set-Content -Path $marker -Value 'vulkan-0.7.4' -Encoding ASCII
    Write-Host "[完成] vendor\audiocpp\vulkan" -ForegroundColor Green
} finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
