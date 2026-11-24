#!/usr/bin/env bash
#
# load-deployment.sh: reload deployment info from a deployment-info.txt and
# provide connect/start/stop helpers. Bash port of load-deployment.ps1.
#
# SOURCE this file (don't run it) so the env vars and functions stay in your
# shell:
#   source deploy/load-deployment.sh --info-file /path/to/deployment-info.txt
#   Connect-Forwarder
#
# Exports AWS_PROFILE, REGION, INSTANCE_ID, KEY_PATH, SG_ID, ... from the
# info file written by deploy-aws.sh / deploy-aws.ps1, then defines:
#
#   Connect-Forwarder        SSH into the instance (re-queries current public IP)
#   Start-Forwarder          Start the instance (if stopped) and SSH in
#   Stop-Forwarder           Stop the instance (preserves disk + sessions)
#   Update-ForwarderIngress  Point the SSH rule at your current public IP
#                            (run if SSH times out); old IPs are revoked
#   Show-Forwarder           Print all loaded env vars
#
# The helpers use the deployment's AWS_PROFILE. Because AWS_PROFILE is
# exported, plain `aws` commands in this shell use that profile too.
#
# Options (the PowerShell names, e.g. -InfoFile, work too):
#   --info-file FILE     deployment-info.txt (required)
#   --aws-profile NAME   profile for info files without an AWS_PROFILE line.
#                        Default: tgf
#   -h, --help           show this help
#
# This file runs inside your shell: it never exits it and doesn't change
# your shell options.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "[x] Source this file instead of running it, so the helpers stay in your shell:" >&2
  echo "      source $0 --info-file <deployment-info.txt>" >&2
  exit 1
fi

_fwd_say() {   # _fwd_say <color> <tag> <fd> <message...>; colors only on a terminal
  local color=$1 tag=$2 fd=$3
  shift 3
  if [[ -t $fd ]]; then printf '\033[%sm[%s] %s\033[0m\n' "$color" "$tag" "$*" >&"$fd"
  else printf '[%s] %s\n' "$tag" "$*" >&"$fd"; fi
}

_fwd_load() {
  local info_file="" aws_profile="tgf" line pair have_region="" have_instance="" have_keypath="" have_profile=""
  local -a pairs=()
  while (($#)); do
    case "$1" in
      --info-file|-InfoFile)     info_file=${2:-}; shift ;;
      --aws-profile|-AwsProfile) aws_profile=${2:-}; shift ;;
      -h|--help)
        awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "${BASH_SOURCE[0]}"
        return 2 ;;
      *) _fwd_say 31 x 2 "unknown argument '$1' (try --help)"; return 1 ;;
    esac
    shift
  done
  if [[ -z $info_file ]]; then _fwd_say 31 x 2 "--info-file is required (try --help)"; return 1; fi
  if [[ ! -f $info_file ]]; then _fwd_say 31 x 2 "$info_file not found."; return 1; fi

  # Parse "KEY = VALUE" lines, ignoring comments and blanks (CRLF-tolerant,
  # so files written by deploy-aws.ps1 on Windows work too).
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'}
    if [[ $line =~ ^[[:space:]]*([[:upper:]_]+)[[:space:]]*=[[:space:]]*(.*[^[:space:]])[[:space:]]*$ ]]; then
      pairs+=("${BASH_REMATCH[1]}=${BASH_REMATCH[2]}")
      case ${BASH_REMATCH[1]} in
        REGION)      have_region=1 ;;
        INSTANCE_ID) have_instance=1 ;;
        KEY_PATH)    have_keypath=1 ;;
        AWS_PROFILE) have_profile=1 ;;
      esac
    fi
  done <"$info_file"

  if [[ -z $have_region || -z $have_instance || -z $have_keypath ]]; then
    _fwd_say 31 x 2 "Info file is missing required keys (REGION / INSTANCE_ID / KEY_PATH)."
    return 1
  fi
  for pair in "${pairs[@]}"; do export "$pair"; done
  if [[ -z $have_profile ]]; then export AWS_PROFILE="$aws_profile"; fi   # older info files

  _fwd_say 32 + 1 "Loaded deployment from $info_file"
  echo "    AWS_PROFILE = $AWS_PROFILE"
  echo "    INSTANCE_ID = $INSTANCE_ID"
  echo "    REGION      = $REGION"
  echo "    KEY_PATH    = $KEY_PATH"
  echo
}

_fwd_load "$@"
case $? in
  0) unset -f _fwd_load ;;
  2) unset -f _fwd_load; return 0 ;;   # --help
  *) unset -f _fwd_load; return 1 ;;
esac

# ---------------------------------------------------------------- functions

# aws with the deployment's profile + region.
_fwd_aws() { aws --profile "$AWS_PROFILE" --region "$REGION" "$@"; }

Show-Forwarder() {
  local name
  printf '%-17s %s\n' Name Value ---- -----
  for name in ACCOUNT_ID AMI_ID AWS_PROFILE AZ DEPLOYED_AT DEPLOYED_FROM_IP INSTANCE_ID INSTANCE_TAG \
              INSTANCE_TYPE KEY_NAME KEY_PATH PUBLIC_IP REGION SG_ID SG_NAME; do
    if [[ -n ${!name:-} ]]; then printf '%-17s %s\n' "$name" "${!name}"; fi
  done
}

_fwd_public_ip() {
  local ip
  ip=$(_fwd_aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null) || return 1
  if [[ -z $ip || $ip == None ]]; then return 1; fi
  echo "$ip"
}

_fwd_state() {
  local state
  if ! state=$(_fwd_aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
      --query 'Reservations[0].Instances[0].State.Name' --output text); then
    echo unknown   # aws already printed why
    return
  fi
  echo "$state"
}

Connect-Forwarder() {
  local state ip
  state=$(_fwd_state)
  if [[ $state != running ]]; then
    _fwd_say 33 '!' 1 "Instance is '$state'. Start it first with: Start-Forwarder"
    return 1
  fi
  if ! ip=$(_fwd_public_ip); then
    _fwd_say 31 x 2 "No public IP found on instance."
    return 1
  fi
  export PUBLIC_IP=$ip
  _fwd_say 36 '*' 1 "Connecting to ec2-user@$ip..."
  ssh -i "$KEY_PATH" "ec2-user@$ip"
}

Start-Forwarder() {
  local state
  state=$(_fwd_state)
  if [[ $state == running ]]; then
    _fwd_say 36 '*' 1 "Instance already running."
  else
    _fwd_say 36 '*' 1 "Starting instance $INSTANCE_ID..."
    _fwd_aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null || return 1
    _fwd_aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" || return 1
  fi
  Connect-Forwarder
}

Stop-Forwarder() {
  _fwd_say 36 '*' 1 "Stopping instance $INSTANCE_ID..."
  _fwd_aws ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null || return 1
  _fwd_aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" || return 1
  _fwd_say 32 + 1 "Stopped. (Disk + sessions preserved.)"
}

Update-ForwarderIngress() {
  # Useful when your laptop's public IP has changed and SSH times out. Old
  # IPs are revoked: a mobile IP you no longer hold can go to someone else.
  local my_ip new_cidr existing cidr
  my_ip=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')
  if [[ ! $my_ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    _fwd_say 31 x 2 "Could not determine your public IP (got '$my_ip')."
    return 1
  fi
  new_cidr="$my_ip/32"

  existing=$(_fwd_aws ec2 describe-security-groups --group-ids "$SG_ID" \
    --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`].IpRanges[].CidrIp' --output text) || return 1
  existing=$(tr -s '[:space:]' '\n' <<<"$existing")

  # Add the new rule before revoking old ones, so a failure can't lock you out.
  if grep -qxF -- "$new_cidr" <<<"$existing"; then
    _fwd_say 36 '*' 1 "Rule for $new_cidr already exists."
  else
    _fwd_say 36 '*' 1 "Authorizing SSH from $new_cidr on $SG_ID..."
    _fwd_aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --protocol tcp --port 22 --cidr "$new_cidr" >/dev/null || return 1
    _fwd_say 32 + 1 "Added."
  fi
  for cidr in $existing; do
    if [[ $cidr == "$new_cidr" ]]; then continue; fi
    if _fwd_aws ec2 revoke-security-group-ingress --group-id "$SG_ID" \
        --protocol tcp --port 22 --cidr "$cidr" >/dev/null; then
      _fwd_say 32 + 1 "Revoked old rule $cidr."
    else
      _fwd_say 31 x 2 "Could not revoke $cidr."
    fi
  done
}

_fwd_say 36 '*' 1 "Available commands:"
echo "  Show-Forwarder          show all loaded env vars"
echo "  Connect-Forwarder       SSH into the running instance"
echo "  Start-Forwarder         start instance and SSH in"
echo "  Stop-Forwarder          stop the instance (keeps disk)"
echo "  Update-ForwarderIngress point SSH access at your current IP"
