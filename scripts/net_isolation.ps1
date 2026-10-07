# net_isolation.ps1 - BlankSlate v2 network isolation (Phase 5).
#
#   -Mode enable   (run as ADMINISTRATOR) FIRST sweeps any stale rules with the
#                   BlankSlate-Isolation-* name (logged), THEN creates fresh
#                   outbound BLOCK rules for the daemon interpreters, and turns
#                   on Windows Firewall dropped-connection logging.
#   -Mode disable  (run as ADMINISTRATOR) removes every BlankSlate-Isolation-* rule.
#   -Mode status    show current isolation rules (no admin required).
#
# While enabled: python/the frozen daemon cannot open outbound internet
# connections. Loopback (127.0.0.1 -> Ollama/IPC/HUD) is unaffected by design.
# NOTE: `pip install` will fail until you disable again.
#
# Usage (elevated):
#   powershell -ExecutionPolicy Bypass -File scripts\net_isolation.ps1 -Mode enable

param(
    [ValidateSet("enable", "disable", "status")]
    [string]$Mode = "status"
)

$ErrorActionPreference = "Stop"
$RulePrefix = "BlankSlate-Isolation"
$RepoRoot   = "C:\Users\archa\Documents\BlankSlate_V2"
$LogFile    = Join-Path $RepoRoot "daemon\logs\net_isolation.log"
$DaemonExe  = Join-Path $RepoRoot "daemon\dist\blankslate-daemon\blankslate-daemon.exe"
$FwLog      = "$env:windir\System32\LogFiles\Firewall\pfirewall.log"

function Write-Log([string]$Message) {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Mode.ToUpper(), $Message
    Write-Host $line
    $dir = Split-Path -Parent $LogFile
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Test-IsAdministrator {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-IsolationRules {
    @(Get-NetFirewallRule -DisplayName "$RulePrefix-*" -ErrorAction SilentlyContinue)
}

function Remove-StaleRules([string]$Context) {
    $rules = Get-IsolationRules
    if ($rules.Count -eq 0) {
        Write-Log "no '$RulePrefix-*' rules found ($Context)."
        return
    }
    foreach ($rule in $rules) {
        $program = (Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue).Program
        Write-Log ("STALE RULE REMOVED: Name='{0}' DisplayName='{1}' Enabled={2} Direction={3} Action={4} Program='{5}' ({6})" -f `
            $rule.Name, $rule.DisplayName, $rule.Enabled, $rule.Direction, $rule.Action, $program, $Context)
        Remove-NetFirewallRule -Name $rule.Name
    }
    Write-Log ("swept {0} stale rule(s) ({1})." -f $rules.Count, $Context)
}

switch ($Mode) {
    "status" {
        Write-Log "status requested."
        $rules = Get-IsolationRules
        if ($rules.Count -eq 0) {
            Write-Host "NO isolation rules active."
        }
        foreach ($rule in $rules) {
            $program = (Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule).Program
            Write-Host ("  {0}  Enabled={1} {2} {3} Program={4}" -f `
                $rule.DisplayName, $rule.Enabled, $rule.Direction, $rule.Action, $program)
        }
        Write-Host "Firewall dropped-connection log: $FwLog"
        exit 0
    }

    "disable" {
        if (-not (Test-IsAdministrator)) {
            Write-Log "DISABLE refused: not elevated."
            throw "Run elevated: right-click PowerShell -> 'Run as administrator', then scripts\net_isolation.ps1 -Mode disable"
        }
        Write-Log "disable requested - removing all '$RulePrefix-*' rules."
        Remove-StaleRules "disable"
        Write-Log "isolation DISABLED. Windows Firewall dropped-connection logging left ON (harmless; revert with: netsh advfirewall set allprofiles logging droppedconnections no)."
        exit 0
    }

    "enable" {
        if (-not (Test-IsAdministrator)) {
            Write-Log "ENABLE refused: not elevated."
            throw "Run elevated: right-click PowerShell -> 'Run as administrator', then scripts\net_isolation.ps1 -Mode enable"
        }
        Write-Log "=== enable requested ==="

        # 1) ALWAYS sweep stale leftovers first (idempotent re-runs, logged).
        Remove-StaleRules "pre-enable sweep"

        # 2) Resolve the programs to isolate.
        $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
        if (-not $pythonExe) { throw "python not found on PATH." }
        if ($pythonExe -like "*WindowsApps*") {
            throw "python resolves to the WindowsApps store stub ($pythonExe). Fix PATH first."
        }
        $targets = @($pythonExe)
        if (Test-Path $DaemonExe) {
            $targets += $DaemonExe
        } else {
            Write-Log "frozen daemon not found at $DaemonExe (its rule skipped; dev python still covered)."
        }

        foreach ($target in $targets) {
            $name = "$RulePrefix-$(Split-Path $target -Leaf)"
            New-NetFirewallRule -Name $name -DisplayName $name `
                -Direction Outbound -Action Block -Program $target `
                -Enabled True -Profile Any `
                -Description "BlankSlate Phase 5 network isolation. Remove: scripts\net_isolation.ps1 -Mode disable" | Out-Null
            Write-Log "RULE CREATED: Name='$name' Direction=Outbound Action=Block Program='$target'"
        }

        # 3) Dropped-connection logging (system firewall log).
        netsh advfirewall set allprofiles logging droppedconnections yes | Out-Null
        Write-Log "dropped-connection logging ENABLED (all profiles). Log: $FwLog"

        $count = (Get-IsolationRules).Count
        Write-Log "ENABLED: $count rule(s) active. Loopback (127.0.0.1) unaffected by design; outbound internet from python/the daemon is BLOCKED (pip included) until -Mode disable."
        exit 0
    }
}
