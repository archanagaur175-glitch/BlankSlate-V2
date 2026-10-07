# stop_prod.ps1 - Stop the BlankSlate v2 production processes (daemon + ollama).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\stop_prod.ps1

$ErrorActionPreference = "SilentlyContinue"

$targets = @(
    "blankslate-daemon",
    "ollama"
)

foreach ($name in $targets) {
    $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.Id -Force -ErrorAction Stop
            Write-Host "[stop_prod] Stopped: $($p.ProcessName) (PID $($p.Id))"
        } catch {
            Write-Host "[stop_prod] Could not stop: $($p.ProcessName) (PID $($p.Id)) - $($_.Exception.Message)"
        }
    }
}
Write-Host "[stop_prod] Done."
