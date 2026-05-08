<#
.SYNOPSIS
    Tear down the AWS resources created by deploy-aws.ps1.

.DESCRIPTION
    Reads the same deployment-info.txt that load-deployment.ps1 reads,
    terminates the EC2 instance, and removes the security group + key pair.
    Optionally also deletes the .pem and the info file from disk.

    DESTRUCTIVE: the EC2 instance's disk (and your Telegram sessions) is
    permanently destroyed by this script. Use with care.

.PARAMETER InfoFile
    Path to deployment-info.txt produced by deploy-aws.ps1.

.PARAMETER KeepLocalFiles
    If set, the .pem and deployment-info.txt are left on disk (only the
    AWS resources are torn down).

.EXAMPLE
    .\teardown-aws.ps1 -InfoFile C:\Users\me\tg-forwarder-aws\deployment-info.txt
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InfoFile,

    [switch]$KeepLocalFiles
)

$ErrorActionPreference = "Continue"   # we want to attempt every step even if one fails

if (-not (Test-Path $InfoFile)) {
    Write-Host "[x] $InfoFile not found." -ForegroundColor Red
    exit 1
}

# Parse the info file
$info = @{}
Get-Content $InfoFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    if ($line -match "^([A-Z_]+)\s*=\s*(.+)$") {
        $info[$Matches[1]] = $Matches[2].Trim()
    }
}

Write-Host ""
Write-Host "About to TEAR DOWN:" -ForegroundColor Yellow
Write-Host "  Region       : $($info.REGION)"
Write-Host "  Instance     : $($info.INSTANCE_ID)"
Write-Host "  Security grp : $($info.SG_ID)"
Write-Host "  Key pair     : $($info.KEY_NAME)"
if (-not $KeepLocalFiles) {
    Write-Host "  Local files  : $($info.KEY_PATH)" -ForegroundColor Yellow
    Write-Host "                 $InfoFile" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "This is DESTRUCTIVE. Telegram sessions and forwarder state on the instance will be lost."
$confirm = Read-Host "Type 'yes' to proceed"
if ($confirm -ne "yes") {
    Write-Host "Aborted." -ForegroundColor Cyan
    exit 0
}

if ($info.INSTANCE_ID) {
    Write-Host "[*] Terminating instance $($info.INSTANCE_ID)..." -ForegroundColor Cyan
    & aws ec2 terminate-instances --region $info.REGION --instance-ids $info.INSTANCE_ID 2>&1 | Out-Null
    & aws ec2 wait instance-terminated --region $info.REGION --instance-ids $info.INSTANCE_ID 2>&1 | Out-Null
    Write-Host "[+] Instance terminated." -ForegroundColor Green
}

if ($info.SG_ID) {
    Write-Host "[*] Deleting security group $($info.SG_ID)..." -ForegroundColor Cyan
    & aws ec2 delete-security-group --region $info.REGION --group-id $info.SG_ID 2>&1 | Out-Null
}

if ($info.KEY_NAME) {
    Write-Host "[*] Deleting key pair $($info.KEY_NAME)..." -ForegroundColor Cyan
    & aws ec2 delete-key-pair --region $info.REGION --key-name $info.KEY_NAME 2>&1 | Out-Null
}

if (-not $KeepLocalFiles) {
    if ($info.KEY_PATH -and (Test-Path $info.KEY_PATH)) {
        Remove-Item $info.KEY_PATH -Force
    }
    Remove-Item $InfoFile -Force
    Write-Host "[+] Local files removed." -ForegroundColor Green
}

Write-Host ""
Write-Host "[+] Teardown complete." -ForegroundColor Green
