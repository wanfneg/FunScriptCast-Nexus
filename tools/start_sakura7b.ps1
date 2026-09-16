# 启动 Sakura-7B 专用的隔离 Ollama 实例（模型库放 F 盘）
#
# 为什么需要这个脚本
#   1. 用户的默认 Ollama 模型库在 C 盘（约 8 GB，C 盘长期 90%+ 满），而 Sakura-7B
#      的 GGUF 有 3.96 GB —— 创建模型时会把 blob 复制进模型库，C 盘放不下。
#   2. 用独立端口 + 独立模型库，可以**不打扰**用户现有的 Ollama 实例与其模型。
#
# 用法
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_sakura7b.ps1
#
# 启动后：http://127.0.0.1:11435 提供 Ollama API，模型名 sakura-7b
# 配套配置：vendor\subtitle\config.json → translate.ollama.base_url = http://127.0.0.1:11435
#
# 注意：本脚本启动的是**独立进程**，重启电脑后需要重新运行；若要开机自启，
#       可把本脚本放进「任务计划程序」或启动文件夹。

$ErrorActionPreference = 'Stop'

$Port      = 11435
$ModelsDir = 'F:\ollama\models'
$SrcGguf   = 'E:\Development\_ref\models-sakura\sakura-7b-qwen2.5-v1.0-iq4xs.gguf'
$ModelName = 'sakura-7b'
$Ollama    = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'

function Wait-Port([int]$p, [int]$timeoutSec) {
    # 轮询探测（短间隔 + 上限），不用固定 sleep
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try { $null = Invoke-RestMethod "http://127.0.0.1:$p/api/tags" -TimeoutSec 2; return $true } catch { }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

Write-Host "[1/4] 环境校验" -ForegroundColor Cyan
if (-not (Test-Path $Ollama)) { throw "找不到 ollama.exe：$Ollama" }
Write-Host "      ollama : $Ollama"
Write-Host "      模型库 : $ModelsDir"
Write-Host "      端口   : $Port"

Write-Host "[2/4] 若已在运行则直接复用" -ForegroundColor Cyan
if (Wait-Port $Port 2) {
    Write-Host "      :$Port 已在运行 ✓"
} else {
    New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
    $env:OLLAMA_MODELS = $ModelsDir
    $env:OLLAMA_HOST   = "127.0.0.1:${Port}"
    Write-Host "      启动隔离实例…"
    Start-Process -FilePath $Ollama -ArgumentList 'serve' -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $env:TEMP 'ollama-f.log') `
        -RedirectStandardError  (Join-Path $env:TEMP 'ollama-f.err')
    if (-not (Wait-Port $Port 90)) {
        $err = Join-Path $env:TEMP 'ollama-f.err'
        if (Test-Path $err) { Get-Content $err -Tail 15 | ForEach-Object { Write-Host "      $_" } }
        throw ":${Port} 在 90 秒内未就绪"
    }
    Write-Host "      :$Port 就绪 ✓"
}

Write-Host "[3/4] 确认 $ModelName 已注册" -ForegroundColor Cyan
$tags = (Invoke-RestMethod "http://127.0.0.1:${Port}/api/tags" -TimeoutSec 10).models
$has = $tags | Where-Object { $_.name -like "$ModelName*" }
if ($has) {
    Write-Host ("      {0}  {1:N2} GB ✓" -f $has.name, ($has.size / 1GB))
} else {
    if (-not (Test-Path $SrcGguf)) { throw "缺 GGUF：$SrcGguf" }
    Write-Host "      未注册，用 GGUF 创建（首次会复制约 4 GB，需要 $ModelsDir 有空间）…"
    $mf = Join-Path $env:TEMP 'Modelfile.sakura7b'
    # 官方参数：temperature 0.1 / top_p 0.3（见 Sakura 官方仓库）
    @("FROM $($SrcGguf -replace '\\','/')", 'PARAMETER temperature 0.1', 'PARAMETER top_p 0.3') |
        Set-Content -Path $mf -Encoding UTF8
    $env:OLLAMA_MODELS = $ModelsDir
    $env:OLLAMA_HOST   = "127.0.0.1:${Port}"
    & $Ollama create $ModelName -f $mf
    if ($LASTEXITCODE -ne 0) { throw "ollama create 失败（退出码 $LASTEXITCODE）—— 多半是 $ModelsDir 空间不足" }
    $tags = (Invoke-RestMethod "http://127.0.0.1:${Port}/api/tags" -TimeoutSec 10).models
    $tags | ForEach-Object { Write-Host ("      {0}  {1:N2} GB" -f $_.name, ($_.size / 1GB)) }
}

Write-Host "[4/4] 完成" -ForegroundColor Green
Write-Host "      字幕服务配置应为：translate.ollama.base_url = http://127.0.0.1:${Port}"
Write-Host "                        translate.ollama.model    = $ModelName"
