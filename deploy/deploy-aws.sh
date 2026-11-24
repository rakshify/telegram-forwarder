#!/usr/bin/env bash
#
# deploy-aws.sh: provision a t3.micro EC2 instance pre-configured for
# telegram-forwarder. Bash port of deploy-aws.ps1: same behavior, same
# deployment-info.txt format (the .sh and .ps1 tools read each other's files).
#
# Creates an SSH key pair, a security group (SSH from your current IP only)
# and an Amazon Linux 2023 instance with Docker + buildx + compose installed
# on first boot (cloud-init). Uses the "tgf" AWS CLI profile unless you pass
# --aws-profile.
#
# Saves the .pem and a deployment-info.txt (AWS_PROFILE, REGION, INSTANCE_ID,
# SG_ID, KEY_NAME, ...) into the folder you specify. load-deployment.sh and
# teardown-aws.sh read that file, including which profile to use.
#
# If a step fails part-way, the resources this run created are deleted again.
# Re-run with --force to tear down the deployment recorded in the output
# folder (via teardown-aws.sh) and create a fresh one.
#
# Usage:
#   deploy/deploy-aws.sh --output-folder DIR [options]
#
# Options (the PowerShell names, e.g. -OutputFolder, work too):
#   --output-folder DIR    where the .pem and deployment-info.txt go
#                          (required; created if it doesn't exist)
#   --aws-profile NAME     AWS CLI profile. Default: tgf
#   --region REGION        Default: the profile's region, else us-east-1
#   --instance-type TYPE   EC2 instance type (x86_64). Default: t3.micro
#   --name-prefix PREFIX   prefix for the key pair, security group and
#                          instance tag. Default: tg-forwarder
#   --force                if DIR already holds a deployment-info.txt, tear
#                          that deployment down and recreate it
#   -h, --help             show this help
#
# Examples:
#   deploy/deploy-aws.sh --output-folder /data/Documents/tg-forwarder-aws
#   deploy/deploy-aws.sh --output-folder ./aws-deploy --region us-west-2 --force

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"

# ---------------------------------------------------------------- helpers

say() {   # say <color> <tag> <fd> <message...>; colors only on a terminal
  local color=$1 tag=$2 fd=$3
  shift 3
  if [[ -t $fd ]]; then printf '\033[%sm[%s] %s\033[0m\n' "$color" "$tag" "$*" >&"$fd"
  else printf '[%s] %s\n' "$tag" "$*" >&"$fd"; fi
}
info() { say 36 '*' 1 "$@"; }
ok()   { say 32 '+' 1 "$@"; }
warn() { say 33 '!' 2 "$@"; }
die()  { say 31 x 2 "$@"; exit 1; }
usage() { awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$SCRIPT_PATH"; }
value() { [[ $# -ge 2 && -n $2 ]] || die "$1 needs a value (try --help)"; printf '%s' "$2"; }

OUTPUT_FOLDER=""
PROFILE="tgf"
REGION=""
INSTANCE_TYPE="t3.micro"
NAME_PREFIX="tg-forwarder"
FORCE=0
while (($#)); do
  case "$1" in
    --output-folder|-OutputFolder) OUTPUT_FOLDER=$(value "$@"); shift ;;
    --aws-profile|-AwsProfile)     PROFILE=$(value "$@"); shift ;;
    --region|-Region)              REGION=$(value "$@"); shift ;;
    --instance-type|-InstanceType) INSTANCE_TYPE=$(value "$@"); shift ;;
    --name-prefix|-NamePrefix)     NAME_PREFIX=$(value "$@"); shift ;;
    --force|-Force)                FORCE=1 ;;
    -h|--help)                     usage; exit 0 ;;
    *)                             die "unknown argument '$1' (try --help)" ;;
  esac
  shift
done
[[ -n $OUTPUT_FOLDER ]] || die "--output-folder is required (try --help)"

# Run aws with this deployment's profile and region. An explicit --profile
# also makes the CLI ignore AWS_PROFILE / AWS_ACCESS_KEY_ID from your shell.
awsx() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

# Like awsx, but exits on failure (aws prints its own error to stderr first).
# Inside $(...), the exit ends the subshell and set -e then stops the script.
aws_ok() {
  local out
  if out=$(awsx "$@"); then
    if [[ -n $out ]]; then printf '%s\n' "$out"; fi
    return 0
  fi
  die "AWS CLI command failed: aws $*"
}

# Right after an instance terminates, its network interface can take a few
# seconds to let go of the security group (DependencyViolation): retry.
remove_security_group() {
  local group_id=$1 attempt err=""
  for attempt in 1 2 3 4 5 6; do
    if err=$(awsx ec2 delete-security-group --group-id "$group_id" 2>&1 >/dev/null); then return 0; fi
    if [[ $err == *InvalidGroup.NotFound* ]]; then return 0; fi
    if [[ $err != *DependencyViolation* ]]; then break; fi
    sleep 10
  done
  printf '%s\n' "$err" >&2
  return 1
}

# Runs when provisioning fails part-way: delete what this run created.
undo_provisioning() {
  warn "Provisioning did not finish. Removing what this run created..."
  if [[ -n $CREATED_INSTANCE_ID ]]; then
    info "  terminating instance $CREATED_INSTANCE_ID (takes a minute)"
    awsx ec2 terminate-instances --instance-ids "$CREATED_INSTANCE_ID" >/dev/null 2>&1 || true
    awsx ec2 wait instance-terminated --instance-ids "$CREATED_INSTANCE_ID" >/dev/null 2>&1 || true
  fi
  if [[ -n $CREATED_SG_ID ]]; then
    info "  deleting security group $CREATED_SG_ID"
    if ! remove_security_group "$CREATED_SG_ID"; then warn "  Could not delete $CREATED_SG_ID; delete it by hand."; fi
  fi
  if (( CREATED_KEY )); then
    info "  deleting key pair $KEY_NAME and $KEY_FILE"
    awsx ec2 delete-key-pair --key-name "$KEY_NAME" >/dev/null 2>&1 || true
    rm -f -- "$KEY_FILE"
  fi
}

# ---------------------------------------------------------------- pre-flight
# Read-only checks: nothing is created until all of them pass.

info "telegram-forwarder AWS provisioner"

command -v aws >/dev/null \
  || die "AWS CLI not found in PATH. Install it: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
command -v curl >/dev/null || die "curl not found in PATH."

PROFILES=$(aws configure list-profiles 2>/dev/null || true)
grep -qxF -- "$PROFILE" <<<"$PROFILES" \
  || die "AWS CLI profile '$PROFILE' not found. Set it up with: aws configure --profile $PROFILE"
[[ -n $REGION ]] || REGION=$(aws configure get region --profile "$PROFILE" 2>/dev/null || true)
[[ -n $REGION ]] || REGION="us-east-1"

IDENTITY=$(awsx sts get-caller-identity --query '[Account,Arn]' --output text) \
  || die "AWS credentials for profile '$PROFILE' don't work. Check: aws sts get-caller-identity --profile $PROFILE"
read -r ACCOUNT_ID CALLER_ARN <<<"$IDENTITY"
info "Account: $ACCOUNT_ID   Profile: $PROFILE   Region: $REGION"
info "InstanceType: $INSTANCE_TYPE   NamePrefix: $NAME_PREFIX"
if [[ $CALLER_ARN == *":root" ]]; then
  warn "Profile '$PROFILE' uses ROOT access keys ($CALLER_ARN). An IAM user is safer (see README)."
fi

# Absolute path, so the paths saved in deployment-info.txt work from anywhere
# (-m: may not exist yet; -s: keep symlinks such as /data as typed).
OUTPUT_FOLDER=$(realpath -m -s -- "$OUTPUT_FOLDER")

KEY_NAME="$NAME_PREFIX-key"
SG_NAME="$NAME_PREFIX-sg"
INSTANCE_TAG="$NAME_PREFIX"
KEY_FILE="$OUTPUT_FOLDER/$KEY_NAME.pem"
INFO_FILE="$OUTPUT_FOLDER/deployment-info.txt"

# -------- existing deployment in this folder

if [[ -e $INFO_FILE ]]; then
  if (( ! FORCE )); then
    warn "Found existing $INFO_FILE. Pass --force to tear down and recreate, or delete the file manually."
    die "Aborting to avoid accidental teardown."
  fi
  info "Tearing down the existing deployment first (--force)..."
  "$SCRIPT_DIR/teardown-aws.sh" --info-file "$INFO_FILE" --aws-profile "$PROFILE" --force \
    || die "Teardown of the existing deployment failed, so no new one was created."
fi

[[ ! -e $KEY_FILE ]] || die "$KEY_FILE already exists. Move it aside first."

# Check whether a key pair with this name already exists in AWS (we wouldn't
# be able to download its private key).
if awsx ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  die "AWS already has a key pair named '$KEY_NAME' but the .pem isn't in $OUTPUT_FOLDER. Delete it: aws ec2 delete-key-pair --profile $PROFILE --region $REGION --key-name $KEY_NAME"
fi

VPC_ID=$(aws_ok ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
[[ $VPC_ID == vpc-* ]] \
  || die "No default VPC in $REGION. Create one: aws ec2 create-default-vpc --profile $PROFILE --region $REGION"

EXISTING_SG=$(aws_ok ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$SG_NAME" \
  --query 'SecurityGroups[0].GroupId' --output text)
[[ $EXISTING_SG != sg-* ]] \
  || die "Security group '$SG_NAME' already exists ($EXISTING_SG). Delete it: aws ec2 delete-security-group --profile $PROFILE --region $REGION --group-id $EXISTING_SG"

# Not every AZ offers every instance type (us-east-1e has no t3.micro), and a
# launch without a subnet can land there. Pick the first default subnet whose
# AZ offers $INSTANCE_TYPE.
OFFERED_AZS=$(aws_ok ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters "Name=instance-type,Values=$INSTANCE_TYPE" \
  --query 'InstanceTypeOfferings[].Location' --output text | tr -s '[:space:]' '\n')
SUBNETS=$(aws_ok ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" Name=default-for-az,Values=true \
  --query 'sort_by(Subnets,&AvailabilityZone)[].[AvailabilityZone,SubnetId]' --output text)
AZ=""
SUBNET_ID=""
while read -r row_az row_subnet; do
  if [[ -n $row_az ]] && grep -qxF -- "$row_az" <<<"$OFFERED_AZS"; then
    AZ=$row_az
    SUBNET_ID=$row_subnet
    break
  fi
done <<<"$SUBNETS"
[[ -n $SUBNET_ID ]] || die "No default subnet in $REGION is in an AZ that offers $INSTANCE_TYPE."

# AWS's "latest Amazon Linux 2023" pointer lives in SSM. An IAM user with only
# AmazonEC2FullAccess (what the README suggests) can't read SSM, so fall back
# to searching EC2 images.
if AMI_ID=$(awsx ssm get-parameter \
      --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
      --query Parameter.Value --output text 2>/dev/null) && [[ $AMI_ID == ami-* ]]; then
  :
else
  info "Can't read the SSM AMI pointer (needs ssm:GetParameter); searching EC2 images instead."
  AMI_ID=$(aws_ok ec2 describe-images --owners amazon \
    --filters 'Name=name,Values=al2023-ami-2023.*-x86_64' Name=state,Values=available \
    --query "sort_by(Images,&join('',[CreationDate,Name]))[-1].ImageId" --output text)
fi
[[ $AMI_ID == ami-* ]] || die "Could not find an Amazon Linux 2023 AMI in $REGION."
info "AMI: $AMI_ID   AZ: $AZ ($SUBNET_ID)"

MY_IP=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]') \
  || die "Could not determine your public IP. Check internet connection."
[[ $MY_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "Unexpected public IP '$MY_IP'."

# ---------------------------------------------------------------- create
# From here on, a failure (or Ctrl+C) deletes whatever this run created.

if [[ ! -d $OUTPUT_FOLDER ]]; then
  mkdir -p -- "$OUTPUT_FOLDER"
  ok "Created folder: $OUTPUT_FOLDER"
fi

CREATED_KEY=0
CREATED_SG_ID=""
CREATED_INSTANCE_ID=""
SUCCEEDED=0
WORK_DIR=$(mktemp -d)
on_exit() {
  rm -rf "$WORK_DIR"
  if (( ! SUCCEEDED )); then undo_provisioning; fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# -------- 1. key pair
info "Creating key pair: $KEY_NAME"
PEM=$(aws_ok ec2 create-key-pair --key-name "$KEY_NAME" --query KeyMaterial --output text)
CREATED_KEY=1
(umask 077 && printf '%s\n' "${PEM//$'\r'/}" >"$KEY_FILE")
chmod 400 "$KEY_FILE"   # ssh refuses private keys that other users can read
ok "Saved private key: $KEY_FILE"

# -------- 2. security group, ingress for SSH from this machine
info "Creating security group: $SG_NAME  (SSH from $MY_IP/32)"
CREATED_SG_ID=$(aws_ok ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC_ID" \
  --description "telegram-forwarder SSH access" --query GroupId --output text)
aws_ok ec2 authorize-security-group-ingress --group-id "$CREATED_SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_IP/32" >/dev/null
ok "Security group: $CREATED_SG_ID"

# -------- 3. user-data: install Docker + buildx + compose plugin on first boot
cat >"$WORK_DIR/user-data.sh" <<'USERDATA'
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
USERDATA

# -------- 4. launch
info "Launching $INSTANCE_TYPE instance in $AZ..."
CREATED_INSTANCE_ID=$(aws_ok ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$CREATED_SG_ID" \
  --associate-public-ip-address \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_TAG}]" \
  --user-data "file://$WORK_DIR/user-data.sh" \
  --query 'Instances[0].InstanceId' --output text)
ok "Instance: $CREATED_INSTANCE_ID"

info "Waiting for instance to reach 'running' state..."
aws_ok ec2 wait instance-running --instance-ids "$CREATED_INSTANCE_ID" >/dev/null

PUBLIC_IP=$(aws_ok ec2 describe-instances --instance-ids "$CREATED_INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
[[ $PUBLIC_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "Instance $CREATED_INSTANCE_ID came up without a public IP."
ok "Public IP: $PUBLIC_IP"

# -------- 5. write deployment-info.txt
DEPLOYED_AT=$(date '+%Y-%m-%d %H:%M:%S %:z')
cat >"$INFO_FILE" <<EOF
# Generated by deploy-aws.sh on $DEPLOYED_AT
# Reload these in any new shell via:
#   source "$SCRIPT_DIR/load-deployment.sh" --info-file "$INFO_FILE"

AWS_PROFILE    = $PROFILE
ACCOUNT_ID     = $ACCOUNT_ID
REGION         = $REGION
INSTANCE_ID    = $CREATED_INSTANCE_ID
INSTANCE_TYPE  = $INSTANCE_TYPE
KEY_NAME       = $KEY_NAME
KEY_PATH       = $KEY_FILE
SG_NAME        = $SG_NAME
SG_ID          = $CREATED_SG_ID
AMI_ID         = $AMI_ID
INSTANCE_TAG   = $INSTANCE_TAG
AZ             = $AZ
DEPLOYED_AT    = $DEPLOYED_AT
DEPLOYED_FROM_IP = $MY_IP

# Dynamic - re-query before use; changes when instance stops/starts
PUBLIC_IP      = $PUBLIC_IP
EOF
ok "Saved deployment info: $INFO_FILE"
SUCCEEDED=1

# -------- done

echo
ok "Deployment complete."
echo
echo "Wait ~60 seconds for Docker to finish installing on the instance, then SSH in:"
echo
echo "  ssh -i \"$KEY_FILE\" ec2-user@$PUBLIC_IP"
echo
echo "Or use the load-deployment.sh helper to set env vars and connect:"
echo
echo "  source \"$SCRIPT_DIR/load-deployment.sh\" --info-file \"$INFO_FILE\""
echo "  Connect-Forwarder"
