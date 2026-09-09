# 下载 VibeVoice-ASR-HF 模型分片（.NET HttpClient，带续传与重试）
# 用法： powershell -ExecutionPolicy Bypass -File tests\diag\download_vibevoice.ps1

$ErrorActionPreference = 'Stop'
$dir = 'E:\Development\FunScriptCast-Nexus\models\vibevoice'
$base = 'https://huggingface.co/microsoft/VibeVoice-ASR-HF/resolve/main'
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$files = @(
    'config.json', 'generation_config.json', 'processor_config.json',
    'tokenizer.json', 'tokenizer_config.json', 'model.safetensors.index.json',
    'chat_template.jinja'
) + (1..8 | ForEach-Object { 'model-{0:00000}-of-00008.safetensors' -f $_ })

Add-Type -AssemblyName System.Net.Http

foreach ($f in $files) {
    $out = Join-Path $dir $f
    $url = "$base/$f"
    $attempt = 0
    while ($attempt -lt 6) {
        $attempt++
        $have = if (Test-Path $out) { (Get-Item $out).Length } else { 0 }
        try {
            $handler = New-Object System.Net.Http.HttpClientHandler
            $handler.Timeout = [TimeSpan]::FromMinutes(30)
            $client = New-Object System.Net.Http.HttpClient($handler)
            $req = New-Object System.Net.Http.HttpRequestMessage('Get', $url)
            if ($have -gt 0) { $req.Headers.Range = New-Object System.Net.Http.Headers.RangeHeaderValue($have, $null) }
            $resp = $client.SendAsync($req, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).Result
            $total = if ($resp.Content.Headers.ContentLength) { $have + $resp.Content.Headers.ContentLength } else { 0 }
            $stream = $resp.Content.ReadAsStreamAsync().Result
            $mode = if ($have -gt 0 -and $resp.StatusCode -eq 'PartialContent') { 'ab' } else { 'wb' }
            if ($mode -eq 'wb') { $have = 0 }
            $fs = [System.IO.File]::Open($out, [System.IO.FileMode]::Append)
            $buf = New-Object byte[] (4MB)
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            while ($true) {
                $n = $stream.Read($buf, 0, $buf.Length)
                if ($n -le 0) { break }
                $fs.Write($buf, 0, $n)
                $have += $n
                if ($sw.Elapsed.TotalSeconds -gt 15) {
                    $mb = $have / 1MB
                    $rate = $mb / $sw.Elapsed.TotalSeconds
                    Write-Host ("  {0}: {1:N1} MB / {2:N1} MB  ({3:N1} MB/s)" -f $f, $mb, ($total/1MB), $rate)
                    $sw.Restart()
                }
            }
            $fs.Close(); $stream.Close(); $client.Dispose()
            $done = (Get-Item $out).Length
            if ($total -gt 0 -and $done -lt $total) { throw "不完整：$done / $total" }
            Write-Host ("OK  {0}  {1:N1} MB" -f $f, ($done/1MB)) -ForegroundColor Green
            break
        } catch {
            Write-Host ("RETRY {0} (attempt {1}): {2}" -f $f, $attempt, $_.Exception.Message) -ForegroundColor Yellow
            Start-Sleep -Seconds 5
        }
    }
}
Write-Host '全部完成' -ForegroundColor Cyan
Get-ChildItem $dir | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB)}} | Format-Table -AutoSize
