# clean_procs.ps1 - Kill any stale BlankSlate / Ollama / daemon processes
# Usage: pwsh -ExecutionPolicy Bypass -File scripts\clean_procs.ps1

$ErrorActionPreference = "SilentlyContinue"

$targets = @(
    "ollama",
    "blankslate*",
    "daemon*",
    "vite",
    "cargo",
    "node"
)

$found = @()

foreach ($pattern in $targets) {
    $procs = Get-Process -Name $pattern -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        $found += $p
    }
}

if ($found.Count -eq 0) {
    Write-Host "[clean_procs] No stale processes found."
} else {
    foreach ($p in $found) {
        try {
            Stop-Process -Id $p.Id -Force -ErrorAction Stop
            Write-Host "[clean_procs] Killed: $($p.ProcessName) (PID $($p.Id))"
        } catch {
            Write-Host "[clean_procs] Could not kill: $($p.ProcessName) (PID $($p.Id)) - $($_.Exception.Message)"
        }
    }
    Start-Sleep -Milliseconds 500
    Write-Host "[clean_procs] Done. Killed $($found.Count) process(es)."
}
