# 测量 audiocpp_cli 的峰值显存占用（对比我们 PyTorch 管线的 3.4GB）
param(
    [string]$Model = 'E:\audiocpp-portable\models\Qwen3-ASR-0.6B',
    [string]$Backend = 'cuda',
    [string]$Audio = 'E:\audiocpp-portable\tmp\clip60.wav'
)

$ErrorActionPreference = 'Stop'
$p = 'E:\audiocpp-portable'
$before = [int]$((Get-Counter '\GPU Process Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples |
    Where-Object { $_.InstanceName -ne 'total' } | Measure-Object -Property CookedValue -Sum).Sum / 1MB
Write-Host ("基线显存（全系统）: {0:N0} MB" -f $before)

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "$p\$Backend\audiocpp_cli.exe"
$psi.Arguments = "--task asr --family qwen3_asr --model `"$Model`" --backend $Backend --audio `"$Audio`" --language ja --text-out `"$p\tmp\vram_out.txt`""
$psi.WorkingDirectory = $p
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$proc = [System.Diagnostics.Process]::Start($psi)

$peak = 0
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while (-not $proc.HasExited -and $sw.Elapsed.TotalSeconds -lt 300) {
    Start-Sleep -Milliseconds 200
    try {
        $cur = [int]$((Get-Counter '\GPU Process Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples |
            Where-Object { $_.InstanceName -ne 'total' } | Measure-Object -Property CookedValue -Sum).Sum / 1MB
        if ($cur -gt $peak) { $peak = $cur }
    } catch {}
}
$proc.WaitForExit()
$sw.Stop()

Write-Host ("峰值显存（全系统）: {0:N0} MB" -f $peak)
Write-Host ("增量              : {0:N0} MB" -f ($peak - $before))
Write-Host ("耗时              : {0:N1} s" -f $sw.Elapsed.TotalSeconds)
Write-Host ("退出码            : {0}" -f $proc.ExitCode)
if (Test-Path "$p\tmp\vram_out.txt") {
    Write-Host "识别结果:"
    Get-Content "$p\tmp\vram_out.txt" -Encoding UTF8 | ForEach-Object { "  $_" }
}
