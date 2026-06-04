# claude-mcp-bridge restart script — kill old, start new
# Run: powershell.exe -ExecutionPolicy Bypass -File _restart_all.ps1
# After it finishes: open http://127.0.0.1:8765/ and send a new message to spawn fresh Claude.

$ErrorActionPreference = "Continue"
$root = "D:\aiproject\claude-mcp-bridge"
Set-Location $root

Write-Host "[1/6] Snapshotting current targets..." -ForegroundColor Cyan
$daemonPids = (Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*claude_daemon.py*"
}).ProcessId
$webPids = (Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*web_server.py*"
}).ProcessId
$bridgePids = (Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*mcp_bridge.py*"
}).ProcessId
Write-Host "  daemon PIDs:  $($daemonPids -join ', ')"
Write-Host "  web    PIDs:  $($webPids -join ', ')"
Write-Host "  bridge PIDs:  $($bridgePids -join ', ')"

Write-Host "[2/6] Killing daemon (this also takes down child claude + mcp_bridge)..." -ForegroundColor Cyan
foreach ($p in $daemonPids) {
    try {
        Stop-Process -Id $p -Force -ErrorAction Stop
        Write-Host "  killed daemon PID $p"
    } catch {
        Write-Host "  failed to kill ${p}: $_" -ForegroundColor Yellow
    }
}
Start-Sleep -Seconds 3

Write-Host "[3/6] Killing web_server..." -ForegroundColor Cyan
foreach ($p in $webPids) {
    try {
        Stop-Process -Id $p -Force -ErrorAction Stop
        Write-Host "  killed web_server PID $p"
    } catch {
        Write-Host "  failed to kill ${p}: $_" -ForegroundColor Yellow
    }
}

Write-Host "[4/6] Sweeping leftover mcp_bridge / orphaned claude TUIs..." -ForegroundColor Cyan
Start-Sleep -Seconds 2
$leftover = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*mcp_bridge.py*" -or
    ($_.Name -eq "claude.exe" -and $_.CommandLine -like "*dangerously-skip-permissions*")
}
foreach ($p in $leftover) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Write-Host "  killed leftover $($p.Name) PID $($p.ProcessId)"
    } catch {
        Write-Host "  failed to kill PID $($p.ProcessId) - $_" -ForegroundColor Yellow
    }
}

Write-Host "[5/6] Waiting for port 8765 to free up..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    $still = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if (-not $still) { break }
    Start-Sleep -Milliseconds 500
}
$still = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($still) {
    Write-Host "  WARNING: port 8765 still held by PID $($still.OwningProcess) — aborting" -ForegroundColor Red
    exit 1
}
Write-Host "  port 8765 is free"

Write-Host "[6/6] Starting new web_server + daemon (detached)..." -ForegroundColor Cyan
# Use Start-Process with -WindowStyle Hidden so they survive this script exiting.
# Logs go to web_server.log (web_server) and chat_sessions/default/daemon.log (daemon).
Start-Process -FilePath "python" -ArgumentList "web_server.py" -WorkingDirectory $root -WindowStyle Hidden
Write-Host "  web_server.py launched"

Start-Sleep -Seconds 3  # give web_server a head start

Start-Process -FilePath "python" -ArgumentList "claude_daemon.py", "default" -WorkingDirectory $root -WindowStyle Hidden
Write-Host "  claude_daemon.py default launched"

Start-Sleep -Seconds 2
Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Open http://127.0.0.1:8765/ in your browser."
Write-Host "Send a test message to spawn a fresh Claude conversation."
Write-Host ""
Write-Host "Verify:"
Write-Host "  Get-Content chat_sessions\default\daemon.log -Tail 20"
Write-Host "  Get-Content chat_sessions\default\meta.json"
Write-Host "  Get-Content web_server.log -Tail 20"
