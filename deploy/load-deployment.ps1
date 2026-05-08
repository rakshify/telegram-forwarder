<#
.SYNOPSIS
    Reload deployment info from a deployment-info.txt and provide connect/start/stop helpers.

.DESCRIPTION
    Sets process-level env vars (REGION, INSTANCE_ID, KEY_PATH, SG_ID, ...) from
    the info file written by deploy-aws.ps1, then defines:

      Connect-Forwarder       SSH into the instance (re-queries current public IP)
      Start-Forwarder         Start the instance (if stopped) and SSH in
      Stop-Forwarder          Stop the instance (preserves disk + sessions)
      Update-ForwarderIngress Add your current public IP to the SG (run if SSH times out)
      Show-Forwarder          Print all loaded env vars

.PARAMETER InfoFile
    Path to deployment-info.txt produced by deploy-aws.ps1.

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
    [string]$InfoFile
)

if (-not (Test-Path $InfoFile)) {
    Write-Host "[x] $InfoFile not found." -ForegroundColor Red
    return
}

# Parse "KEY = VALUE" lines, ignoring comments and blanks
Get-Content $InfoFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    if ($line -match "^([A-Z_]+)\s*=\s*(.+)$") {
        $name  = $Matches[1]
        $value = $Matches[2].Trim()
        Set-Item -Path "env:$name" -Value $value
    }
}

if (-not $env:REGION -or -not $env:INSTANCE_ID -or -not $env:KEY_PATH) {
    Write-Host "[x] Info file is missing required keys (REGION / INSTANCE_ID / KEY_PATH)." -ForegroundColor Red
    return
}

Write-Host "[+] Loaded deployment from $InfoFile" -ForegroundColor Green
Write-Host "    INSTANCE_ID = $env:INSTANCE_ID"
Write-Host "    REGION      = $env:REGION"
Write-Host "    KEY_PATH    = $env:KEY_PATH"
Write-Host ""

# ---------------------------------------------------------------- functions

function Show-Forwarder {
    Get-ChildItem env: | Where-Object {
        $_.Name -in @('REGION','INSTANCE_ID','INSTANCE_TYPE','KEY_NAME','KEY_PATH',
                      'SG_NAME','SG_ID','AMI_ID','INSTANCE_TAG','PUBLIC_IP',
                      'DEPLOYED_AT','DEPLOYED_FROM_IP')
    } | Format-Table Name, Value -AutoSize
}

function _Get-CurrentPublicIp {
    $ip = aws ec2 describe-instances --region $env:REGION `
        --instance-ids $env:INSTANCE_ID `
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $ip -or $ip -eq "None") { return $null }
    return $ip.Trim()
}

function _Get-InstanceState {
    $state = aws ec2 describe-instances --region $env:REGION `
        --instance-ids $env:INSTANCE_ID `
        --query 'Reservations[0].Instances[0].State.Name' --output text 2>$null
    if ($LASTEXITCODE -ne 0) { return "unknown" }
    return $state.Trim()
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
        aws ec2 start-instances --region $env:REGION --instance-ids $env:INSTANCE_ID | Out-Null
        aws ec2 wait instance-running --region $env:REGION --instance-ids $env:INSTANCE_ID
    }
    Connect-Forwarder
}

function Stop-Forwarder {
    Write-Host "[*] Stopping instance $env:INSTANCE_ID..." -ForegroundColor Cyan
    aws ec2 stop-instances --region $env:REGION --instance-ids $env:INSTANCE_ID | Out-Null
    aws ec2 wait instance-stopped --region $env:REGION --instance-ids $env:INSTANCE_ID
    Write-Host "[+] Stopped. (Disk + sessions preserved.)" -ForegroundColor Green
}

function Update-ForwarderIngress {
    # Useful when your laptop's public IP has changed and SSH times out.
    $myIp = (Invoke-RestMethod "https://checkip.amazonaws.com" -TimeoutSec 10).Trim()
    Write-Host "[*] Authorizing SSH from $myIp/32 on $env:SG_ID..." -ForegroundColor Cyan
    $output = aws ec2 authorize-security-group-ingress --region $env:REGION `
        --group-id $env:SG_ID --protocol tcp --port 22 --cidr "$myIp/32" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[+] Added." -ForegroundColor Green
    } elseif ($output -match "InvalidPermission.Duplicate") {
        Write-Host "[*] Rule for $myIp/32 already exists." -ForegroundColor Cyan
    } else {
        Write-Host "[x] $output" -ForegroundColor Red
    }
}

Write-Host "Available commands:" -ForegroundColor Cyan
Write-Host "  Show-Forwarder          show all loaded env vars"
Write-Host "  Connect-Forwarder       SSH into the running instance"
Write-Host "  Start-Forwarder         start instance and SSH in"
Write-Host "  Stop-Forwarder          stop the instance (keeps disk)"
Write-Host "  Update-ForwarderIngress add your current IP to the SG"
