# 构建字幕服务的自包含轻量运行时 → dist-app\runtime\
#
#   powershell -ExecutionPolicy Bypass -File build\make_runtime.ps1
#
# 产出 dist-app\runtime\（约 40-60 MB）：
#   python.exe + python310.dll   官方 embeddable 发行版
#   Lib\site-packages\           fastapi / uvicorn / numpy（字幕服务 audiocpp
#                                模式所需的全部第三方依赖；torch 不需要，
#                                见 vendor/subtitle/asr_engine 的懒加载说明）
#
# 宿主查找解释器的顺序里 runtime\python.exe 排第一（host_server._subtitle_python），
# 命中即自包含，不再借用任何外部 .venv。
param(
    [string]$PythonVersion = "3.10.11",
    [string]$OutDir = ""
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root 'dist-app\runtime' }
$tmp = Join-Path $env:TEMP ("nexus-runtime-" + [guid]::NewGuid().ToString('N').Substring(0, 8))

if (Test-Path $OutDir) {
    Write-Host "runtime 已存在：$OutDir（如需重建请先删除）" -ForegroundColor Yellow
    exit 0
}
New-Item -ItemType Directory -Path $tmp, $OutDir -Force | Out-Null

# ---- 1. 下载官方 embeddable 发行版 ----
$zipUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$zip = Join-Path $tmp "python-embed.zip"
Write-Host "[1/3] 下载 $zipUrl" -ForegroundColor Cyan
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $OutDir -Force

# embeddable 默认禁用 site-packages：编辑 ._pth 启用，并指向 Lib\site-packages
$pth = Get-ChildItem $OutDir -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "embeddable 包里找不到 ._pth 文件" }
$pthText = Get-Content $pth.FullName -Raw
$pthText = $pthText -replace '#import site', 'import site'
if ($pthText -notmatch 'Lib\\site-packages') {
    $pthText = $pthText + "Lib\site-packages`r`n"
}
Set-Content -Path $pth.FullName -Value $pthText -Encoding ASCII

# ---- 2/3. 安装字幕服务（audiocpp 模式）的运行依赖 ----
# 只装轻量依赖（fastapi/uvicorn/numpy）；torch/qwen_asr 在 audiocpp 模式不需要
# （asr_engine 已懒加载）。runtime 本体不需要 pip，直接 --target 装进 site-packages。
# NO_PROXY=*：元凶是 **Windows 系统代理**（注册表 Internet Settings，Clash 类
# 工具会写入 127.0.0.1:7897），pip 的 urllib 通过 getproxies() 读注册表，与
# pip.ini 无关（--isolated 也拦不住）。代理软件没开时 pip 全部请求 TLS 失败，
# 报 "check_hostname requires server_hostname"。NO_PROXY=* 让全部主机直连。
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { throw "找不到 venv Python：$venvPy" }
Write-Host "[2/3] 安装 fastapi / uvicorn / numpy …" -ForegroundColor Cyan
$sitePkgs = Join-Path $OutDir 'Lib\site-packages'
New-Item -ItemType Directory -Path $sitePkgs -Force | Out-Null
$env:NO_PROXY = '*'
$env:no_proxy = '*'
& $venvPy -m pip install --target $sitePkgs "fastapi" "uvicorn" "numpy"
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }

# ---- 4. 冒烟验证：runtime python 能导入全部依赖 ----
Write-Host "[3/3] 冒烟验证…" -ForegroundColor Cyan
$smoke = & (Join-Path $OutDir 'python.exe') -c "import fastapi, uvicorn, numpy, sys; print('runtime ok', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0 -or $smoke -notmatch 'runtime ok') { throw "runtime 冒烟验证失败" }
Write-Host $smoke

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
$size = (Get-ChildItem $OutDir -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("完成：{0}（{1:N1} MB）" -f $OutDir, $size) -ForegroundColor Green
