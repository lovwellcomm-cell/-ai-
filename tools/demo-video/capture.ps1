<#
  启动本地应用 + FFmpeg 抓屏 + Playwright 操作，一次跑完录制。

  用法（在仓库根目录）：
    powershell -ExecutionPolicy Bypass -File tools/demo-video/capture.ps1
    powershell -File tools/demo-video/capture.ps1 -NoRecord   # 只跑操作，不录屏（调试用）

  产物：
    demo/demo-raw.mp4                原始录制（约 60s，含真实等待）
    tools/demo-video/timeline.json   每个动作的真实时刻
#>
param(
    [string]$Out = "demo/demo-raw.mkv",
    [int]$Port = 8501,
    [switch]$NoRecord,
    [switch]$KeepServer
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$ffmpeg = "C:\Users\lovwe\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg.Essentials_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe"
$W = 1440
$H = 900
# 只抓页面内容区：跳过 Chrome 标签栏与地址栏（约 100px 高）
$CAP_Y = 100

Set-Location $root

# ---------- 1. 起服务 ----------
Write-Host "[1/4] 启动 Streamlit…" -ForegroundColor Cyan
$env:PUBLIC_DEMO = "1"
$env:SESSION_LIMIT = "20"          # 演示需要多次提问，默认 5 次不够
$env:DAILY_LIMIT = "200"
$env:LLM_TIMEOUT = "90"

$server = Start-Process -FilePath "python" `
    -ArgumentList "-m","streamlit","run","app.py","--server.port",$Port,"--server.headless","true","--browser.gatherUsageStats","false" `
    -PassThru -WindowStyle Hidden

# 等端口就绪
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}
if (-not $ready) {
    if (-not $KeepServer) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
    throw "Streamlit 未能在 30 秒内启动"
}
Write-Host "      就绪 http://localhost:$Port" -ForegroundColor Green

# 预热一次，确保向量模型已加载（避免录制中卡在模型加载）
Write-Host "      预热模型…" -ForegroundColor DarkGray
try { Invoke-WebRequest -Uri "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 10 | Out-Null } catch { }

# ---------- 2. 起 FFmpeg ----------
# 用 MKV 而不是 MP4：MKV 是流式容器，强制结束时不会丢索引（MP4 会写不出 moov atom 而整段损坏）。
$ff = $null
if (-not $NoRecord) {
    Write-Host "[2/4] 启动 FFmpeg 抓屏…" -ForegroundColor Cyan
    $ffArgs = @(
        "-f","gdigrab",
        "-framerate","30",
        "-offset_x","0","-offset_y","$CAP_Y",
        "-video_size","${W}x${H}",
        "-draw_mouse","1",
        "-i","desktop",
        "-c:v","libx264","-preset","ultrafast","-crf","18",
        "-pix_fmt","yuv420p",
        "-y", $Out
    )
    $ff = Start-Process -FilePath $ffmpeg -ArgumentList $ffArgs -PassThru -WindowStyle Hidden
    # 记录 ffmpeg 启动的墙钟时刻，post.py 用它算出开头要裁掉多少秒
    $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    Set-Content -Path "tools/demo-video/ffmpeg-start.json" -Value "{`"startedAt`": $stamp}" -Encoding utf8
    Start-Sleep -Seconds 2   # 给 ffmpeg 建立抓屏的时间
}

# ---------- 3. Playwright 操作 ----------
Write-Host "[3/4] Playwright 执行演示脚本…" -ForegroundColor Cyan
$env:PLAYWRIGHT_MODULE = "C:/Users/lovwe/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright"
node "tools/demo-video/record.mjs" `
    --scenario "tools/demo-video/scenario.json" `
    --timeline "tools/demo-video/timeline.json"

$code = $LASTEXITCODE
if ($code -ne 0) { Write-Host "      ⚠ 录制脚本返回 $code" -ForegroundColor Yellow }

# ---------- 4. 收尾 ----------
if ($ff) {
    Write-Host "[4/4] 停止 FFmpeg…" -ForegroundColor Cyan
    Start-Sleep -Milliseconds 800
    # MKV 流式写入，直接结束也不会损坏
    try {
        Stop-Process -Id $ff.Id -Force -ErrorAction SilentlyContinue
    } catch { }
    Start-Sleep -Seconds 2
}

if (-not $KeepServer) {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}

if ((Test-Path $Out) -and -not $NoRecord) {
    $mb = [math]::Round((Get-Item $Out).Length / 1MB, 2)
    Write-Host "`n完成：$Out ($mb MB)" -ForegroundColor Green
    $probe = & $ffmpeg -hide_banner -i $Out 2>&1 | Out-String
    ($probe -split "`n" | Where-Object { $_ -match "Duration|Stream #0:0" }) | ForEach-Object { "  " + $_.Trim() }
} else {
    Write-Host "`n只执行了操作，未录屏。" -ForegroundColor Yellow
}
