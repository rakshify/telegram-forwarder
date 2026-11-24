<#
.SYNOPSIS
    Reload deployment info from a deployment-info.txt and provide connect/start/stop helpers.

.DESCRIPTION
    Sets process-level env vars (AWS_PROFILE, REGION, INSTANCE_ID, KEY_PATH,
    SG_ID, ...) from the info file written by deploy-aws.ps1, then defines:

      Connect-Forwarder       SSH into the instance (re-queries current public IP)
      Start-Forwarder         Start the instance (if stopped) and SSH in
      Stop-Forwarder          Stop the instance (preserves disk + sessions)
      Update-ForwarderIngress Point the SSH rule at your current public IP
                              (run if SSH times out); old IPs are revoked
      Show-Forwarder          Print all loaded env vars

    The helpers use the deployment's AWS_PROFILE. Because $env:AWS_PROFILE is
    set, plain `aws` commands in this shell use that profile too.

.PARAMETER InfoFile
    Path to deployment-info.txt produced by deploy-aws.ps1.

.PARAMETER AwsProfile
    AWS CLI profile for info files without an AWS_PROFILE line. Default: tgf.

.EXAMPLE
    . .\load-deployment.ps1 -InfoFile C:\Users\me\tg-forwarder-aws\deployment-info.txt
    Connect-Forwarder

.NOTES
    DOT-SOURCE this script (`. .\load-deployment.ps1 ...`) so the env vars and
    functions remain in your shell. Running it without the leading dot puts
    them in a child shell that exits immediately.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$InfoFile,

    [string]$AwsProfile = "tgf"
)

if (-not (Test-Path $InfoFile)) {
    Write-Host "[x] $InfoFile not found." -ForegroundColor Red
    return
}

# Parse "KEY = VALUE" lines, ignoring comments and blanks
$loaded = @{}
Get-Content $InfoFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    if ($line -match "^([A-Z_]+)\s*=\s*(.+)$") {
        $loaded[$Matches[1]] = $Matches[2].Trim()
    }
}

if (-not $loaded.REGION -or -not $loaded.INSTANCE_ID -or -not $loaded.KEY_PATH) {
    Write-Host "[x] Info file is missing required keys (REGION / INSTANCE_ID / KEY_PATH)." -ForegroundColor Red
    return
}
if (-not $loaded.AWS_PROFILE) { $loaded.AWS_PROFILE = $AwsProfile }   # older info files
foreach ($name in @($loaded.Keys)) {
    Set-Item -Path "env:$name" -Value $loaded[$name]
}
Remove-Variable loaded, name, line -ErrorAction SilentlyContinue

Write-Host "[+] Loaded deployment from $InfoFile" -ForegroundColor Green
Write-Host "    AWS_PROFILE = $env:AWS_PROFILE"
Write-Host "    INSTANCE_ID = $env:INSTANCE_ID"
Write-Host "    REGION      = $env:REGION"
Write-Host "    KEY_PATH    = $env:KEY_PATH"
Write-Host ""

# ---------------------------------------------------------------- functions

# aws with the deployment's profile + region. Returns @{ Ok; Out; Err } and
# never throws, whatever $ErrorActionPreference your shell uses.
function _Invoke-ForwarderAws {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & aws --profile $env:AWS_PROFILE --region $env:REGION @args 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
    $all = @($lines)
    [pscustomobject]@{
        Ok  = ($code -eq 0)
        Out = (@($all | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n").Trim()
        Err = (@($all | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] }) -join "`n").Trim()
    }
}

function Show-Forwarder {
    Get-ChildItem env: | Where-Object {
        $_.Name -in @('AWS_PROFILE','ACCOUNT_ID','REGION','INSTANCE_ID','INSTANCE_TYPE','KEY_NAME',
                      'KEY_PATH','SG_NAME','SG_ID','AMI_ID','INSTANCE_TAG','AZ','PUBLIC_IP',
                      'DEPLOYED_AT','DEPLOYED_FROM_IP')
    } | Format-Table Name, Value -AutoSize
}

function _Get-CurrentPublicIp {
    $r = _Invoke-ForwarderAws ec2 describe-instances --instance-ids $env:INSTANCE_ID `
        --query "Reservations[0].Instances[0].PublicIpAddress" --output text
    if (-not $r.Ok -or -not $r.Out -or $r.Out -eq "None") { return $null }
    return $r.Out
}

function _Get-InstanceState {
    $r = _Invoke-ForwarderAws ec2 describe-instances --instance-ids $env:INSTANCE_ID `
        --query "Reservations[0].Instances[0].State.Name" --output text
    if (-not $r.Ok) {
        Write-Host "[x] $($r.Err)" -ForegroundColor Red
        return "unknown"
    }
    return $r.Out
}

function Connect-Forwarder {
    $state = _Get-InstanceState
    if ($state -ne "running") {
        Write-Host "[!] Instance is '$state'. Start it first with: Start-Forwarder" -ForegroundColor Yellow
        return
    }
    $ip = _Get-CurrentPublicIp
    if (-not $ip) {
        Write-Host "[x] No public IP found on instance." -ForegroundColor Red
        return
    }
    $env:PUBLIC_IP = $ip
    Write-Host "[*] Connecting to ec2-user@$ip..." -ForegroundColor Cyan
    ssh -i $env:KEY_PATH "ec2-user@$ip"
}

function Start-Forwarder {
    $state = _Get-InstanceState
    if ($state -eq "running") {
        Write-Host "[*] Instance already running." -ForegroundColor Cyan
    } else {
        Write-Host "[*] Starting instance $env:INSTANCE_ID..." -ForegroundColor Cyan
        $r = _Invoke-ForwarderAws ec2 start-instances --instance-ids $env:INSTANCE_ID
        if (-not $r.Ok) { Write-Host "[x] $($r.Err)" -ForegroundColor Red; return }
        $r = _Invoke-ForwarderAws ec2 wait instance-running --instance-ids $env:INSTANCE_ID
        if (-not $r.Ok) { Write-Host "[x] $($r.Err)" -ForegroundColor Red; return }
    }
    Connect-Forwarder
}

function Stop-Forwarder {
    Write-Host "[*] Stopping instance $env:INSTANCE_ID..." -ForegroundColor Cyan
    $r = _Invoke-ForwarderAws ec2 stop-instances --instance-ids $env:INSTANCE_ID
    if (-not $r.Ok) { Write-Host "[x] $($r.Err)" -ForegroundColor Red; return }
    $r = _Invoke-ForwarderAws ec2 wait instance-stopped --instance-ids $env:INSTANCE_ID
    if (-not $r.Ok) { Write-Host "[x] $($r.Err)" -ForegroundColor Red; return }
    Write-Host "[+] Stopped. (Disk + sessions preserved.)" -ForegroundColor Green
}

function Update-ForwarderIngress {
    # Useful when your laptop's public IP has changed and SSH times out. Old
    # IPs are revoked: a mobile IP you no longer hold can go to someone else.
    try {
        $myIp = "$(Invoke-RestMethod 'https://checkip.amazonaws.com' -TimeoutSec 10)".Trim()
    } catch {
        Write-Host "[x] Could not determine your public IP: $_" -ForegroundColor Red
        return
    }
    $newCidr = "$myIp/32"

    $r = _Invoke-ForwarderAws ec2 describe-security-groups --group-ids $env:SG_ID `
        --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`].IpRanges[].CidrIp' --output text
    if (-not $r.Ok) { Write-Host "[x] $($r.Err)" -ForegroundColor Red; return }
    $existing = @($r.Out -split "\s+" | Where-Object { $_ })

    # Add the new rule before revoking old ones, so a failure can't lock you out.
    if ($existing -contains $newCidr) {
        Write-Host "[*] Rule for $newCidr already exists." -ForegroundColor Cyan
    } else {
        Write-Host "[*] Authorizing SSH from $newCidr on $env:SG_ID..." -ForegroundColor Cyan
        $r = _Invoke-ForwarderAws ec2 authorize-security-group-ingress --group-id $env:SG_ID `
            --protocol tcp --port 22 --cidr $newCidr
        if (-not $r.Ok) { Write-Host "[x] $($r.Err)" -ForegroundColor Red; return }
        Write-Host "[+] Added." -ForegroundColor Green
    }
    foreach ($cidr in $existing) {
        if ($cidr -eq $newCidr) { continue }
        $r = _Invoke-ForwarderAws ec2 revoke-security-group-ingress --group-id $env:SG_ID `
            --protocol tcp --port 22 --cidr $cidr
        if ($r.Ok) { Write-Host "[+] Revoked old rule $($cidr)." -ForegroundColor Green }
        else { Write-Host "[x] Could not revoke $($cidr): $($r.Err)" -ForegroundColor Red }
    }
}

Write-Host "Available commands:" -ForegroundColor Cyan
Write-Host "  Show-Forwarder          show all loaded env vars"
Write-Host "  Connect-Forwarder       SSH into the running instance"
Write-Host "  Start-Forwarder         start instance and SSH in"
Write-Host "  Stop-Forwarder          stop the instance (keeps disk)"
Write-Host "  Update-ForwarderIngress point SSH access at your current IP"
