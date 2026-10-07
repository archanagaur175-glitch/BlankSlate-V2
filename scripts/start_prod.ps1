# start_prod.ps1 - Boot BlankSlate v2 for production.
#
#  1. Starts the official Ollama server (RTX 2050 CUDA backend) if not already serving.
#  2. Starts the frozen blankslate-daemon if not already running.
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\start_prod.ps1

$ErrorActionPreference = "Stop"

$OllamaExe   = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
$OllamaHost  = "http://127.0.0.1:11434"
$DaemonExe   = "C:\Users\archa\Documents\BlankSlate_V2\daemon\dist\blankslate-daemon\blankslate-daemon.exe"
$IpcPort     = 8765

function Test-PortOpen([string]$hostName, [int]$port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $result = $c.BeginConnect($hostName, $port, $null, $null)
        $ok = $result.AsyncWaitHandle.WaitOne(800)
        if ($ok) { $c.Close(); return $true }
        $c.Close(); return $false
    } catch { return $false }
}

function Start-Ollama {
    Write-Host "[start_prod] Starting Ollama ($OllamaExe) ..."
    if (-not (Test-Path $OllamaExe)) {
        throw "Ollama not found at $OllamaExe . Install it from https://ollama.com/download/OllamaSetup.exe"
    }
    $logDir = "C:\Users\archa\Documents\BlankSlate_V2\daemon\logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $logOut = Join-Path $logDir "ollama.out.log"
    $logErr = Join-Path $logDir "ollama.err.log"
    $env:CUDA_VISIBLE_DEVICES = "0"
    $env:OLLAMA_MODELS = "C:\Users\archa\.ollama\models"
    $env:OLLAMA_FLASH_ATTENTION = "1"
    Start-Process -FilePath $OllamaExe -ArgumentList "serve" -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr
    # Wait for the API
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 1000
        try {
            $r = Invoke-WebRequest -Uri "$OllamaHost/api/tags" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { Write-Host "[start_prod] Ollama API ready."; return }
        } catch {}
    }
    throw "Ollama did not become ready on $OllamaHost within 60s."
}

function Start-Daemon {
    Write-Host "[start_prod] Starting daemon ($DaemonExe) ..."
    if (-not (Test-Path $DaemonExe)) {
        throw "Frozen daemon not found at $DaemonExe . Rebuild it first."
    }
    # Daemon env vars (BS_MIC_DEVICE, BS_TTS_ENABLED, etc.)
    if ($env:BS_MIC_DEVICE) { Write-Host "[start_prod] Using mic device: $($env:BS_MIC_DEVICE)" }
    Start-Process -FilePath $DaemonExe -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 1000
        if (Test-PortOpen "127.0.0.1" $IpcPort) { Write-Host "[start_prod] Daemon IPC ready on :$IpcPort."; return }
    }
    throw "Daemon did not open IPC port $IpcPort within 30s."
}

# 1. Ollama
if (Test-PortOpen "127.0.0.1" 11434) {
    Write-Host "[start_prod] Ollama already serving on $OllamaHost."
} else {
    Start-Ollama
}

# 2. Daemon
if (Test-PortOpen "127.0.0.1" $IpcPort) {
    Write-Host "[start_prod] Daemon already running on :$IpcPort."
} else {
    Start-Daemon
}

Write-Host ""
Write-Host "[start_prod] BlankSlate v2 is up:"
Write-Host "   Ollama  -> $OllamaHost"
Write-Host "   Daemon  -> ws://127.0.0.1:$IpcPort"
Write-Host "[start_prod] Done."
