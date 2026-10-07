# sandbox_user_setup.ps1 - BlankSlate v2 Phase 6a one-time admin setup for the
# sandbox's dedicated low-privilege account (design: docs\SANDBOX_CODE_EXECUTION_DESIGN.md).
#
#   -Mode enable   (run as ADMINISTRATOR) creates/resets the bs-sandbox local
#                   account (never an Administrator), grants the two logon
#                   rights, applies the ACL boundaries (bs-sandbox AND the
#                   AppContainer package SID - probe evidence: AC access checks
#                   ignore user-SID ACEs), and hands the password to the daemon
#                   as a DPAPI CurrentUser blob. Idempotent:
#                   re-runs reset the password and re-apply every ACE cleanly.
#   -Mode disable  (run as ADMINISTRATOR) removes our ACEs, logon rights,
#                   credential files, and the account.
#   -Mode status    show account/ACL/credential ground truth (no admin needed).
#
# Account contract (pinned by the 6a design doc - must match 6b code):
#   cred  daemon\data\sandbox_user.cred   DPAPI(CurrentUser), entropy
#         "BlankSlate.v2.sandbox.v1", plaintext = account password
#   meta  daemon\data\sandbox_user.json   {user, python, created}
#
# Usage (elevated):
#   powershell -ExecutionPolicy Bypass -File scripts\sandbox_user_setup.ps1 -Mode enable

param(
    [ValidateSet("enable", "disable", "status")]
    [string]$Mode = "status",
    [string]$UserName = "bs-sandbox"
)

$ErrorActionPreference = "Stop"
$RepoRoot     = "C:\Users\archa\Documents\BlankSlate_V2"
$LogFile      = Join-Path $RepoRoot "daemon\logs\sandbox_setup.log"
$CredFile     = Join-Path $RepoRoot "daemon\data\sandbox_user.cred"
$MetaFile     = Join-Path $RepoRoot "daemon\data\sandbox_user.json"
$ScratchRoot  = Join-Path $RepoRoot "daemon\data\sandbox"
$ProfileName   = "BlankSlateSandbox"
$ProfileRoot   = $env:USERPROFILE
$EntropyPlain = "BlankSlate.v2.sandbox.v1"
$RightNames   = @("SeInteractiveLogonRight", "SeBatchLogonRight")
$Secedit      = Join-Path $env:windir "System32\secedit.exe"

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

function Get-PythonExe {
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $pythonExe) { throw "python not found on PATH." }
    if ($pythonExe -like "*WindowsApps*") {
        throw "python resolves to the WindowsApps store stub ($pythonExe). Fix PATH first."
    }
    return $pythonExe
}

function Get-AppContainerPackageSid([string]$Name) {
    if (-not ("BlankSlate.SandboxNative" -as [type])) {
        Add-Type -Namespace BlankSlate -Name SandboxNative -MemberDefinition @"
[DllImport("userenv", CharSet = CharSet.Unicode, ExactSpelling = true)]
public static extern int DeriveAppContainerSidFromAppContainerName(string name, out IntPtr sid);
[DllImport("advapi32", CharSet = CharSet.Unicode, ExactSpelling = true)]
public static extern bool ConvertSidToStringSidW(IntPtr sid, out IntPtr str);
[DllImport("kernel32")]
public static extern IntPtr LocalFree(IntPtr mem);
"@
    }
    $sidPtr = [IntPtr]::Zero
    $hr = [BlankSlate.SandboxNative]::DeriveAppContainerSidFromAppContainerName($Name, [ref]$sidPtr)
    if ($hr -ne 0 -or $sidPtr -eq [IntPtr]::Zero) {
        throw "DeriveAppContainerSidFromAppContainerName('$Name') failed hr=0x$([int]$hr.ToString('X8'))"
    }
    $strPtr = [IntPtr]::Zero
    if (-not [BlankSlate.SandboxNative]::ConvertSidToStringSidW($sidPtr, [ref]$strPtr)) {
        throw "ConvertSidToStringSidW failed."
    }
    $sidStr = [Runtime.InteropServices.Marshal]::PtrToStringUni($strPtr)
    [BlankSlate.SandboxNative]::LocalFree($strPtr) | Out-Null
    return $sidStr
}

function Invoke-Icacls([string[]]$Iargs, [string]$What) {
    $out = & icacls.exe @Iargs 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed for $What (rc=$LASTEXITCODE): $($out.Trim())"
    }
    return $out.Trim()
}

function Clear-OurAces([string]$Path, [string]$PkgSid = "") {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    & icacls.exe $Path /remove:d $UserName /remove:g $UserName /Q 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "icacls remove for '$Path' returned rc=$LASTEXITCODE (no prior ACE - continuing)."
    }
    if ($PkgSid) {
        & icacls.exe $Path /remove:g "*$PkgSid" /Q 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Log "icacls pkg remove for '$Path' returned rc=$LASTEXITCODE (no prior ACE - continuing)."
        }
    }
}

function Set-LogonRights([bool]$Add) {
    $exportInf = Join-Path $env:TEMP "bs_sandbox_rights_export.inf"
    $finalInf  = Join-Path $env:TEMP "bs_sandbox_rights_final.inf"
    $db        = Join-Path $env:TEMP "bs_sandbox_rights.sdb"
    & $Secedit /export /cfg $exportInf /areas USER_RIGHTS /quiet 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $exportInf)) {
        throw "secedit /export failed (rc=$LASTEXITCODE) - refusing to configure rights from a stale file."
    }
    $sid = (Get-LocalUser -Name $UserName).SID.Value
    $result = @()
    $seen = @{}
    foreach ($line in (Get-Content -LiteralPath $exportInf)) {
        $m = [regex]::Match($line, "^(Se\w+)\s*=\s*(.*)$")
        if ($m.Success -and ($RightNames -contains $m.Groups[1].Value)) {
            $right = $m.Groups[1].Value
            $seen[$right] = $true
            $holders = @()
            foreach ($raw in ($m.Groups[2].Value -split "[,;]")) {
                $h = $raw.Trim()
                if (-not $h) { continue }
                $bare = $h.TrimStart("*")
                if ($bare -ne $sid -and $bare -ne "$env:COMPUTERNAME\$UserName" -and $bare -ne $UserName) {
                    $holders += $h
                }
            }
            if ($Add) { $holders += "*$sid" }
            $result += ("{0} = {1}" -f $right, ($holders -join ","))
        } else {
            $result += $line
        }
    }
    foreach ($right in $RightNames) {
        if (-not $seen[$right]) {
            if ($Add) { $result += ("{0} = *{1}" -f $right, $sid) }
        } elseif (-not $Add) {
            # line already emitted (holders minus our SID)
        }
    }
    $result | Set-Content -LiteralPath $finalInf -Encoding Unicode
    Remove-Item -LiteralPath $db -Force -ErrorAction SilentlyContinue
    $cfgOut = & $Secedit /configure /db $db /cfg $finalInf /areas USER_RIGHTS /quiet 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "secedit /configure failed (rc=$LASTEXITCODE): $($cfgOut.Trim())"
    }
    Remove-Item -LiteralPath $exportInf, $finalInf -Force -ErrorAction SilentlyContinue
    return $sid
}

function Show-Grants([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Host ("    {0}: ABSENT ({1})" -f $Label, $Path)
        return
    }
    $dump = (& icacls.exe $Path 2>&1 | Out-String).Trim()
    Write-Host ("    {0} ({1}):" -f $Label, $Path)
    foreach ($l in ($dump -split "`r?`n")) { Write-Host "      $l" }
}

function Assert-Ace([string]$Path, [string]$Trustee, [string]$Perm, [string]$What) {
    $lines = @(& icacls.exe $Path 2>&1 | ForEach-Object { "$_" })
    $hit = $lines | Where-Object {
        $_ -like "*$Trustee*" -and $_ -like "*($Perm)*" -and $_ -notlike "*DENY*"
    }
    if (-not $hit) {
        throw "ACL ASSERT FAILED ($What): allow $($Trustee)($Perm) not found on $Path"
    }
    Write-Log "ACL assert OK ($What): $($Trustee)($Perm) on $Path"
}

function Assert-AceDeny([string]$Path, [string]$Trustee, [string]$Perm, [string]$What) {
    $lines = @(& icacls.exe $Path 2>&1 | ForEach-Object { "$_" })
    $hit = $lines | Where-Object {
        $_ -like "*$Trustee*" -and $_ -like "*($Perm)*" -and $_ -like "*DENY*"
    }
    if (-not $hit) {
        throw "ACL ASSERT FAILED ($What): DENY $($Trustee)($Perm) not found on $Path"
    }
    Write-Log "ACL assert OK ($What): DENY $($Trustee)($Perm) on $Path"
}

function Test-Ace([string]$Path, [string]$Trustee, [string]$Perm, [bool]$Deny = $false) {
    $lines = @(& icacls.exe $Path 2>&1 | ForEach-Object { "$_" })
    $hit = $lines | Where-Object {
        $_ -like "*$Trustee*" -and $_ -like "*($Perm)*" -and
        $(if ($Deny) { $_ -like "*DENY*" } else { $_ -notlike "*DENY*" })
    }
    return [bool]$hit
}

function Get-SandboxChainDirs {
    return @(
        $ProfileRoot,
        (Join-Path $ProfileRoot "AppData"),
        (Join-Path $ProfileRoot "AppData\Local"),
        (Join-Path $ProfileRoot "AppData\Local\Programs"),
        (Join-Path $ProfileRoot "AppData\Local\Programs\Python"),
        (Join-Path $ProfileRoot "Documents"),
        $RepoRoot,
        (Join-Path $RepoRoot "daemon"),
        (Join-Path $RepoRoot "daemon\data")
    )
}

switch ($Mode) {
    "status" {
        Write-Log "status requested."
        $u = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
        if (-not $u) {
            Write-Host "ACCOUNT ABSENT - run elevated: -Mode enable"
        } else {
            $admin = @(Get-LocalGroupMember -Group "Administrators" -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match "[\\/]$([regex]::Escape($UserName))$" })
            Write-Host ("ACCOUNT: {0} Enabled={1} PasswordNeverExpires={2}" -f $u.Name, $u.Enabled, $u.PasswordNeverExpires)
            Write-Host ("ADMIN MEMBER: {0}  (must be False)" -f ($admin.Count -gt 0))
            $groups = @(Get-LocalGroup | Where-Object { (Get-LocalGroupMember $_ -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match "[\\/]$([regex]::Escape($UserName))$" }) } | ForEach-Object { $_.Name })
            Write-Host ("GROUPS: {0}" -f $(if ($groups.Count) { $groups -join ", " } else { "(none)" }))
        }
        foreach ($f in @($CredFile, $MetaFile)) {
            if (Test-Path -LiteralPath $f) {
                Write-Host ("CRED: {0}  {1}B" -f $f, (Get-Item -LiteralPath $f).Length)
            } else {
                Write-Host ("CRED: {0}  ABSENT" -f $f)
            }
        }
        Write-Host "ACLs:"
        $pkgSid = ""
        try { $pkgSid = Get-AppContainerPackageSid $ProfileName } catch { Write-Host "PKG SID: unavailable ($($_.Exception.Message))" }
        if ($pkgSid) { Write-Host "PACKAGE: '$ProfileName' -> $pkgSid" }
        Show-Grants $ScratchRoot "scratch (need: bs-sandbox M + pkg M)"
        Show-Grants $RepoRoot "repo   (need: bs-sandbox deny)"
        Show-Grants (Split-Path -Parent (Get-PythonExe)) "python (need: bs-sandbox RX + pkg RX)"
        Show-Grants $ProfileRoot "profile(root) (need: pkg traverse X)"
        exit 0
    }

    "disable" {
        if (-not (Test-IsAdministrator)) {
            Write-Log "DISABLE refused: not elevated."
            throw "Run elevated: right-click PowerShell -> 'Run as administrator', then scripts\sandbox_user_setup.ps1 -Mode disable"
        }
        Write-Log "disable requested - tearing down '$UserName'."
        $pkgSid = ""
        try { $pkgSid = Get-AppContainerPackageSid $ProfileName } catch { Write-Log "pkg SID derive failed: $($_.Exception.Message)" }
        if (Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue) {
            Set-LogonRights $false | Out-Null
            Write-Log "logon rights removed for '$UserName'."
        }
        Clear-OurAces $ScratchRoot $pkgSid
        foreach ($d in (Get-SandboxChainDirs)) { Clear-OurAces $d $pkgSid }
        try { Clear-OurAces (Split-Path -Parent (Get-PythonExe)) $pkgSid } catch { Write-Log "python ACE clear skipped: $($_.Exception.Message)" }
        Write-Log "ACEs removed on scratch/chain/python (bs-sandbox + package SID)."
        foreach ($f in @($CredFile, $MetaFile)) {
            if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force; Write-Log "REMOVED: $f" }
        }
        if (Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue) {
            Remove-LocalUser -Name $UserName
            Write-Log "ACCOUNT DELETED: '$UserName' (files it created in scratch remain, owned by the deleted SID - normal cleanup handles them)."
        }
        Write-Log "sandbox setup DISABLED."
        exit 0
    }

    "enable" {
        if (-not (Test-IsAdministrator)) {
            Write-Log "ENABLE refused: not elevated."
            throw "Run elevated: right-click PowerShell -> 'Run as administrator', then scripts\sandbox_user_setup.ps1 -Mode enable"
        }
        Write-Log "=== enable requested ==="

        $pythonExe = Get-PythonExe
        $pythonRoot = Split-Path -Parent $pythonExe
        Write-Log "python: $pythonExe"

        $pool = @((48..57) + (65..90) + (97..122) + @(33, 35, 36, 37, 38, 42, 43, 45, 61, 63, 64, 94, 95) |
            ForEach-Object { [char]$_ })
        $pwdPlain = -join ((1..24) | ForEach-Object { Get-Random -InputObject $pool })
        $secure = ConvertTo-SecureString $pwdPlain -AsPlainText -Force

        $existing = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
        if ($existing) {
            Set-LocalUser -Name $UserName -Password $secure
            Set-LocalUser -Name $UserName -PasswordNeverExpires $true
            Write-Log "EXISTING account reset (password rotated, PasswordNeverExpires=True)."
        } else {
            New-LocalUser -Name $UserName -Password $secure -PasswordNeverExpires -AccountNeverExpires `
                -Description "BlankSlate v2 sandbox - unprivileged" | Out-Null
            Write-Log "ACCOUNT CREATED: '$UserName'."
        }
        $u = Get-LocalUser -Name $UserName
        if (-not $u.Enabled) { Enable-LocalUser -Name $UserName; Write-Log "account enabled." }

        if (-not (Get-LocalGroupMember -Group "Users" -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match "[\\/]$([regex]::Escape($UserName))$" })) {
            Add-LocalGroupMember -Group "Users" -Member $UserName
            Write-Log "added '$UserName' to Users."
        }
        $adminHits = @(Get-LocalGroupMember -Group "Administrators" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "[\\/]$([regex]::Escape($UserName))$" })
        if ($adminHits.Count -gt 0) {
            Remove-LocalGroupMember -Group "Administrators" -Member $UserName -ErrorAction SilentlyContinue
            Write-Log "REMOVED unexpected Administrators membership."
        }
        $adminNow = @(Get-LocalGroupMember -Group "Administrators" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "[\\/]$([regex]::Escape($UserName))$" }).Count -gt 0
        Write-Log "ASSERT not-admin: $($adminNow -eq $false) (members of Administrators: none matching '$UserName')"
        if ($adminNow) { throw "account is an Administrator - aborting." }

        $sid = Set-LogonRights $true
        Write-Log "logon rights granted (SeInteractive + SeBatch) to SID $sid (prior holders preserved)."

        if (-not (Test-Path -LiteralPath $ScratchRoot)) {
            New-Item -ItemType Directory -Path $ScratchRoot -Force | Out-Null
        }
        $pkgSid = Get-AppContainerPackageSid $ProfileName
        Write-Log "package profile '$ProfileName' -> $pkgSid"

        Clear-OurAces $ScratchRoot $pkgSid
        $r1 = Invoke-Icacls @($ScratchRoot, "/grant", "${UserName}:(OI)(CI)M", "/T", "/C", "/Q") "scratch grant"
        $r1b = Invoke-Icacls @($ScratchRoot, "/grant", "*${pkgSid}:(OI)(CI)M", "/T", "/C", "/Q") "scratch pkg grant"
        Write-Log "ACL: GRANT M -> $ScratchRoot (bs-sandbox + package SID, /T)"

        if (Test-Ace $RepoRoot $UserName "RX" $true) {
            Write-Log "repo DENY already present - skip re-apply (each apply walks the whole repo tree)"
        } else {
            Clear-OurAces $RepoRoot $pkgSid
            $r2 = Invoke-Icacls @($RepoRoot, "/deny", "${UserName}:(OI)(CI)RX", "/C", "/Q") "repo deny"
            Write-Log "ACL: DENY RX -> $RepoRoot (recursive by inheritance; scratch keeps its explicit grant)"
        }

        if ((Test-Ace $pythonRoot $UserName "RX") -and (Test-Ace $pythonRoot $pkgSid "RX")) {
            Write-Log "python grants already present - skip re-apply (each apply walks the python tree)"
        } else {
            Clear-OurAces $pythonRoot $pkgSid
            $r3 = Invoke-Icacls @($pythonRoot, "/grant", "${UserName}:(OI)(CI)RX", "/C", "/Q") "python grant"
            $r3b = Invoke-Icacls @($pythonRoot, "/grant", "*${pkgSid}:(OI)(CI)RX", "/C", "/Q") "python pkg grant"
            Write-Log "ACL: GRANT RX -> $pythonRoot (bs-sandbox + package SID)"
        }

        $chainCount = 0
        foreach ($d in (Get-SandboxChainDirs)) {
            if (-not (Test-Path -LiteralPath $d)) { Write-Log "chain dir ABSENT (skipped): $d"; continue }
            if (Test-Ace $d $pkgSid "X") { continue }
            Invoke-Icacls @($d, "/grant", "*${pkgSid}:(X)", "/C", "/Q") "chain traverse" | Out-Null
            $chainCount++
        }
        Write-Log "ACL: GRANT traverse X -> package SID on $chainCount path folders (single-object edits, no tree walks)"

        Add-Type -AssemblyName System.Security
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($pwdPlain)
        $entropy = [System.Text.Encoding]::UTF8.GetBytes($EntropyPlain)
        $blob = [System.Security.Cryptography.ProtectedData]::Protect(
            $bytes, $entropy, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        $credDir = Split-Path -Parent $CredFile
        if (-not (Test-Path -LiteralPath $credDir)) { New-Item -ItemType Directory -Path $credDir -Force | Out-Null }
        [System.IO.File]::WriteAllBytes($CredFile, $blob)
        @{ user = $UserName; python = $pythonExe; created = (Get-Date -Format o) } |
            ConvertTo-Json | Set-Content -LiteralPath $MetaFile -Encoding UTF8
        Write-Log "CREDENTIAL: DPAPI blob -> $CredFile ($($blob.Length)B, entropy '$EntropyPlain'); meta -> $MetaFile. Password NOT printed."

        Write-Log "--- verification (ground truth) ---"
        $checkInf = Join-Path $env:TEMP "bs_sandbox_rights_check.inf"
        & $Secedit /export /cfg $checkInf /areas USER_RIGHTS /quiet 2>&1 | Out-Null
        if (Test-Path -LiteralPath $checkInf) {
            $rightsOk = $true
            foreach ($line in (Get-Content -LiteralPath $checkInf)) {
                if ($line -match "^(SeInteractiveLogonRight|SeBatchLogonRight)\s*=") {
                    Write-Log "RIGHTS: $line"
                    $hasOurs = ($line -match [regex]::Escape($sid)) -or
                               ($line -match [regex]::Escape($UserName))
                    if (-not $hasOurs) { $rightsOk = $false }
                }
            }
            Remove-Item -LiteralPath $checkInf -Force -ErrorAction SilentlyContinue
            if (-not $rightsOk) {
                throw "logon rights verification FAILED: SID $sid missing from exported rights."
            }
            Write-Log "RIGHTS assert OK: SID $sid present in both exported rights."
        }
        Write-Log "ACL dump - scratch:"; Show-Grants $ScratchRoot "scratch"
        Write-Log "ACL dump - repo:";     Show-Grants $RepoRoot "repo"
        Write-Log "ACL dump - python:";   Show-Grants $pythonRoot "python"
        Write-Log "ACL dump - profile:";  Show-Grants $ProfileRoot "profile(root)"
        Assert-Ace $ScratchRoot $UserName "M" "scratch bs-sandbox"
        Assert-Ace $ScratchRoot $pkgSid "M" "scratch package"
        Assert-AceDeny $RepoRoot $UserName "RX" "repo bs-sandbox deny"
        Assert-Ace $pythonRoot $UserName "RX" "python bs-sandbox"
        Assert-Ace $pythonRoot $pkgSid "RX" "python package"
        Assert-Ace $ProfileRoot $pkgSid "X" "profile package traverse"
        Assert-Ace (Join-Path $ProfileRoot "AppData\Local") $pkgSid "X" "AppData\Local traverse"
        Assert-Ace (Join-Path $RepoRoot "daemon") $pkgSid "X" "daemon traverse"
        Write-Log "ENABLED: account '$UserName' ready. Daemon contract: $CredFile + $MetaFile."
        Write-Log "Re-check anytime (no admin): scripts\sandbox_user_setup.ps1 -Mode status"
        exit 0
    }
}
