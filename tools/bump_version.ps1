# Nexus 版本号自增（构建前调用）：version.json 的 patch +1，versionCode +1。
#
#   powershell -ExecutionPolicy Bypass -File tools\bump_version.ps1
#   powershell -ExecutionPolicy Bypass -File tools\bump_version.ps1 -Show   # 只看不改
#
# 规则：1.0.0 -> 1.0.1 -> … -> 1.0.99 -> 1.1.0（patch 到 99 进位 minor）

param([switch]$Show)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$versionFile = Join-Path $root 'version.json'
if (-not (Test-Path $versionFile)) { throw "找不到 $versionFile" }

$raw = [System.IO.File]::ReadAllText($versionFile, [System.Text.Encoding]::UTF8)
$data = $raw | ConvertFrom-Json

$parts = ($data.versionName -split '\.')
while ($parts.Count -lt 3) { $parts += '0' }
$major = [int]$parts[0]
$minor = [int]$parts[1]
$patch = [int]$parts[2] + 1
if ($patch -gt 99) { $patch = 0; $minor += 1 }
if ($minor -gt 99) { $minor = 0; $major += 1 }

$newName = "$major.$minor.$patch"
$newCode = [int]$data.versionCode + 1

if ($Show) {
    Write-Host "当前 $($data.versionName) (code $($data.versionCode)) -> 下次 $newName (code $newCode)" -ForegroundColor Cyan
    exit 0
}

$newJson = [ordered]@{}
foreach ($p in $data.PSObject.Properties) {
    if ($p.Name -eq 'versionName') { $newJson['versionName'] = $newName }
    elseif ($p.Name -eq 'versionCode') { $newJson['versionCode'] = $newCode }
    else { $newJson[$p.Name] = $p.Value }
}
$text = ($newJson | ConvertTo-Json -Depth 5) + "`n"
[System.IO.File]::WriteAllText($versionFile, $text, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "版本已更新：$($data.versionName) -> $newName (code $newCode)" -ForegroundColor Green
