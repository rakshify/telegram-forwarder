#!/usr/bin/env bash
#
# setup-ec2.sh: provision the "tgf-ec2" EC2 instance for telegram-forwarder
# with the "tgf" AWS CLI profile, and save everything needed to use it in
# the output folder you pass (same layout as /data/Documents/scribe-ec2).
#
# Creates, in the profile's region:
#   key pair        tgf-ec2-key   private key saved as tgf-ec2-key.pem (chmod 400)
#   security group  tgf-ec2-sg    inbound SSH (22) from this machine's public IP only
#   instance        tgf-ec2       Ubuntu 24.04 LTS, t3.micro, default VPC, public IP,
#                                 16 GB encrypted gp3 root disk, IMDSv2 only.
#                                 First boot installs Docker Engine + compose +
#                                 buildx and adds 2 GB of swap.
#
# Writes to the output folder:
#   env-vars.txt        instance details, SSH command, stop/start/teardown commands
#   tgf-ec2-key.pem     SSH private key
#   update-ssh-ip.sh    re-points the SSH rule at your current public IP
#
# Usage:
#   deploy/setup-ec2.sh [-y] <output-folder>
#
#   <output-folder>  where the .pem, env-vars.txt and update-ssh-ip.sh go;
#                    created if missing (e.g. /data/Documents/tgf-ec2)
#   -y, --yes        create without asking for confirmation
#   -h, --help       show this help
#
# Optional overrides (environment variables):
#   PROFILE=tgf  NAME=tgf-ec2  REGION=<profile region>
#   INSTANCE_TYPE=t3.micro (x86_64 types only)  UBUNTU_VERSION=24.04  VOLUME_GB=16
#
# If a step fails, the AWS resources this run already created are deleted
# again, so you can fix the problem and re-run.

set -euo pipefail

PROFILE="${PROFILE:-tgf}"
NAME="${NAME:-tgf-ec2}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
UBUNTU_VERSION="${UBUNTU_VERSION:-24.04}"
VOLUME_GB="${VOLUME_GB:-16}"

KEY_NAME="$NAME-key"
SG_NAME="$NAME-sg"
OS_USER="ubuntu"
CANONICAL_OWNER_ID="099720109477"   # Canonical's AWS account (official Ubuntu AMIs)
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

info() { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }
usage() { awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$SCRIPT_PATH"; }

ASSUME_YES=0
OUT_DIR=""
for arg in "$@"; do
  case "$arg" in
    -y|--yes)  ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    -*)        die "unknown option '$arg' (try --help)" ;;
    *)         [[ -z $OUT_DIR ]] || die "give exactly one output folder (got '$OUT_DIR' and '$arg')"
               OUT_DIR=$arg ;;
  esac
done
[[ -n $OUT_DIR ]] || die "missing output folder. Usage: $0 [-y] <output-folder>  (see --help)"

# Absolute path, so the SSH command and paths saved in env-vars.txt work from
# any directory. -m: the folder doesn't have to exist yet. -s: keep symlinks
# as typed (e.g. /data/... stays /data/..., not the link's target).
OUT_DIR=$(realpath -m -s -- "$OUT_DIR")
PEM="$OUT_DIR/$KEY_NAME.pem"
INFO="$OUT_DIR/env-vars.txt"
HELPER="$OUT_DIR/update-ssh-ip.sh"

# --------------------------------------------------------------- pre-flight
# Everything in this section is read-only: nothing is created until you confirm.

for tool in aws curl; do
  command -v "$tool" >/dev/null || die "'$tool' is not installed"
done

PROFILES=$(aws configure list-profiles 2>/dev/null || true)
if ! grep -qxF "$PROFILE" <<<"$PROFILES"; then
  die "AWS profile '$PROFILE' is not configured (run: aws configure --profile $PROFILE)"
fi
REGION="${REGION:-$(aws configure get region --profile "$PROFILE" 2>/dev/null || true)}"
REGION="${REGION:-us-east-1}"

# Always pass --profile explicitly. An explicit profile makes the CLI ignore
# AWS_PROFILE / AWS_ACCESS_KEY_ID exported in the calling shell, so this
# script can't silently end up in another account.
awsx() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

for f in "$PEM" "$INFO" "$HELPER"; do
  if [[ -e $f ]]; then
    die "$f already exists: $OUT_DIR already holds a deployment. Move it aside or pass another folder."
  fi
done

info "Checking credentials (profile '$PROFILE', region $REGION)..."
IDENTITY=$(awsx sts get-caller-identity --query '[Account,Arn]' --output text) \
  || die "AWS credentials for profile '$PROFILE' don't work"
read -r ACCOUNT_ID CALLER_ARN <<<"$IDENTITY"
if [[ $CALLER_ARN == *":root" ]]; then
  warn "profile '$PROFILE' uses ROOT access keys ($CALLER_ARN). An IAM user is safer."
fi

if awsx ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  die "key pair '$KEY_NAME' already exists in $REGION, and AWS can't hand out its private key again. Delete it or set NAME."
fi

VPC_ID=$(awsx ec2 describe-vpcs --filters Name=is-default,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
if [[ $VPC_ID != vpc-* ]]; then
  die "no default VPC in $REGION (create one: aws ec2 create-default-vpc --profile $PROFILE --region $REGION)"
fi

EXISTING_SG=$(awsx ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$SG_NAME" \
  --query 'SecurityGroups[0].GroupId' --output text)
if [[ $EXISTING_SG == sg-* ]]; then
  die "security group '$SG_NAME' already exists ($EXISTING_SG). Delete it or set NAME."
fi

# Not every AZ offers every instance type (us-east-1e has no t3.micro), so pick
# the first default subnet whose AZ offers $INSTANCE_TYPE.
OFFERED_AZS=$(awsx ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters "Name=instance-type,Values=$INSTANCE_TYPE" \
  --query 'InstanceTypeOfferings[].Location' --output text | tr -s '[:space:]' '\n')
SUBNETS=$(awsx ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" Name=default-for-az,Values=true \
  --query 'sort_by(Subnets,&AvailabilityZone)[].[AvailabilityZone,SubnetId]' --output text)
AZ=""; SUBNET_ID=""
while read -r az subnet; do
  if [[ -n $az ]] && grep -qxF "$az" <<<"$OFFERED_AZS"; then
    AZ=$az; SUBNET_ID=$subnet; break
  fi
done <<<"$SUBNETS"
[[ -n $SUBNET_ID ]] || die "no default subnet in $REGION sits in an AZ that offers $INSTANCE_TYPE"

AMI_INFO=$(awsx ec2 describe-images --owners "$CANONICAL_OWNER_ID" \
  --filters "Name=name,Values=ubuntu/images/*ubuntu-*-$UBUNTU_VERSION-amd64-server-*" \
            Name=state,Values=available Name=architecture,Values=x86_64 \
            Name=root-device-type,Values=ebs Name=virtualization-type,Values=hvm \
  --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name,RootDeviceName]' --output text)
read -r AMI_ID AMI_NAME ROOT_DEVICE <<<"$AMI_INFO"
[[ $AMI_ID == ami-* ]] || die "no official Ubuntu $UBUNTU_VERSION amd64 AMI found in $REGION"
UBUNTU_LABEL="Ubuntu $UBUNTU_VERSION"
if [[ $UBUNTU_VERSION =~ ^[0-9]*[02468]\.04$ ]]; then UBUNTU_LABEL+=" LTS"; fi

MY_IP=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]') \
  || die "couldn't reach checkip.amazonaws.com to find this machine's public IP"
[[ $MY_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "unexpected public IP '$MY_IP'"

# Burstable types (t2/t3/t3a/t4g) default to "unlimited" CPU credits, which
# bills for sustained CPU above baseline. "standard" throttles instead.
CREDIT_ARGS=()
CREDIT_NOTE=""
if [[ $INSTANCE_TYPE =~ ^t[0-9] ]]; then
  CREDIT_ARGS=(--credit-specification CpuCredits=standard)
  CREDIT_NOTE=" (CPU credits: standard, so no surprise burst charges)"
fi

echo
echo "About to create in AWS account $ACCOUNT_ID (profile '$PROFILE', region $REGION):"
printf '  %-15s %-13s %s\n' "key pair"       "$KEY_NAME" "private key -> $PEM"
printf '  %-15s %-13s %s\n' "security group" "$SG_NAME"  "inbound SSH from $MY_IP/32 only"
printf '  %-15s %-13s %s\n' "instance"       "$NAME"     "$INSTANCE_TYPE, $UBUNTU_LABEL ($AMI_ID)"
printf '  %-15s %-13s %s\n' ""               ""          "${VOLUME_GB} GB gp3, $AZ ($SUBNET_ID)"
echo "Details will be saved in $OUT_DIR"
echo
if (( ! ASSUME_YES )); then
  read -r -p "Proceed? [y/N] " answer || answer=""
  if [[ ! $answer =~ ^[Yy]([Ee][Ss])?$ ]]; then
    echo "Aborted. Nothing was created."
    exit 0
  fi
fi

# --------------------------------------------------------------- rollback
# Until the details are saved, any failure (or Ctrl+C) removes what this
# run created, so a re-run starts clean.

CREATED_DIR=0; CREATED_KEY=0; SG_ID=""; INSTANCE_ID=""; COMMITTED=0
WORK_DIR=$(mktemp -d)

on_exit() {
  local rc=$?
  rm -rf "$WORK_DIR"
  if (( rc == 0 || COMMITTED )); then return; fi
  echo >&2
  echo "Setup failed (exit $rc). Removing what this run created..." >&2
  if [[ -n $INSTANCE_ID ]]; then
    echo "  terminating instance $INSTANCE_ID (takes a minute)" >&2
    awsx ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null 2>&1 || true
    awsx ec2 wait instance-terminated --instance-ids "$INSTANCE_ID" >/dev/null 2>&1 || true
  fi
  if [[ -n $SG_ID ]]; then
    echo "  deleting security group $SG_ID" >&2
    awsx ec2 delete-security-group --group-id "$SG_ID" >/dev/null 2>&1 \
      || echo "  could not delete $SG_ID; remove it by hand" >&2
  fi
  if (( CREATED_KEY )); then
    echo "  deleting key pair $KEY_NAME and $PEM" >&2
    awsx ec2 delete-key-pair --key-name "$KEY_NAME" >/dev/null 2>&1 || true
    rm -f "$PEM"
  fi
  if (( CREATED_DIR )); then rmdir "$OUT_DIR" 2>/dev/null || true; fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --------------------------------------------------------------- create

umask 077   # the output folder holds a private key: keep it owner-only
if [[ ! -d $OUT_DIR ]]; then
  mkdir -p "$OUT_DIR"
  CREATED_DIR=1
fi

info "Creating key pair $KEY_NAME..."
awsx ec2 create-key-pair --key-name "$KEY_NAME" --key-type ed25519 --key-format pem \
  --tag-specifications "ResourceType=key-pair,Tags=[{Key=Name,Value=$KEY_NAME}]" \
  --query KeyMaterial --output text >"$WORK_DIR/key.pem"
CREATED_KEY=1
mv "$WORK_DIR/key.pem" "$PEM"
chmod 400 "$PEM"

info "Creating security group $SG_NAME (SSH from $MY_IP/32)..."
SG_ID=$(awsx ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC_ID" \
  --description "$NAME - SSH from owner IP only" \
  --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=$SG_NAME}]" \
  --query GroupId --output text)
awsx ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_IP/32" >/dev/null

# First-boot script, run once as root by cloud-init.
cat >"$WORK_DIR/user-data.sh" <<'USERDATA'
#!/bin/bash
# tgf-ec2 first-boot setup (cloud-init). Log: /var/log/cloud-init-output.log
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -y -o DPkg::Lock::Timeout=600)   # wait out unattended-upgrades on first boot

# 2 GB swap: t3.micro has 1 GB RAM, and `docker compose build` can run out without it.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Docker Engine + compose v2 + buildx from Docker's official apt repository.
"${APT[@]}" update
"${APT[@]}" install ca-certificates curl git unzip
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
"${APT[@]}" update
"${APT[@]}" install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod -aG docker ubuntu
USERDATA

info "Launching $INSTANCE_TYPE instance $NAME in $AZ..."
INSTANCE_ID=$(awsx ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$SG_ID" \
  --associate-public-ip-address \
  --block-device-mappings "DeviceName=$ROOT_DEVICE,Ebs={VolumeSize=$VOLUME_GB,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}" \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  ${CREDIT_ARGS[@]+"${CREDIT_ARGS[@]}"} \
  --user-data "file://$WORK_DIR/user-data.sh" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
                       "ResourceType=volume,Tags=[{Key=Name,Value=$NAME-root}]" \
  --query 'Instances[0].InstanceId' --output text)

info "Waiting for $INSTANCE_ID to reach 'running'..."
awsx ec2 wait instance-running --instance-ids "$INSTANCE_ID"
DETAILS=$(awsx ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].[PublicIpAddress,PrivateIpAddress,LaunchTime,BlockDeviceMappings[0].Ebs.VolumeId]' \
  --output text)
read -r PUBLIC_IP PRIVATE_IP LAUNCH_TIME VOLUME_ID <<<"$DETAILS"
[[ $PUBLIC_IP =~ ^[0-9.]+$ ]] || die "instance $INSTANCE_ID came up without a public IP"

# --------------------------------------------------------------- save details

AWS_FLAGS="--profile $PROFILE --region $REGION"
SSH_CMD="ssh -i $PEM $OS_USER@$PUBLIC_IP"

cat >"$INFO" <<EOF
=========================================
 $NAME — AWS EC2 Instance Details
=========================================
Created: $(date +%F)
Created by: $SCRIPT_PATH
AWS Profile used: $PROFILE
AWS Account ID: $ACCOUNT_ID
Region: $REGION
Availability Zone: $AZ

--- Instance ---
Instance ID:        $INSTANCE_ID
Instance Type:      $INSTANCE_TYPE$CREDIT_NOTE
AMI ID:             $AMI_ID
AMI Name:           $AMI_NAME ($UBUNTU_LABEL)
Default OS user:    $OS_USER
Public IP:          $PUBLIC_IP   (changes if the instance is stopped and started)
Private IP:         $PRIVATE_IP
VPC ID:             $VPC_ID
Subnet ID:          $SUBNET_ID ($AZ, default subnet)
Root volume:        $VOLUME_ID ($VOLUME_GB GB gp3, encrypted, deleted on termination)
Instance metadata:  IMDSv2 only
Launch Time (UTC):  $LAUNCH_TIME

--- First-boot setup (cloud-init, runs once) ---
Installs Docker Engine + compose v2 + buildx (Docker's official apt repo),
git and unzip, and adds a 2 GB swapfile (t3.micro has only 1 GB RAM).
User '$OS_USER' is in the docker group, so docker works without sudo (log out
and back in if you connected before setup finished).
Wait for / check it:  $SSH_CMD 'cloud-init status --wait'
Log on the instance:  /var/log/cloud-init-output.log

--- Security Group ---
Security Group Name: $SG_NAME
Security Group ID:   $SG_ID
Inbound rules:
  - TCP 22 (SSH) from <your current public IP>/32 only (recorded IP at creation: $MY_IP/32)
Outbound rules:
  - All traffic allowed (AWS default)

NOTE: Your inbound rule is locked to a single IP. When your public IP
changes (mobile/cellular internet, another network), SSH will time out.
Run update-ssh-ip.sh (in this directory): it detects your current public
IP and re-points the security group rule at it.

--- Key Pair ---
Key Pair Name:  $KEY_NAME (ed25519)
Pem file path:  $PEM
Permissions:    400 (owner read-only, required by ssh)

--- SSH Command ---
$SSH_CMD

The public IP changes after a stop/start. Look up the current one with:
aws ec2 describe-instances --instance-ids $INSTANCE_ID $AWS_FLAGS --query 'Reservations[0].Instances[0].PublicIpAddress' --output text

--- Helper script ---
Path: $HELPER
Purpose: Authorizes the $SG_NAME security group for your current public
IP and revokes the previous one. Run this first if SSH times out.
Usage:
  $HELPER

--- Stop / start (disk and data are kept; no compute charge while stopped) ---
aws ec2 stop-instances  --instance-ids $INSTANCE_ID $AWS_FLAGS
aws ec2 start-instances --instance-ids $INSTANCE_ID $AWS_FLAGS

--- Teardown (when no longer needed) ---
aws ec2 terminate-instances --instance-ids $INSTANCE_ID $AWS_FLAGS
aws ec2 delete-security-group --group-id $SG_ID $AWS_FLAGS   # after instance terminates
aws ec2 delete-key-pair --key-name $KEY_NAME $AWS_FLAGS
EOF

{
  echo '#!/usr/bin/env bash'
  echo "# Updates the $SG_NAME security group so SSH (port 22) is allowed"
  echo "# only from whatever public IP this machine currently has."
  echo "# Run this any time your public IP changes and SSH stops working."
  echo "# Generated by $SCRIPT_PATH"
  echo 'set -euo pipefail'
  echo
  printf 'PROFILE=%q\nREGION=%q\nSG_ID=%q\n' "$PROFILE" "$REGION" "$SG_ID"
  cat <<'HELPER_BODY'

aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

NEW_IP=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')
if [[ ! $NEW_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "Could not determine this machine's public IP (got '$NEW_IP')." >&2
  exit 1
fi
NEW_CIDR="$NEW_IP/32"
echo "Current public IP: $NEW_IP"

EXISTING_CIDRS=$(aws_ ec2 describe-security-groups --group-ids "$SG_ID" \
  --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`].IpRanges[].CidrIp' \
  --output text | tr -s '[:space:]' '\n')

# Add the new rule before revoking the old ones, so a failure can't lock you out.
if grep -qxF "$NEW_CIDR" <<<"$EXISTING_CIDRS"; then
  echo "Rule for $NEW_CIDR already present. Nothing to add."
else
  echo "Authorizing new rule: $NEW_CIDR"
  aws_ ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$NEW_CIDR" >/dev/null
fi

for CIDR in $EXISTING_CIDRS; do
  if [[ $CIDR != "$NEW_CIDR" ]]; then
    echo "Revoking old rule: $CIDR"
    aws_ ec2 revoke-security-group-ingress \
      --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$CIDR" >/dev/null
  fi
done

echo "Done. SSH is now allowed from $NEW_CIDR only."
HELPER_BODY
} >"$HELPER"
chmod 700 "$HELPER"

COMMITTED=1   # details are saved; from here on nothing gets rolled back
info "Saved $INFO, $HELPER and $PEM"

# --------------------------------------------------------------- readiness (best effort)

if command -v ssh >/dev/null; then
  info "Waiting for SSH, then for first-boot setup (Docker install); usually 2-4 minutes..."
  SSH=(ssh -i "$PEM" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new
       -o LogLevel=ERROR -o ServerAliveInterval=15)
  reachable=0
  for ((i = 0; i < 30; i++)); do
    if "${SSH[@]}" "$OS_USER@$PUBLIC_IP" true 2>/dev/null; then reachable=1; break; fi
    sleep 10
  done
  if (( reachable )); then
    if "${SSH[@]}" "$OS_USER@$PUBLIC_IP" \
         'timeout 900 cloud-init status --wait >/dev/null; cloud-init status; docker --version && docker compose version'; then
      info "Instance is ready."
    else
      warn "first-boot setup didn't finish cleanly. Check /var/log/cloud-init-output.log on the instance."
    fi
  else
    warn "SSH not reachable after 5 minutes. The instance may still be booting; try the SSH command below shortly."
  fi
fi

echo
echo "Done. $NAME is running in $REGION ($AZ)."
echo "  SSH:      $SSH_CMD"
echo "  Details:  $INFO"
echo "  New IP?   $HELPER"
