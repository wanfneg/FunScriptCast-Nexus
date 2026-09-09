# 用 CSV 整体替换术语表（原有条目全部丢弃）
#
#   powershell -ExecutionPolicy Bypass -File tools\import_glossary_csv.ps1 -Ja ja.csv -En en.csv
#   powershell -ExecutionPolicy Bypass -File tools\import_glossary_csv.ps1 -Ja ja.csv          # 只换日语表
#
# CSV 格式：两列「原文,译文」，首行表头可选（term,translation / 原文,译文…），
#           UTF-8（可带 BOM）或 GBK 均可；第三列起忽略。

param(
    [string]$Ja = '',
    [string]$En = '',
    [int]$Port = 8790
)

$ErrorActionPreference = 'Stop'

function Import-One($lang, $path) {
    if (-not $path) { return }
    if (-not (Test-Path $path)) { throw "找不到 CSV：$path" }
    $body = @{ path = (Resolve-Path $path).Path; lang = $lang; mode = 'replace' } | ConvertTo-Json
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/glossary/import" -Method POST `
        -Body $body -ContentType 'application/json' -TimeoutSec 60
    if ($r.ok) {
        Write-Host ("[{0}] 已替换：{1} 条（跳过 {2}）" -f $lang, $r.count, $r.skipped) -ForegroundColor Green
    } else {
        Write-Host ("[{0}] 失败：{1}" -f $lang, $r.error) -ForegroundColor Red
    }
}

if (-not $Ja -and -not $En) { throw '至少要给 -Ja 或 -En 之一' }

# 确认宿主在跑
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/state" -TimeoutSec 5 | Out-Null
} catch {
    throw "宿主未运行（127.0.0.1:$Port）。先启动 FunScriptCast-Nexus 或 host_server.py"
}

Import-One 'ja' $Ja
Import-One 'en' $En
