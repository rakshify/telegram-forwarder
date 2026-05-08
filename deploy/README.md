# AWS deployment guide

End-to-end runbook for deploying telegram-forwarder on a single AWS EC2 instance from a Windows laptop. Two scripts in this folder do most of the work; this README sequences the human steps.

```
deploy/
├── README.md                 ← you are here
├── deploy-aws.ps1            ← provisions EC2 + Docker, saves keys/info
├── load-deployment.ps1       ← reloads info into a new shell + helpers
└── teardown-aws.ps1          ← destroys everything (use with care)
```

---

## Prerequisites

Run these once. Skip steps you've already done.

### 1. Install the AWS CLI for Windows

Download and run <https://awscli.amazonaws.com/AWSCLIV2.msi>. After install, in PowerShell:

```powershell
aws --version       # should print aws-cli/2.x
```

### 2. Configure AWS credentials

You need an Access Key ID and Secret Access Key for an IAM user that can create EC2 instances. **Do not use root credentials.**

In the AWS console: IAM → Users → create user (e.g. `cli-admin`), attach the `AmazonEC2FullAccess` policy. Then on the user's "Security credentials" tab → **Create access key** → choose "Command Line Interface (CLI)" → copy both values.

```powershell
aws configure
# AWS Access Key ID:     AKIA...
# AWS Secret Access Key: ...
# Default region name:   us-east-1     (or your preferred region)
# Default output format: json

aws sts get-caller-identity   # should print your account ID
```

### 3. Allow PowerShell to run local scripts (one-time)

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Type `Y` when prompted. This allows scripts you wrote / downloaded yourself to run.

### 4. Confirm OpenSSH is available

```powershell
ssh -V       # should print OpenSSH version
```

If it errors: Settings → Apps → Optional features → install "OpenSSH Client".

### 5. Confirm Docker Desktop is installed locally

You need this for `docker compose build`-style local commands (and to test the project before deploying).

```powershell
docker --version
```

---

## Provisioning

Pick a folder on your laptop where the SSH key (`.pem`) and deployment info will live. The scripts handle everything else.

```powershell
cd <path-to-your-cloned-telegram-forwarder>\deploy

# Provision an EC2 instance.
# OutputFolder is where the .pem and deployment-info.txt will be saved.
.\deploy-aws.ps1 -OutputFolder C:\Users\you\tg-forwarder-aws
```

What this does:
1. Creates an SSH key pair, saves the private key to `<OutputFolder>\tg-forwarder-key.pem` with the right permissions.
2. Creates a security group that allows SSH from your current public IP.
3. Looks up the latest Amazon Linux 2023 AMI.
4. Launches a `t3.micro` instance, with cloud-init installing Docker + buildx + Docker Compose v2.
5. Waits for it to boot and gets the public IP.
6. Writes a `deployment-info.txt` next to the .pem containing every variable you'll need later (REGION, INSTANCE_ID, KEY_PATH, SG_ID, etc.).

**Customizations:** `-Region us-west-2`, `-InstanceType t3.small`, `-NamePrefix mybot`. Defaults are `us-east-1`, `t3.micro`, `tg-forwarder`.

When it finishes, you'll see something like:

```
[+] Deployment complete.

  ssh -i "C:\Users\you\tg-forwarder-aws\tg-forwarder-key.pem" ec2-user@54.123.45.67
```

The `deployment-info.txt` file in `OutputFolder` looks like this:

```
REGION         = us-east-1
INSTANCE_ID    = i-0abcdef1234567890
INSTANCE_TYPE  = t3.micro
KEY_NAME       = tg-forwarder-key
KEY_PATH       = C:\Users\you\tg-forwarder-aws\tg-forwarder-key.pem
SG_NAME        = tg-forwarder-sg
SG_ID          = sg-0abcdef1234567890
AMI_ID         = ami-...
INSTANCE_TAG   = tg-forwarder
DEPLOYED_AT    = 2026-05-06 14:32:11 -07:00
DEPLOYED_FROM_IP = 73.x.x.x

PUBLIC_IP      = 54.123.45.67
```

**Save this folder.** Losing the .pem means rebuilding the instance (see "Recovery" at the bottom).

**Wait ~60 seconds** for cloud-init to finish installing Docker on the instance before you SSH in. The instance is reachable as soon as `deploy-aws.ps1` prints "Deployment complete," but `docker --version` won't work yet.

---

## Loading the deployment into a fresh shell

Any time you open a new PowerShell window and want to work with the instance:

```powershell
. .\load-deployment.ps1 -InfoFile C:\Users\you\tg-forwarder-aws\deployment-info.txt
```

The leading `.` is mandatory (PowerShell "dot-source" — runs the script in your current shell so the env vars and functions stick around).

It sets `$env:REGION`, `$env:INSTANCE_ID`, `$env:KEY_PATH`, etc., and defines four helper functions:

| Function | What it does |
|---|---|
| `Connect-Forwarder` | SSH into the instance (re-queries current public IP). |
| `Start-Forwarder` | Start the instance if stopped, then SSH in. |
| `Stop-Forwarder` | Stop the instance. Disk + Telegram sessions preserved; you stop paying for compute. |
| `Update-ForwarderIngress` | Add your current public IP to the security group. Run this if SSH suddenly times out — usually means your laptop's IP changed. |
| `Show-Forwarder` | Print all loaded env vars. |

Typical first SSH-in:

```powershell
. .\load-deployment.ps1 -InfoFile C:\Users\you\tg-forwarder-aws\deployment-info.txt
Connect-Forwarder
```

---

## On the EC2 box: install the project

You're now SSHed into the instance as `ec2-user`. Sanity-check Docker is up:

```bash
docker --version            # should print Docker version
docker compose version      # should print Docker Compose v2.x
```

If either errors, cloud-init isn't done yet — wait another 30 seconds and try again.

### Get the project onto the box

Easiest is `scp` from your laptop. From a PowerShell window with the deployment loaded:

```powershell
# From your laptop
scp -i $env:KEY_PATH path\to\telegram-forwarder.zip ec2-user@$env:PUBLIC_IP:~/
```

Or if you have the project in git, clone it directly on the box:

```bash
# On the EC2 box
git clone <your-repo-url> telegram-forwarder
cd telegram-forwarder
```

For the zip case:

```bash
# On the EC2 box
unzip telegram-forwarder.zip
cd telegram-forwarder
```

### Configure secrets

```bash
cp .env.example .env
nano .env
# Fill in TG_API_ID, TG_API_HASH, BOT_TOKEN
```

### Build the image

```bash
docker compose build
```

### Log in your Telegram user account(s)

This step needs the OTP/2FA prompts from Telegram, so it's interactive. SSH in to do it:

```bash
docker compose run --rm forwarder login
# Phone number, OTP, optional 2FA
```

The output prints a `short_id` (e.g. `15e2b0`) — note this. Repeat `login` for each Telegram account you want to use as a source listener.

```bash
docker compose run --rm forwarder list-users
```

### Drop your config(s) in `configs/`

By convention, name each user's config `configs/<short_id>.json`:

```bash
cp configs/mixed.example.json configs/15e2b0.json
nano configs/15e2b0.json
# Replace REPLACE_WITH_SHORT_ID with 15e2b0, fill in real chat ids/hashes
```

See the project's main `README.md` (sections 6–7) for the full schema. The example files in `configs/` are templates.

### Start the forwarder

Two patterns — the project's main README §8 covers both in detail. Short version:

**One user, long-running** — edit `command:` in `docker-compose.yml`:

```yaml
    command: ["forward", "-c", "/app/configs/15e2b0.json"]
```

Then:

```bash
docker compose up -d
docker compose logs -f forwarder
# Ctrl+C to exit the tail. Container keeps running.
```

**Multiple users, declarative** — edit `docker-compose.users.yml` to add per-user services, then:

```bash
docker compose -f docker-compose.users.yml up -d
docker compose -f docker-compose.users.yml ps
```

The forwarder now runs in the background, restarts on container crash (`restart: unless-stopped`), and survives EC2 reboots.

You can `exit` your SSH session — the bot keeps forwarding.

---

## Day-to-day operations

### Check what's happening

```powershell
. .\load-deployment.ps1 -InfoFile C:\Users\you\tg-forwarder-aws\deployment-info.txt
Connect-Forwarder
```

```bash
# On the EC2 box
docker compose logs --tail 100 forwarder    # last 100 lines
docker compose logs -f forwarder            # tail live
docker compose ps                           # status
```

### Update the forwarding config

Edit `configs/<short_id>.json` on the EC2 box, then:

```bash
docker compose restart forwarder
```

Bind-mounts mean compose picks up your edits without rebuilding the image.

### Stop the instance to save money (when not in active use)

In PowerShell:

```powershell
Stop-Forwarder
```

Disk and Telegram sessions are preserved; `Start-Forwarder` brings everything back, including the running containers.

Stopped instances don't incur compute charges, only EBS storage (~$0.80/month for the default 8 GB).

### SSH suddenly times out

Your laptop's public IP probably changed (residential ISPs rotate them, or you switched networks).

```powershell
Update-ForwarderIngress
Connect-Forwarder
```

### Move to a new laptop

Copy the `OutputFolder` (with the .pem and `deployment-info.txt`) to the new machine. Install AWS CLI, run `aws configure` with the same credentials, and `load-deployment.ps1` will re-establish everything.

---

## Recovery scenarios

### "I lost my .pem file"

The instance is fine; you just can't SSH in. AWS doesn't store private keys.

Use **EC2 Instance Connect** (browser-based shell, no key required). In the AWS console: EC2 → Instances → select your instance → Connect → "EC2 Instance Connect" tab → Connect. A browser terminal opens.

Inside that browser shell:

```bash
# Generate a fresh keypair on your laptop first (in PowerShell):
#   ssh-keygen -t ed25519 -f $HOME\tg-forwarder-key-v2 -N '""'
#   Get-Content $HOME\tg-forwarder-key-v2.pub     # copy this line
#
# Then in the browser shell:
echo 'ssh-ed25519 AAAAC3Nz... your-comment' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

After that, your new private key on the laptop works. Update `deployment-info.txt` to point `KEY_PATH` at the new file.

### "I lost the deployment-info.txt"

If you still have the .pem and remember (or can find) the instance's Name tag:

```powershell
$REGION="us-east-1"
aws ec2 describe-instances --region $REGION `
  --filters "Name=tag:Name,Values=tg-forwarder" `
            "Name=instance-state-name,Values=running,stopped,pending" `
  --query 'Reservations[].Instances[].{ID:InstanceId,IP:PublicIpAddress,Key:KeyName,SG:SecurityGroups[0].GroupId}' `
  --output table
```

Reconstruct `deployment-info.txt` by hand from those values, then `load-deployment.ps1` works again.

### "I lost both"

The instance is still running but you have no way back in. Same fix as the lost-pem case: use EC2 Instance Connect via the browser, then push a fresh public key. Find the instance via `describe-instances` first.

If even that fails (Instance Connect is somehow disabled), the only path left is launching a new instance with a fresh key, attaching the old EBS volume to it temporarily, copying out `data/sessions/`, then destroying the old volume. Painful — better to just `teardown-aws.ps1` the old deployment and start over.

---

## Tearing it all down

When you're done with the project entirely:

```powershell
.\teardown-aws.ps1 -InfoFile C:\Users\you\tg-forwarder-aws\deployment-info.txt
```

Asks for confirmation, then terminates the instance, deletes the security group, deletes the key pair, and removes the local .pem and info file.

**This is destructive.** The Telegram sessions on the instance are gone forever. Don't run it unless you mean it.

To keep the local files (in case you want them as a record):

```powershell
.\teardown-aws.ps1 -InfoFile C:\Users\you\tg-forwarder-aws\deployment-info.txt -KeepLocalFiles
```

---

## Cost reference

For a `t3.micro` running 24/7 in `us-east-1`:

- Compute: ~$7.50/month (free if you're in the AWS Free Tier — 750 hours/month for 12 months on new accounts).
- EBS storage (8 GB gp3): ~$0.80/month.
- Egress traffic: usually $0; bot traffic is well under the 100 GB/month free egress.

Stopping the instance with `Stop-Forwarder` while you're not actively forwarding cuts the compute charge to $0 — only EBS continues.
