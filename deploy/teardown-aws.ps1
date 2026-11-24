<#
.SYNOPSIS
    Tear down the AWS resources created by deploy-aws.ps1.

.DESCRIPTION
    Reads the same deployment-info.txt that load-deployment.ps1 reads,
    terminates the EC2 instance, and removes the security group + key pair.
    Unless -KeepLocalFiles is set, it then deletes the .pem and the info file,
    but only if every AWS step succeeded, so a failed teardown can be re-run.

    Uses the AWS_PROFILE recorded in the info file (-AwsProfile, default
    "tgf", only applies to info files that don't record one), and refuses to
    run if that profile belongs to a different account than the deployment.

    DESTRUCTIVE: the EC2 instance's disk (and your Telegram sessions) is
    permanently destroyed by this script. Use with care.

.PARAMETER InfoFile
    Path to deployment-info.txt produced by deploy-aws.ps1.

.PARAMETER AwsProfile
    AWS CLI profile for info files without an AWS_PROFILE line. Default: tgf.

.PARAMETER KeepLocalFiles
    If set, the .pem and deployment-info.txt are left on disk (only the
    AWS resources are torn down).

.PARAMETER Force
    Skip the "type yes" confirmation (deploy-aws.ps1 -Force uses this).

.EXAMPLE
    .\teardown-aws.ps1 -InfoFile C:\Users\me\tg-forwarder-aws\deployment-info.txt
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InfoFile,

    [string]$AwsProfile = "tgf",

    [switch]$KeepLocalFiles,

    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Info  { param($m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok    { param($m) Write-Host "[+] $m" -ForegroundColor Green }
function Die   { param($m) Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# Runs `aws --profile <AwsProfile> --region <Region> <args>` and returns
# @{ Ok; Out; Err } instead of throwing (see deploy-aws.ps1 for why stderr is
# captured this way).
function Invoke-AwsRaw {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & aws --profile $AwsProfile --region $Region @args 2>&1
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

# Right after an instance terminates, its network interface can take a few
# seconds to let go of the security group (DependencyViolation): retry.
function Remove-SecurityGroup {
    param([string]$GroupId)
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        $r = Invoke-AwsRaw ec2 delete-security-group --group-id $GroupId
        if ($r.Ok -or $r.Err -match "InvalidGroup\.NotFound") { return $true }
        if ($r.Err -notmatch "DependencyViolation") { break }
        Start-Sleep -Seconds 10
    }
    Write-Host $r.Err -ForegroundColor Red
    return $false
}

if (-not (Test-Path $InfoFile)) { Die "$InfoFile not found." }

# Parse the info file
$info = @{}
Get-Content $InfoFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    if ($line -match "^([A-Z_]+)\s*=\s*(.+)$") {
        $info[$Matches[1]] = $Matches[2].Trim()
    }
}

# Anything else (e.g. setup-ec2.sh's env-vars.txt) is refused before touching
# AWS or deleting any local file.
if (-not $info.REGION -or -not ($info.INSTANCE_ID -or $info.SG_ID -or $info.KEY_NAME)) {
    Die "$InfoFile isn't a deployment-info.txt from deploy-aws.ps1 (needs REGION plus INSTANCE_ID / SG_ID / KEY_NAME). Nothing was changed."
}
$Region = $info.REGION
if ($info.AWS_PROFILE) { $AwsProfile = $info.AWS_PROFILE }

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { Die "AWS CLI not found in PATH." }
$identity = Invoke-AwsRaw sts get-caller-identity --query "Account" --output text
if (-not $identity.Ok) {
    Write-Host $identity.Err -ForegroundColor Red
    Die "AWS credentials for profile '$AwsProfile' don't work. Nothing was changed."
}
$AccountId = $identity.Out
if ($info.ACCOUNT_ID -and $info.ACCOUNT_ID -ne $AccountId) {
    Die "Profile '$AwsProfile' is account $AccountId, but this deployment is in account $($info.ACCOUNT_ID). Nothing was changed."
}

Write-Host ""
Write-Host "About to TEAR DOWN (account $AccountId, profile '$AwsProfile'):" -ForegroundColor Yellow
Write-Host "  Region       : $Region"
Write-Host "  Instance     : $($info.INSTANCE_ID)"
Write-Host "  Security grp : $($info.SG_ID)"
Write-Host "  Key pair     : $($info.KEY_NAME)"
if (-not $KeepLocalFiles) {
    Write-Host "  Local files  : $($info.KEY_PATH)" -ForegroundColor Yellow
    Write-Host "                 $InfoFile" -ForegroundColor Yellow
}
Write-Host ""
if (-not $Force) {
    Write-Host "This is DESTRUCTIVE. Telegram sessions and forwarder state on the instance will be lost."
    $confirm = Read-Host "Type 'yes' to proceed"
    if ($confirm -ne "yes") {
        Write-Host "Aborted." -ForegroundColor Cyan
        exit 0
    }
}

# The security group can only go once the instance is gone, so stop here if
# termination fails.
if ($info.INSTANCE_ID) {
    Info "Terminating instance $($info.INSTANCE_ID)..."
    $r = Invoke-AwsRaw ec2 terminate-instances --instance-ids $info.INSTANCE_ID
    if ($r.Ok) {
        $wait = Invoke-AwsRaw ec2 wait instance-terminated --instance-ids $info.INSTANCE_ID
        if (-not $wait.Ok) {
            Write-Host $wait.Err -ForegroundColor Red
            Die "Instance didn't reach 'terminated' yet. Nothing else was changed; re-run in a minute."
        }
        Ok "Instance terminated."
    } elseif ($r.Err -match "InvalidInstanceID\.NotFound") {
        Ok "Instance already gone."
    } else {
        Write-Host $r.Err -ForegroundColor Red
        Die "Could not terminate the instance. Nothing else was changed."
    }
}

$failed = $false
if ($info.SG_ID) {
    Info "Deleting security group $($info.SG_ID)..."
    if (Remove-SecurityGroup $info.SG_ID) { Ok "Security group deleted." } else { $failed = $true }
}
if ($info.KEY_NAME) {
    Info "Deleting key pair $($info.KEY_NAME)..."
    $r = Invoke-AwsRaw ec2 delete-key-pair --key-name $info.KEY_NAME
    if ($r.Ok) { Ok "Key pair deleted." } else { Write-Host $r.Err -ForegroundColor Red; $failed = $true }
}
if ($failed) {
    Die "Some AWS resources could not be deleted (see above). Local files were kept, so you can re-run this."
}

if (-not $KeepLocalFiles) {
    if ($info.KEY_PATH -and (Test-Path $info.KEY_PATH)) {
        Remove-Item $info.KEY_PATH -Force
    }
    Remove-Item $InfoFile -Force
    Ok "Local files removed."
}

Write-Host ""
Ok "Teardown complete."
exit 0
