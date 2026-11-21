# Hosting Heimdall: Oracle Always Free + Cloudflare (runbook, task 065)

Design and trust boundaries: `docs/decisions/ADR-009-hosted-deployment.md`. Pipeline: `docs/tasks/064-cicd.md`.
Result: `https://heimdall.<domain>` (prod) and `https://dev.heimdall.<domain>` (dev). Invitees sign in with an
access code and nothing else.

**Cost:**
- Oracle Always Free compute (2 VMs within 4 OCPU / 24 GB / 200 GB): $0
- Cloudflare Free: $0
- GitHub Free (private repo): $0
- Domain: about $10 a year

**Steps marked YOU involve accounts, credentials or publishing.** Agents must not do them (docs/guardrails.md).

## 0. Before you start
- [ ] **YOU** Oracle Cloud account with the **Singapore** home region. Upgrading to pay-as-you-go costs nothing within the free limits, and it gets you A1 capacity more easily and stops idle-VM reclamation. Set a budget alert at $1.
- [ ] **YOU** Cloudflare account. Register or transfer a domain (or point an existing one's nameservers to Cloudflare).
- [ ] **YOU** GitHub private repository, then push: `git remote add origin git@github.com:<you>/heimdall.git && git push -u origin main`.

## 1. Two VMs (OCI console → Compute → Instances)
| VM | Shape | OCPU / RAM | Boot volume | Image |
|---|---|---|---|---|
| `heimdall-prod` | VM.Standard.A1.Flex | 3 / 18 GB | 150 GB | Canonical Ubuntu 24.04 (aarch64) |
| `heimdall-dev` | VM.Standard.A1.Flex | 1 / 6 GB | 50 GB | Canonical Ubuntu 24.04 (aarch64) |

- Networking: put both in a VCN subnet. In the subnet's security list, **delete every ingress rule** (including 22). Egress: all.
- Enable the **Bastion** plugin on both instances (Oracle Cloud Agent tab). Create one Bastion in the VCN.
- SSH only through a Bastion "managed SSH session". Sessions are time-limited, and there is no public port.
- "Out of capacity" for A1: retry in another availability domain, or later. The pay-as-you-go upgrade helps.

## 2. Host setup (each VM, over the Bastion session)
```bash
sudo apt-get update && sudo apt-get -y full-upgrade
sudo apt-get -y install unattended-upgrades ca-certificates curl rsync age
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
curl -fsSL https://get.docker.com | sudo sh            # Docker Engine + compose plugin
sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw --force enable
sudo mkdir -p /srv/heimdall/{data,state} && sudo chown -R 10001:10001 /srv/heimdall/data
sudo useradd -m -s /bin/bash heimdall-runner && sudo usermod -aG docker heimdall-runner
sudo chown -R heimdall-runner: /srv/heimdall/state
```
Membership of the `docker` group is equivalent to root. Only the runner account has it, and it runs only this repo's main and tag jobs (§5).

## 3. Data (from the laptop, through a Bastion port-forwarding session to the VM's port 22 on localhost:2222)
```bash
rsync -av -e "ssh -p 2222" data/basemap data/ref data/replay ubuntu@localhost:/tmp/heimdall-data/
rsync -av -e "ssh -p 2222" data/analyst.duckdb ubuntu@localhost:/tmp/heimdall-data/     # prod only
# on the VM:
sudo rsync -a /tmp/heimdall-data/ /srv/heimdall/data/ && sudo chown -R 10001:10001 /srv/heimdall/data
```
- `heimdall.duckdb` and the lake rebuild from live ingest. You can copy `data/lake` too if you want history.
- **Do not copy `data/secrets/keys.json` blindly.** Put each VM's API keys in (§6) or through the Keys page with an admin code.

## 4. Cloudflare
- **Tunnel**, one per VM. Zero Trust → Networks → Tunnels → Create, choose "Cloudflared", and copy the token into that VM's `hosted.env` (§6). Public hostname: `heimdall.<domain>` (or `dev.heimdall.<domain>`) → service `http://edge:8080`. The dashboard may ask you to pick a Zero Trust plan; choose Free.
- **SSL/TLS**: Full. Turn on Always Use HTTPS, HSTS (max-age 12 months, include subdomains) and minimum TLS 1.2.
- **Security → WAF**: enable the Cloudflare Free managed ruleset. Add a rate-limiting rule: if the URI path equals `/api/login`, allow 5 requests per 10 s per IP, then block for 10 s. The app also limits attempts itself.
- **Caching**: leave the defaults. Gated pages and tiles are `Cache-Control: private` and must never be edge-cached.
- Optional: Bot Fight Mode. If it blocks the CI smoke test, add a WAF skip rule for the GitHub runner user agent on `/api/ready` and `/login.html` only.

## 5. GitHub Actions
- Settings → Actions → General:
  - Fork pull request workflows: off.
  - Workflow permissions: read.
  - Require approval for outside collaborators.
- Settings → Secrets and variables → Actions:
  - Variables `DEV_URL=https://dev.heimdall.<domain>` and `PROD_URL=https://heimdall.<domain>`.
  - Secret `BENCH_CODE`: an analyst code issued on **dev** for the perf check.
- **Self-hosted runners**: Settings → Actions → Runners → New (Linux, ARM64). On each VM, as `heimdall-runner`, follow the shown commands. Add the labels `heimdall-dev` (dev VM) or `heimdall-prod` (prod VM). Then `sudo ./svc.sh install heimdall-runner && sudo ./svc.sh start`.
  - Only `ci.yml` deploy-dev (push to main) uses `heimdall-dev`, and only `release.yml` (tags) uses `heimdall-prod`. PR jobs run on GitHub-hosted runners only.
  - **The runner label is a convention, not an access control.** Anyone with write access to the repo can push a workflow that targets `heimdall-prod`. Docker on that runner is root on the prod VM (security review 2026-10-04, High).
    - On GitHub Free, keep the repo **single-writer**: you only, and collaborators get read access at most.
    - Before you give anyone write access, upgrade to GitHub Pro (about $4 a month). Then:
      1. Add an environment `production` with yourself as required reviewer, and reference it from `deploy-prod`.
      2. Add a tag ruleset that restricts `v*` to you.
      3. Put the prod runner in a runner group limited to `release.yml`.

## 6. Host config
Copy `deploy/hosted.env.example` to `/srv/heimdall/hosted.env`, fill it in, and set mode 0600 with owner `heimdall-runner`:
- dev VM: `HEIMDALL_ENV=dev`, `HEIMDALL_MEM=4g`, `HEIMDALL_CPUS=0.9`.
- prod VM: `HEIMDALL_ENV=prod`, and optionally `COMPOSE_PROFILES=llm`.

Source API keys:
- Simplest: after the first deploy, sign in with an admin code and use the Keys page.
- Alternative: write `/srv/heimdall/data/secrets/keys.json` (mode 0600, uid 10001).

## 7. First deploy and access codes
1. Merge to `main`. CI publishes the images, deploys dev, then runs the smoke, perf and ZAP checks.
2. Issue codes on each VM:
   ```bash
   cd ~/actions-runner/_work/heimdall/heimdall/deploy
   docker compose --env-file /srv/heimdall/hosted.env -f compose.yml -f compose.hosted.yml \
     exec heimdall python scripts/access_codes.py add yourname --admin     # code printed once
   ```
   Send codes to invitees privately: a different channel from the link, one code per person.
   Revoke with `... access_codes.py revoke NAME`, which signs that person out at once.
3. Release to prod: `git tag -a v0.4.0 -m "first hosted release" && git push origin v0.4.0`.
4. Prod model (optional): `... exec ollama ollama pull qwen2.5:7b-instruct`. This is a one-time download from `registry.ollama.ai`, a new host for that VM (needs your approval).

## 8. Operate
- **Status**: `docker compose ... ps`. Logs: `docker compose ... logs -f heimdall`. Health: `curl -s https://heimdall.<domain>/api/ready`.
- **Roll back**: re-run the previous tag. Either push a new tag at an earlier commit, or on the VM run `IMAGE_REPO=ghcr.io/<you> deploy/scripts/deploy.sh v0.3.9`. A failed deploy rolls back by itself.
- **Backups**:
  - Each deploy copies `analyst.duckdb` to `/srv/heimdall/state/backups` (14 kept).
  - Also enable an OCI boot-volume backup policy (Bronze; the free tier includes 5 backups).
  - For an off-VM copy, add a nightly cron that encrypts with `age` to your public key and uploads to OCI Object Storage (20 GB free):
    ```bash
    17 3 * * * tar -C /srv/heimdall/data -cz analyst.duckdb secrets | age -r <age-pubkey> > /srv/heimdall/state/nightly.tar.gz.age
    ```
    Then upload with `oci os object put`, or rsync to a machine you own.
- **Restore drill** (quarterly, on dev):
  1. Stop heimdall.
  2. Copy a backup over `analyst.duckdb`.
  3. Deploy the current tag.
  4. Check that notes and verdicts are back.
- **Patching**: unattended-upgrades handles the OS. Dependabot PRs handle the app, images and Actions. Nightly CI re-scans the released images.
- **Idle reclamation**: Always Free A1 VMs whose CPU stays under 20 % for 7 days can be reclaimed. A pay-as-you-go account is exempt; otherwise keep live ingest on.

## 9. When to scale out (ADR-009 stage 2)
Build the scale-out task (067: Postgres/TimescaleDB store, Redis/Valkey bus, N API replicas behind Caddy) only when one of these holds:
- `verify-dev` perf budget fails repeatedly,
- the pipeline tick exceeds 30 s,
- or more than about 25 people use it at once.

Until then, scale up within the free tier (prod can take all 4 OCPU / 24 GB if dev moves back to the laptop).
