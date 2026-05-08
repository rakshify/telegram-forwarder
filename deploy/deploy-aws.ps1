<#
.SYNOPSIS
    Provisions a t3.micro EC2 instance pre-configured for telegram-forwarder.

.DESCRIPTION
    Creates an SSH key pair, security group (SSH from your current IP), and
    launches an Amazon Linux 2023 instance with Docker + buildx + compose
    pre-installed via cloud-init.

    Saves the resulting .pem file and a deployment-info.txt (containing
    INSTANCE_ID, SG_ID, KEY_NAME, REGION, etc.) into the folder you specify.

    The script is idempotent-ish: re-run it with -Force to tear down and
    recreate from scratch.

.PARAMETER OutputFolder
    Folder where the .pem and deployment-info.txt will be written.
    Created if it doesn't exist.

.PARAMETER Region
    AWS region. Default: us-east-1.

.PARAMETER InstanceType
    EC2 instance type. Default: t3.micro.

.PARAMETER NamePrefix
    Prefix for AWS resource names (key pair, security group, instance tag).
    Default: tg-forwarder.

.PARAMETER Force
    If a deployment-info.txt already exists in OutputFolder, terminate the
    referenced instance and recreate. Without -Force, the script aborts.

.EXAMPLE
    .\deploy-aws.ps1 -OutputFolder C:\Users\me\tg-forwarder-aws

.EXAMPLE
    .\deploy-aws.ps1 -OutputFolder .\aws-deploy -Region us-west-2 -Force
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputFolder,

    [string]$Region = "us-east-1",

    [string]$InstanceType = "t3.micro",

    [string]$NamePrefix = "tg-forwarder",

    [switch]$Force
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- helpers

function Info  { param($m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok    { param($m) Write-Host "[+] $m" -ForegroundColor Green }
function Warn  { param($m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Die   { param($m) Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

function Ensure-AwsCli {
    if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
        Die "AWS CLI not found in PATH. Install from https://awscli.amazonaws.com/AWSCLIV2.msi"
    }
    try {
        aws sts get-caller-identity --query 'Account' --output text 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw }
    } catch {
        Die "aws sts get-caller-identity failed. Run 'aws configure' first."
    }
}

function Ensure-Folder {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
        Ok "Created folder: $Path"
    }
    return (Resolve-Path $Path).Path
}

function Get-MyPublicIp {
    try {
        return (Invoke-RestMethod "https://checkip.amazonaws.com" -TimeoutSec 10).Trim()
    } catch {
        Die "Could not determine your public IP. Check internet connection."
    }
}

# Wraps an `aws` invocation, fails loudly if the CLI returns nonzero
function Invoke-Aws {
    $output = & aws @args 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($output | Out-String) -ForegroundColor Red
        Die "AWS CLI command failed: aws $($args -join ' ')"
    }
    return $output
}

# ---------------------------------------------------------------- script

Info "telegram-forwarder AWS provisioner"
Info "Region: $Region   InstanceType: $InstanceType   NamePrefix: $NamePrefix"

Ensure-AwsCli
$OutputFolder = Ensure-Folder $OutputFolder

$KeyName     = "$NamePrefix-key"
$SgName      = "$NamePrefix-sg"
$InstanceTag = $NamePrefix
$KeyFile     = Join-Path $OutputFolder "$KeyName.pem"
$InfoFile    = Join-Path $OutputFolder "deployment-info.txt"
$UserData    = Join-Path $OutputFolder "_user-data.sh"   # _ prefix => internal artifact

# -------- handle existing deployment

if (Test-Path $InfoFile) {
    if (-not $Force) {
        Warn "Found existing $InfoFile. Pass -Force to tear down and recreate, or delete the file manually."
        Die "Aborting to avoid accidental teardown."
    }

    Info "Tearing down existing deployment (-Force)..."
    $existing = Get-Content $InfoFile | ForEach-Object {
        if ($_ -match "^([A-Z_]+)\s*=\s*(.+)$") { @{ Key = $Matches[1]; Value = $Matches[2].Trim() } }
    }
    $existingMap = @{}; $existing | ForEach-Object { $existingMap[$_.Key] = $_.Value }

    if ($existingMap.INSTANCE_ID) {
        Info "  terminating instance $($existingMap.INSTANCE_ID)"
        & aws ec2 terminate-instances --region $existingMap.REGION --instance-ids $existingMap.INSTANCE_ID 2>&1 | Out-Null
        & aws ec2 wait instance-terminated --region $existingMap.REGION --instance-ids $existingMap.INSTANCE_ID 2>&1 | Out-Null
    }
    if ($existingMap.SG_ID) {
        Info "  deleting security group $($existingMap.SG_ID)"
        & aws ec2 delete-security-group --region $existingMap.REGION --group-id $existingMap.SG_ID 2>&1 | Out-Null
    }
    if ($existingMap.KEY_NAME) {
        Info "  deleting key pair $($existingMap.KEY_NAME)"
        & aws ec2 delete-key-pair --region $existingMap.REGION --key-name $existingMap.KEY_NAME 2>&1 | Out-Null
    }
    Remove-Item $InfoFile -Force
    if (Test-Path $KeyFile) { Remove-Item $KeyFile -Force }
    Ok "Teardown complete."
}

# -------- 1. key pair

Info "Creating key pair: $KeyName"
if (Test-Path $KeyFile) {
    Die "$KeyFile already exists. Move it aside or pass -Force to start fresh."
}

# Check whether a key pair with this name already exists in AWS (we won't be able to download its private key).
# describe-key-pairs writes "InvalidKeyPair.NotFound" to stderr if it doesn't exist, which
# is an EXPECTED case for us. We swallow stderr and inspect the exit code.
$existingKey = $null
try {
    $existingKey = & aws ec2 describe-key-pairs --region $Region --key-names $KeyName `
        --query 'KeyPairs[0].KeyName' --output text 2>$null
} catch {
    # AWS CLI wrote to stderr because the key doesn't exist — that's fine, leave $existingKey null.
}
if ($LASTEXITCODE -eq 0 -and $existingKey -eq $KeyName) {
    Die "AWS already has a key pair named '$KeyName' but the .pem isn't in $OutputFolder. Either pass -Force or delete it: aws ec2 delete-key-pair --region $Region --key-name $KeyName"
}

$pemMaterial = Invoke-Aws ec2 create-key-pair --region $Region --key-name $KeyName `
    --query 'KeyMaterial' --output text
# Out-File with -Encoding ascii avoids BOM (ssh rejects keys with BOM)
$pemMaterial | Out-File -Encoding ascii -FilePath $KeyFile
icacls $KeyFile /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
Ok "Saved private key: $KeyFile"

# -------- 2. security group, ingress for SSH from this machine

$myIp = Get-MyPublicIp
Info "Creating security group: $SgName  (SSH from $myIp/32)"
$sgId = (Invoke-Aws ec2 create-security-group --region $Region `
    --group-name $SgName --description "telegram-forwarder SSH access" `
    --query 'GroupId' --output text).Trim()
Invoke-Aws ec2 authorize-security-group-ingress --region $Region `
    --group-id $sgId --protocol tcp --port 22 --cidr "$myIp/32" | Out-Null
Ok "Security group: $sgId"

# -------- 3. AMI

$amiId = (Invoke-Aws ssm get-parameter --region $Region `
    --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 `
    --query 'Parameter.Value' --output text).Trim()
Info "AMI: $amiId"

# -------- 4. user-data — install Docker + buildx + compose plugin on first boot

@'
#!/bin/bash
set -e
dnf update -y
dnf install -y docker
systemctl enable --now docker
usermod -aG docker ec2-user

mkdir -p /usr/local/lib/docker/cli-plugins

# compose v2
curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# buildx (compose build needs >=0.17)
BUILDX_VERSION=$(curl -s https://api.github.com/repos/docker/buildx/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
curl -SL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-amd64" \
    -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
'@ | Out-File -Encoding ascii -FilePath $UserData

# -------- 5. launch

Info "Launching $InstanceType instance..."
$instanceId = (Invoke-Aws ec2 run-instances --region $Region `
    --image-id $amiId `
    --instance-type $InstanceType `
    --key-name $KeyName `
    --security-group-ids $sgId `
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$InstanceTag}]" `
    --user-data "file://$UserData" `
    --query 'Instances[0].InstanceId' --output text).Trim()
Ok "Instance: $instanceId"

Info "Waiting for instance to reach 'running' state..."
Invoke-Aws ec2 wait instance-running --region $Region --instance-ids $instanceId | Out-Null

$publicIp = (Invoke-Aws ec2 describe-instances --region $Region `
    --instance-ids $instanceId `
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text).Trim()
Ok "Public IP: $publicIp"

# -------- 6. write deployment-info.txt

$deployedAt = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss zzz")
$infoContent = @"
# Generated by deploy-aws.ps1 on $deployedAt
# Reload these in any new PowerShell session via:
#   .\load-deployment.ps1 -InfoFile "$InfoFile"

REGION         = $Region
INSTANCE_ID    = $instanceId
INSTANCE_TYPE  = $InstanceType
KEY_NAME       = $KeyName
KEY_PATH       = $KeyFile
SG_NAME        = $SgName
SG_ID          = $sgId
AMI_ID         = $amiId
INSTANCE_TAG   = $InstanceTag
DEPLOYED_AT    = $deployedAt
DEPLOYED_FROM_IP = $myIp

# Dynamic — re-query before use; changes when instance stops/starts
PUBLIC_IP      = $publicIp
"@
$infoContent | Out-File -Encoding ascii -FilePath $InfoFile
Ok "Saved deployment info: $InfoFile"

# Cleanup the user-data temp
if (Test-Path $UserData) { Remove-Item $UserData -Force }

# -------- 7. done

Write-Host ""
Ok "Deployment complete."
Write-Host ""
Write-Host "Wait ~60 seconds for Docker to finish installing on the instance, then SSH in:"
Write-Host ""
Write-Host "  ssh -i `"$KeyFile`" ec2-user@$publicIp" -ForegroundColor Yellow
Write-Host ""
Write-Host "Or use the load-deployment.ps1 helper to set env vars and connect:"
Write-Host ""
Write-Host "  . .\load-deployment.ps1 -InfoFile `"$InfoFile`"" -ForegroundColor Yellow
Write-Host "  Connect-Forwarder" -ForegroundColor Yellow
