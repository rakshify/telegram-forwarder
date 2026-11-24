#!/usr/bin/env bash
#
# teardown-aws.sh: tear down the AWS resources created by deploy-aws.sh (or
# deploy-aws.ps1). Bash port of teardown-aws.ps1, with the same behavior.
#
# Reads the same deployment-info.txt that load-deployment.sh reads,
# terminates the EC2 instance, and removes the security group + key pair.
# Unless --keep-local-files is set, it then deletes the .pem and the info
# file, but only if every AWS step succeeded, so a failed teardown can be
# re-run.
#
# Uses the AWS_PROFILE recorded in the info file (--aws-profile, default
# "tgf", only applies to info files that don't record one), and refuses to
# run if that profile belongs to a different account than the deployment.
#
# DESTRUCTIVE: the EC2 instance's disk (and your Telegram sessions) is
# permanently destroyed by this script. Use with care.
#
# Usage:
#   deploy/teardown-aws.sh --info-file FILE [options]
#
# Options (the PowerShell names, e.g. -InfoFile, work too):
#   --info-file FILE      deployment-info.txt written by deploy-aws.sh/.ps1 (required)
#   --aws-profile NAME    profile for info files without an AWS_PROFILE line.
#                         Default: tgf
#   --keep-local-files    leave the .pem and deployment-info.txt on disk
#                         (only the AWS resources are torn down)
#   --force               skip the "type yes" confirmation
#                         (deploy-aws.sh --force uses this)
#   -h, --help            show this help
#
# Example:
#   deploy/teardown-aws.sh --info-file /data/Documents/tg-forwarder-aws/deployment-info.txt

set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

say() {   # say <color> <tag> <fd> <message...>; colors only on a terminal
  local color=$1 tag=$2 fd=$3
  shift 3
  if [[ -t $fd ]]; then printf '\033[%sm[%s] %s\033[0m\n' "$color" "$tag" "$*" >&"$fd"
  else printf '[%s] %s\n' "$tag" "$*" >&"$fd"; fi
}
info() { say 36 '*' 1 "$@"; }
ok()   { say 32 '+' 1 "$@"; }
die()  { say 31 x 2 "$@"; exit 1; }
yellow() { if [[ -t 1 ]]; then printf '\033[33m%s\033[0m\n' "$*"; else printf '%s\n' "$*"; fi; }
usage() { awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$SCRIPT_PATH"; }
value() { [[ $# -ge 2 && -n $2 ]] || die "$1 needs a value (try --help)"; printf '%s' "$2"; }

INFO_FILE=""
PROFILE="tgf"
KEEP_LOCAL_FILES=0
FORCE=0
while (($#)); do
  case "$1" in
    --info-file|-InfoFile)               INFO_FILE=$(value "$@"); shift ;;
    --aws-profile|-AwsProfile)           PROFILE=$(value "$@"); shift ;;
    --keep-local-files|-KeepLocalFiles)  KEEP_LOCAL_FILES=1 ;;
    --force|-Force)                      FORCE=1 ;;
    -h|--help)                           usage; exit 0 ;;
    *)                                   die "unknown argument '$1' (try --help)" ;;
  esac
  shift
done
[[ -n $INFO_FILE ]] || die "--info-file is required (try --help)"

awsx() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

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

[[ -f $INFO_FILE ]] || die "$INFO_FILE not found."

# Parse "KEY = VALUE" lines into KV_<KEY> variables (CRLF-tolerant, so files
# written by deploy-aws.ps1 on Windows work too).
unset "${!KV_@}"
while IFS= read -r line || [[ -n $line ]]; do
  line=${line%$'\r'}
  if [[ $line =~ ^[[:space:]]*([[:upper:]_]+)[[:space:]]*=[[:space:]]*(.*[^[:space:]])[[:space:]]*$ ]]; then
    printf -v "KV_${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
  fi
done <"$INFO_FILE"

# Anything else (e.g. setup-ec2.sh's env-vars.txt) is refused before touching
# AWS or deleting any local file.
if [[ -z ${KV_REGION:-} || -z ${KV_INSTANCE_ID:-}${KV_SG_ID:-}${KV_KEY_NAME:-} ]]; then
  die "$INFO_FILE isn't a deployment-info.txt from deploy-aws.sh/.ps1 (needs REGION plus INSTANCE_ID / SG_ID / KEY_NAME). Nothing was changed."
fi
REGION=$KV_REGION
if [[ -n ${KV_AWS_PROFILE:-} ]]; then PROFILE=$KV_AWS_PROFILE; fi

command -v aws >/dev/null || die "AWS CLI not found in PATH."
ACCOUNT_ID=$(awsx sts get-caller-identity --query Account --output text) \
  || die "AWS credentials for profile '$PROFILE' don't work. Nothing was changed."
if [[ -n ${KV_ACCOUNT_ID:-} && $KV_ACCOUNT_ID != "$ACCOUNT_ID" ]]; then
  die "Profile '$PROFILE' is account $ACCOUNT_ID, but this deployment is in account $KV_ACCOUNT_ID. Nothing was changed."
fi

echo
yellow "About to TEAR DOWN (account $ACCOUNT_ID, profile '$PROFILE'):"
echo "  Region       : $REGION"
echo "  Instance     : ${KV_INSTANCE_ID:-}"
echo "  Security grp : ${KV_SG_ID:-}"
echo "  Key pair     : ${KV_KEY_NAME:-}"
if (( ! KEEP_LOCAL_FILES )); then
  yellow "  Local files  : ${KV_KEY_PATH:-}"
  yellow "                 $INFO_FILE"
fi
echo
if (( ! FORCE )); then
  echo "This is DESTRUCTIVE. Telegram sessions and forwarder state on the instance will be lost."
  read -r -p "Type 'yes' to proceed: " confirm || confirm=""
  if [[ $confirm != "yes" ]]; then
    echo "Aborted."
    exit 0
  fi
fi

# The security group can only go once the instance is gone, so stop here if
# termination fails.
if [[ -n ${KV_INSTANCE_ID:-} ]]; then
  info "Terminating instance $KV_INSTANCE_ID..."
  if err=$(awsx ec2 terminate-instances --instance-ids "$KV_INSTANCE_ID" 2>&1 >/dev/null); then
    if ! err=$(awsx ec2 wait instance-terminated --instance-ids "$KV_INSTANCE_ID" 2>&1 >/dev/null); then
      printf '%s\n' "$err" >&2
      die "Instance didn't reach 'terminated' yet. Nothing else was changed; re-run in a minute."
    fi
    ok "Instance terminated."
  elif [[ $err == *InvalidInstanceID.NotFound* ]]; then
    ok "Instance already gone."
  else
    printf '%s\n' "$err" >&2
    die "Could not terminate the instance. Nothing else was changed."
  fi
fi

failed=0
if [[ -n ${KV_SG_ID:-} ]]; then
  info "Deleting security group $KV_SG_ID..."
  if remove_security_group "$KV_SG_ID"; then ok "Security group deleted."; else failed=1; fi
fi
if [[ -n ${KV_KEY_NAME:-} ]]; then
  info "Deleting key pair $KV_KEY_NAME..."
  if err=$(awsx ec2 delete-key-pair --key-name "$KV_KEY_NAME" 2>&1 >/dev/null); then
    ok "Key pair deleted."
  else
    printf '%s\n' "$err" >&2
    failed=1
  fi
fi
if (( failed )); then
  die "Some AWS resources could not be deleted (see above). Local files were kept, so you can re-run this."
fi

if (( ! KEEP_LOCAL_FILES )); then
  if [[ -n ${KV_KEY_PATH:-} && -e $KV_KEY_PATH ]]; then rm -f -- "$KV_KEY_PATH"; fi
  rm -f -- "$INFO_FILE"
  ok "Local files removed."
fi

echo
ok "Teardown complete."
