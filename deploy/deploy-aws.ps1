<#
.SYNOPSIS
    Provisions a t3.micro EC2 instance pre-configured for telegram-forwarder.

.DESCRIPTION
    Creates an SSH key pair, a security group (SSH from your current IP only)
    and an Amazon Linux 2023 instance with Docker + buildx + compose installed
    on first boot (cloud-init). Uses the "tgf" AWS CLI profile unless you pass
    -AwsProfile.

    Saves the .pem and a deployment-info.txt (AWS_PROFILE, REGION,
    INSTANCE_ID, SG_ID, KEY_NAME, ...) into the folder you specify.
    load-deployment.ps1 and teardown-aws.ps1 read that file, including which
    profile to use.

    If a step fails part-way, the resources this run created are deleted
    again. Re-run with -Force to tear down the deployment recorded in
    OutputFolder (via teardown-aws.ps1) and create a fresh one.

    Runs in Windows PowerShell 5.1 and PowerShell 7+ (Windows, Linux, macOS).

.PARAMETER OutputFolder
    Folder where the .pem and deployment-info.txt will be written.
    Created if it doesn't exist.

.PARAMETER AwsProfile
    AWS CLI profile to use. Default: tgf.

.PARAMETER Region
    AWS region. Default: the profile's configured region, else us-east-1.

.PARAMETER InstanceType
    EC2 instance type (x86_64). Default: t3.micro.

.PARAMETER NamePrefix
    Prefix for AWS resource names (key pair, security group, instance tag).
    Default: tg-forwarder.

.PARAMETER Force
    If a deployment-info.txt already exists in OutputFolder, tear that
    deployment down and recreate it. Without -Force, the script aborts.

.EXAMPLE
    .\deploy-aws.ps1 -OutputFolder C:\Users\me\tg-forwarder-aws

.EXAMPLE
    .\deploy-aws.ps1 -OutputFolder .\aws-deploy -AwsProfile tgf -Region us-west-2 -Force
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputFolder,

    [string]$AwsProfile = "tgf",

    [string]$Region = "",

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

# Runs `aws --profile <AwsProfile> --region <Region> <args>` and returns
# @{ Ok; Out; Err } instead of throwing. stderr is captured separately, so a
# CLI warning can never end up inside a value such as the private key, and
# lowering ErrorActionPreference stops Windows PowerShell 5.1 from turning
# redirected stderr into a terminating error.
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

# Like Invoke-AwsRaw, but prints aws's error and exits on failure; returns stdout.
function Invoke-Aws {
    $r = Invoke-AwsRaw @args
    if (-not $r.Ok) {
        if ($r.Err) { Write-Host $r.Err -ForegroundColor Red }
        Die "AWS CLI command failed: aws $($args -join ' ')"
    }
    return $r.Out
}

function Get-MyPublicIp {
    try {
        $ip = "$(Invoke-RestMethod 'https://checkip.amazonaws.com' -TimeoutSec 10)".Trim()
    } catch {
        Die "Could not determine your public IP. Check internet connection."
    }
    if ($ip -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { Die "Unexpected public IP '$ip'." }
    return $ip
}

# ssh refuses private keys that other users can read.
function Protect-KeyFile {
    param([string]$Path)
    if ($env:OS -eq "Windows_NT") {
        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        & icacls $Path /inheritance:r /grant:r "*$($sid):(R)" | Out-Null
    } else {
        & chmod 400 $Path
    }
    if ($LASTEXITCODE -ne 0) { Die "Could not restrict permissions on $Path" }
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

# Runs when provisioning fails part-way: delete what this run created.
function Undo-Provisioning {
    Warn "Provisioning did not finish. Removing what this run created..."
    if ($CreatedInstanceId) {
        Info "  terminating instance $CreatedInstanceId (takes a minute)"
        $null = Invoke-AwsRaw ec2 terminate-instances --instance-ids $CreatedInstanceId
        $null = Invoke-AwsRaw ec2 wait instance-terminated --instance-ids $CreatedInstanceId
    }
    if ($CreatedSgId) {
        Info "  deleting security group $CreatedSgId"
        if (-not (Remove-SecurityGroup $CreatedSgId)) { Warn "  Could not delete $CreatedSgId; delete it by hand." }
    }
    if ($CreatedKey) {
        Info "  deleting key pair $KeyName and $KeyFile"
        $null = Invoke-AwsRaw ec2 delete-key-pair --key-name $KeyName
        try { if (Test-Path $KeyFile) { Remove-Item $KeyFile -Force } }
        catch { Warn "  Could not delete $KeyFile; delete it by hand." }
    }
}

# ---------------------------------------------------------------- pre-flight
# Read-only checks: nothing is created until all of them pass.

Info "telegram-forwarder AWS provisioner"

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Die "AWS CLI not found in PATH. Install it: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
}

$saved = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$knownProfiles = @(& aws configure list-profiles 2>$null)
if (-not $Region) { $Region = "$(& aws configure get region --profile $AwsProfile 2>$null)".Trim() }
$ErrorActionPreference = $saved
if ($knownProfiles -notcontains $AwsProfile) {
    Die "AWS CLI profile '$AwsProfile' not found. Set it up with: aws configure --profile $AwsProfile"
}
if (-not $Region) { $Region = "us-east-1" }

$identity = Invoke-AwsRaw sts get-caller-identity --query "[Account,Arn]" --output text
if (-not $identity.Ok) {
    Write-Host $identity.Err -ForegroundColor Red
    Die "AWS credentials for profile '$AwsProfile' don't work. Check: aws sts get-caller-identity --profile $AwsProfile"
}
$AccountId, $CallerArn = $identity.Out -split "\s+"
Info "Account: $AccountId   Profile: $AwsProfile   Region: $Region"
Info "InstanceType: $InstanceType   NamePrefix: $NamePrefix"
if ($CallerArn -like "*:root") {
    Warn "Profile '$AwsProfile' uses ROOT access keys ($CallerArn). An IAM user is safer (see README)."
}

# Absolute path (relative to PowerShell's current location), so the paths
# saved in deployment-info.txt work from anywhere.
$OutputFolder = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputFolder)

$KeyName     = "$NamePrefix-key"
$SgName      = "$NamePrefix-sg"
$InstanceTag = $NamePrefix
$KeyFile     = Join-Path $OutputFolder "$KeyName.pem"
$InfoFile    = Join-Path $OutputFolder "deployment-info.txt"
$UserData    = Join-Path $OutputFolder "_user-data.sh"   # _ prefix => internal artifact

# -------- existing deployment in this folder

if (Test-Path $InfoFile) {
    if (-not $Force) {
        Warn "Found existing $InfoFile. Pass -Force to tear down and recreate, or delete the file manually."
        Die "Aborting to avoid accidental teardown."
    }
    Info "Tearing down the existing deployment first (-Force)..."
    & (Join-Path $PSScriptRoot "teardown-aws.ps1") -InfoFile $InfoFile -AwsProfile $AwsProfile -Force
    if ($LASTEXITCODE -ne 0) { Die "Teardown of the existing deployment failed, so no new one was created." }
}

if (Test-Path $KeyFile) {
    Die "$KeyFile already exists. Move it aside first."
}

# Check whether a key pair with this name already exists in AWS (we wouldn't
# be able to download its private key).
if ((Invoke-AwsRaw ec2 describe-key-pairs --key-names $KeyName).Ok) {
    Die "AWS already has a key pair named '$KeyName' but the .pem isn't in $OutputFolder. Delete it: aws ec2 delete-key-pair --profile $AwsProfile --region $Region --key-name $KeyName"
}

$VpcId = Invoke-Aws ec2 describe-vpcs --filters "Name=is-default,Values=true" --query "Vpcs[0].VpcId" --output text
if ($VpcId -notlike "vpc-*") {
    Die "No default VPC in $Region. Create one: aws ec2 create-default-vpc --profile $AwsProfile --region $Region"
}

$existingSg = Invoke-Aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VpcId" "Name=group-name,Values=$SgName" --query "SecurityGroups[0].GroupId" --output text
if ($existingSg -like "sg-*") {
    Die "Security group '$SgName' already exists ($existingSg). Delete it: aws ec2 delete-security-group --profile $AwsProfile --region $Region --group-id $existingSg"
}

# Not every AZ offers every instance type (us-east-1e has no t3.micro), and a
# launch without a subnet can land there. Pick the first default subnet whose
# AZ offers $InstanceType.
$offeredAzs = (Invoke-Aws ec2 describe-instance-type-offerings --location-type availability-zone --filters "Name=instance-type,Values=$InstanceType" --query "InstanceTypeOfferings[].Location" --output text) -split "\s+"
$subnetRows = (Invoke-Aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VpcId" "Name=default-for-az,Values=true" --query "sort_by(Subnets,&AvailabilityZone)[].[AvailabilityZone,SubnetId]" --output text) -split "`n"
$Az = $null
$SubnetId = $null
foreach ($row in $subnetRows) {
    $rowAz, $rowSubnet = $row.Trim() -split "\s+"
    if ($rowAz -and ($offeredAzs -contains $rowAz)) { $Az = $rowAz; $SubnetId = $rowSubnet; break }
}
if (-not $SubnetId) { Die "No default subnet in $Region is in an AZ that offers $InstanceType." }

# AWS's "latest Amazon Linux 2023" pointer lives in SSM. An IAM user with only
# AmazonEC2FullAccess (what the README suggests) can't read SSM, so fall back
# to searching EC2 images.
$ssm = Invoke-AwsRaw ssm get-parameter --name "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" --query "Parameter.Value" --output text
if ($ssm.Ok -and $ssm.Out -like "ami-*") {
    $AmiId = $ssm.Out
} else {
    Info "Can't read the SSM AMI pointer (needs ssm:GetParameter); searching EC2 images instead."
    $AmiId = Invoke-Aws ec2 describe-images --owners amazon --filters "Name=name,Values=al2023-ami-2023.*-x86_64" "Name=state,Values=available" --query "sort_by(Images,&join('',[CreationDate,Name]))[-1].ImageId" --output text
}
if ($AmiId -notlike "ami-*") { Die "Could not find an Amazon Linux 2023 AMI in $Region." }
Info "AMI: $AmiId   AZ: $Az ($SubnetId)"

$myIp = Get-MyPublicIp

# ---------------------------------------------------------------- create
# From here on, a failure (or Ctrl+C) deletes whatever this run created.

if (-not (Test-Path $OutputFolder)) {
    New-Item -ItemType Directory -Path $OutputFolder | Out-Null
    Ok "Created folder: $OutputFolder"
}

$CreatedKey = $false
$CreatedSgId = $null
$CreatedInstanceId = $null
$succeeded = $false
try {
    # -------- 1. key pair
    Info "Creating key pair: $KeyName"
    $pem = Invoke-Aws ec2 create-key-pair --key-name $KeyName --query "KeyMaterial" --output text
    $CreatedKey = $true
    # ASCII, LF line endings, no BOM: the format ssh accepts on every OS.
    [System.IO.File]::WriteAllText($KeyFile, ($pem -replace "`r", "") + "`n", [System.Text.Encoding]::ASCII)
    Protect-KeyFile $KeyFile
    Ok "Saved private key: $KeyFile"

    # -------- 2. security group, ingress for SSH from this machine
    Info "Creating security group: $SgName  (SSH from $myIp/32)"
    $CreatedSgId = Invoke-Aws ec2 create-security-group --group-name $SgName --vpc-id $VpcId --description "telegram-forwarder SSH access" --query "GroupId" --output text
    Invoke-Aws ec2 authorize-security-group-ingress --group-id $CreatedSgId --protocol tcp --port 22 --cidr "$myIp/32" | Out-Null
    Ok "Security group: $CreatedSgId"

    # -------- 3. user-data: install Docker + buildx + compose plugin on first boot
    $userDataScript = @'
#!/bin/bash
# telegram-forwarder first-boot setup (cloud-init). Log: /var/log/cloud-init-output.log
set -euxo pipefail
dnf update -y
dnf install -y docker
systemctl enable --now docker
usermod -aG docker ec2-user

mkdir -p /usr/local/lib/docker/cli-plugins

# compose v2
curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# buildx (compose build needs >=0.17)
BUILDX_VERSION=$(curl -fsSL https://api.github.com/repos/docker/buildx/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
curl -fsSL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-amd64" \
    -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
'@
    # Always LF: if this .ps1 was checked out with CRLF line endings (git on
    # Windows), a CRLF user-data script dies on boot with "/bin/bash^M: bad interpreter".
    [System.IO.File]::WriteAllText($UserData, ($userDataScript -replace "`r", "") + "`n", [System.Text.Encoding]::ASCII)

    # -------- 4. launch
    Info "Launching $InstanceType instance in $Az..."
    $CreatedInstanceId = Invoke-Aws ec2 run-instances `
        --image-id $AmiId `
        --instance-type $InstanceType `
        --key-name $KeyName `
        --subnet-id $SubnetId `
        --security-group-ids $CreatedSgId `
        --associate-public-ip-address `
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$InstanceTag}]" `
        --user-data "file://$UserData" `
        --query "Instances[0].InstanceId" --output text
    Ok "Instance: $CreatedInstanceId"

    Info "Waiting for instance to reach 'running' state..."
    Invoke-Aws ec2 wait instance-running --instance-ids $CreatedInstanceId | Out-Null

    $publicIp = Invoke-Aws ec2 describe-instances --instance-ids $CreatedInstanceId --query "Reservations[0].Instances[0].PublicIpAddress" --output text
    if ($publicIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { Die "Instance $CreatedInstanceId came up without a public IP." }
    Ok "Public IP: $publicIp"

    # -------- 5. write deployment-info.txt
    $deployedAt = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss zzz")
    $infoContent = @"
# Generated by deploy-aws.ps1 on $deployedAt
# Reload these in any new PowerShell session via:
#   . .\load-deployment.ps1 -InfoFile "$InfoFile"

AWS_PROFILE    = $AwsProfile
ACCOUNT_ID     = $AccountId
REGION         = $Region
INSTANCE_ID    = $CreatedInstanceId
INSTANCE_TYPE  = $InstanceType
KEY_NAME       = $KeyName
KEY_PATH       = $KeyFile
SG_NAME        = $SgName
SG_ID          = $CreatedSgId
AMI_ID         = $AmiId
INSTANCE_TAG   = $InstanceTag
AZ             = $Az
DEPLOYED_AT    = $deployedAt
DEPLOYED_FROM_IP = $myIp

# Dynamic - re-query before use; changes when instance stops/starts
PUBLIC_IP      = $publicIp
"@
    $infoContent | Out-File -Encoding ascii -FilePath $InfoFile
    Ok "Saved deployment info: $InfoFile"
    $succeeded = $true
} finally {
    if (Test-Path $UserData) { Remove-Item $UserData -Force }
    if (-not $succeeded) { Undo-Provisioning }
}

# -------- done

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
exit 0
