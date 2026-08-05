# Deploying to Oracle (Phase 3)

Exact-but-editable instructions for putting the Family Assistant on an
Oracle Cloud (or any similar Linux) VM, fronted by HTTPS, using Supabase
Postgres as the database. Every `<PLACEHOLDER>` is something you choose —
this guide does not invent a domain, username, or path for you, and no real
domain/IP/secret is committed anywhere in this repo.

This guide is **not executed for you**. Nothing here was run against your
Oracle instance or Supabase project during this coding session — you SSH in
and run each step yourself.

Steps 1–12 and 16–20 are identical either way. Step 13 (the reverse proxy)
has two paths — pick the one that matches your server:

- **Path A — dedicated Nginx + Certbot**: this app is the only thing on the
  box, or at least the only thing on ports 80/443. Nginx binds 80/443
  directly and Certbot manages the certificate.
- **Path B — an existing shared reverse proxy already owns ports 80/443**
  (e.g. another app on the same box already runs Caddy in Docker, publishing
  `80:80`/`443:443`). Skip Nginx and Certbot entirely; add one new site
  entry to the *existing* reverse proxy instead, and bind Gunicorn to a
  host-only TCP port so that proxy can reach it. lifeassistant still gets
  its own dedicated Linux user, project directory, venv, `.env`, systemd
  units, Gunicorn process, and database — completely separate from whatever
  else is on the box. The only thing shared is the box itself and that one
  reverse-proxy container, which just routes by hostname.

Placeholders used throughout:

- `<LINUX_USERNAME>` — a dedicated non-root user, e.g. `lifeassistant`
- `<PROJECT_PATH>` — where you clone the repo, e.g. `/home/<LINUX_USERNAME>/lifeos`
- `<VENV_PATH>` — usually `<PROJECT_PATH>/.venv`
- `<ENV_FILE_PATH>` — usually `<PROJECT_PATH>/.env`
- `<GIT_REMOTE_URL>` — your repository's clone URL
- `<YOUR_DOMAIN>` — the domain you point at this server, e.g. `family.example.com`
- *(Path B only)* `<SHARED_PROXY_COMPOSE_DIR>` — where the existing app's
  `compose.yaml`/`docker-compose.yml` and Caddyfile live, e.g.
  `/opt/<other-app>/deploy/oracle`
- *(Path B only)* `<LOCAL_APP_PORT>` — a host-only TCP port for Gunicorn to
  bind, e.g. `8001` — pick something not already in use
  (`sudo ss -tlnp` to check)

---

## 1. Create a non-root application user

```bash
sudo adduser <LINUX_USERNAME>
sudo usermod -aG sudo <LINUX_USERNAME>   # optional, only if this user needs sudo
su - <LINUX_USERNAME>
```

Everything from here on runs as `<LINUX_USERNAME>`, not root. The systemd
units in `deploy/systemd/` also run the app as this user (never root).

## 2. Clone the repository

```bash
cd ~
git clone <GIT_REMOTE_URL> lifeos
cd lifeos
```

`<PROJECT_PATH>` is now `/home/<LINUX_USERNAME>/lifeos`.

## 3. Create a Python virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

## 4. Install system packages

**Path A (dedicated Nginx):**
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv build-essential libpq-dev \
    nginx certbot python3-certbot-nginx git curl
```

**Path B (existing shared reverse proxy — no Nginx/Certbot needed here):**
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv build-essential libpq-dev git curl
```

(`libpq-dev` is needed to build `psycopg`; adjust package names if you are
on a non-Debian/Ubuntu distribution.)

## 5. Install Python requirements

```bash
pip install -r requirements.txt
```

## 6. Create the `.env`

```bash
cp .env.example .env
```

Edit `.env` and fill in every value — see [`.env.example`](.env.example)
for the full list. At minimum for production you need:

- `DJANGO_SECRET_KEY` — generate with:
  `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`
- `DJANGO_ALLOWED_HOSTS=<YOUR_DOMAIN>`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://<YOUR_DOMAIN>`
- `APP_ENCRYPTION_KEY` — generate with:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `DATABASE_URL` — see step 7
- `OPENAI_API_KEY`, `OPENAI_MODEL`
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (or `GOOGLE_CLIENT_SECRETS_FILE`), `GOOGLE_CALENDAR_ID`
- `AUTHORISED_EMAIL_IKE`, `AUTHORISED_EMAIL_WIFE`
- **Path B only**: `GUNICORN_BIND=127.0.0.1:<LOCAL_APP_PORT>` — makes
  Gunicorn bind a host-only TCP port instead of the default Unix socket, so
  the existing shared reverse proxy can reach it (see step 13, Path B).

`DJANGO_SETTINGS_MODULE=config.settings.production` is set in the systemd
units' `Environment=` line (see step 11), **not** in `.env` — it has to be
known before Django (and therefore `.env` loading) even starts.

Never commit `.env`. It is already in `.gitignore`.

## 7. Setting the Supabase `DATABASE_URL`

In the Supabase dashboard: Project Settings → Database → Connection string.

- **Transaction pooler** (port 6543, `?pgbouncer=true`) — use this for the
  long-running **web app** (Gunicorn). It handles many short-lived
  connections well, which is what a web process under load needs.
- **Direct connection** (port 5432) — use this for **one-off/administrative
  commands** you run by hand: `migrate`, `createsuperuser`, `google_auth`.
  Session-level features (like advisory locks, if ever needed) don't work
  reliably through the transaction pooler.

In practice: put the **transaction pooler** URL in `.env`'s `DATABASE_URL`
(that's what the web service and the two timers use day to day). If a
particular `migrate` run has trouble through the pooler, temporarily export
the **direct** connection string instead just for that one command:

```bash
DATABASE_URL="<direct-connection-string>" python manage.py migrate
```

Example shape (fill in your own values from the Supabase dashboard — do not
reuse this literally):

```text
postgresql://<db-user>:<db-password>@<pooler-host>:6543/postgres?pgbouncer=true
```

## 8. Running migrations

```bash
source .venv/bin/activate
python manage.py migrate
```

## 9. Creating a superuser

```bash
python manage.py createsuperuser
```

This is also how every other family member's login is created later —
via `/admin/` → Users → Add user (staff/superuser status only for whoever
should approve pending reviews).

## 10. Running `collectstatic`

```bash
DJANGO_SETTINGS_MODULE=config.settings.production python manage.py collectstatic --noinput
```

This writes into `<PROJECT_PATH>/staticfiles/`. Path A's example Nginx
config serves these directly (see step 13); Path B serves them through
Gunicorn itself via WhiteNoise (already configured in
`config/settings/production.py`) — no separate static file serving needed
there.

## 11. Installing the Gunicorn systemd service

```bash
sudo cp deploy/systemd/lifeassistant-web.service /etc/systemd/system/lifeassistant-web.service
sudo nano /etc/systemd/system/lifeassistant-web.service   # fill in every <PLACEHOLDER>
sudo systemctl daemon-reload
sudo systemctl enable lifeassistant-web
```

Do not `systemctl start` yet — start everything together in step 16.

## 12. Installing Gmail and reminder timers

```bash
sudo cp deploy/systemd/lifeassistant-gmail.service /etc/systemd/system/
sudo cp deploy/systemd/lifeassistant-gmail.timer /etc/systemd/system/
sudo cp deploy/systemd/lifeassistant-reminders.service /etc/systemd/system/
sudo cp deploy/systemd/lifeassistant-reminders.timer /etc/systemd/system/

sudo nano /etc/systemd/system/lifeassistant-gmail.service       # fill in <PLACEHOLDER>s
sudo nano /etc/systemd/system/lifeassistant-reminders.service   # fill in <PLACEHOLDER>s

sudo systemctl daemon-reload
sudo systemctl enable lifeassistant-gmail.timer
sudo systemctl enable lifeassistant-reminders.timer
```

Each timer fires its `.service` about once a minute. Systemd will not start
a second instance of a unit that's already running, and both underlying
management commands are themselves idempotent/safe-to-overlap (see the
comments in each `.service` file).

## 13. Configuring the reverse proxy

### Path A — dedicated Nginx

```bash
sudo cp deploy/nginx/lifeassistant.conf.example /etc/nginx/sites-available/lifeassistant.conf
sudo nano /etc/nginx/sites-available/lifeassistant.conf   # fill in <YOUR_DOMAIN> and <PROJECT_PATH>
sudo ln -s /etc/nginx/sites-available/lifeassistant.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Continue to step 14 (DNS) then step 15 (Certbot).

### Path B — an existing shared reverse proxy already owns 80/443

This applies when another app on the same box already runs a reverse proxy
(commonly Caddy) in Docker, with `ports: ["80:80", "443:443"]` published to
the host. A container in that state can't reach anything bound to the
*host's* `127.0.0.1` by default — Docker's default bridge network isolates
container-localhost from host-localhost — so two small, additive edits to
the **existing app's** deploy config are needed. Nothing about the existing
app's own site/service is touched.

1. **Confirm the shape of what's already there** (adjust names to match):
   ```bash
   docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
   cat <SHARED_PROXY_COMPOSE_DIR>/Caddyfile
   cat <SHARED_PROXY_COMPOSE_DIR>/compose.yaml   # or docker-compose.yml
   ```
   You're looking for the reverse-proxy service's `ports:` mapping 80/443
   to the host, and whether it already has `network_mode: host` or an
   `extra_hosts: host.docker.internal` entry (if it already does, skip
   straight to the Caddyfile edit below).

2. **Let the proxy container reach the host.** Add this under the existing
   reverse-proxy service in its `compose.yaml`:
   ```yaml
     caddy:
       # ...existing config...
       extra_hosts:
         - "host.docker.internal:host-gateway"
   ```

3. **Add a new site entry** to the existing Caddyfile — a template is at
   [`deploy/caddy/lifeassistant-site.Caddyfile.example`](deploy/caddy/lifeassistant-site.Caddyfile.example).
   Append its contents (with `<YOUR_DOMAIN>` and `<LOCAL_APP_PORT>` filled
   in) to the *existing* Caddyfile, alongside — not replacing — whatever
   site block is already there for the other app.

4. **Recreate just the proxy container** to pick up both changes:
   ```bash
   cd <SHARED_PROXY_COMPOSE_DIR>
   docker compose up -d caddy   # substitute the actual service name if different
   ```

5. Set `GUNICORN_BIND=0.0.0.0:<LOCAL_APP_PORT>` in lifeassistant's own
   `.env` (see step 6) — **not** in the other app's `.env`, and **not**
   `127.0.0.1:<LOCAL_APP_PORT>`. This is the part that's easy to get wrong:
   traffic from the proxy container arrives at the host via the Docker
   bridge interface (`host.docker.internal`, e.g. `172.17.0.1`), which is
   a *different* interface from loopback — a socket bound only to
   `127.0.0.1` will never see it, even once the firewall (next step)
   allows the packet through. `0.0.0.0` means "all interfaces," which is
   exactly what's needed here — but it also means Gunicorn is now
   reachable on the host's public interface too unless you lock that down,
   which is exactly what step 5b does.

5b. **Restrict `<LOCAL_APP_PORT>` to only the proxy's own Docker subnet**,
   at the host firewall. Find the real subnet first — do not assume it
   matches whatever `host.docker.internal` resolved to:
   ```bash
   docker network inspect <compose-project-name>_default \
     --format '{{range .IPAM.Config}}{{println "Subnet:" .Subnet "Gateway:" .Gateway}}{{end}}'

   sudo ufw status verbose   # confirm active, default deny incoming
   ```
   If `ufw` is active:
   ```bash
   sudo ufw allow from <subnet-from-above> to any port <LOCAL_APP_PORT> proto tcp
   sudo ufw deny <LOCAL_APP_PORT>/tcp
   sudo ufw status numbered   # confirm the allow-from-subnet rule isn't shadowed
   ```
   If `ufw` is inactive, check `sudo iptables -S INPUT` and
   `sudo iptables -S DOCKER-USER` instead before deciding what's needed —
   don't leave the port unfiltered either way.

   **Never add `<LOCAL_APP_PORT>` to the Oracle Cloud VCN Security
   List/NSG.** It must stay unreachable from the public internet at the
   cloud-network layer too — the host firewall rule above is what makes it
   reachable *only* to the proxy container, not a substitute for keeping
   it off the public ingress rules.

HTTPS is automatic here — the existing Caddy instance issues and renews a
Let's Encrypt certificate for the new hostname the first time it sees a
request for it, the same way it already does for its own site. **Skip
step 15 (Certbot) entirely** and go straight to step 16.

## 14. Configuring DNS

At your DNS provider, point `<YOUR_DOMAIN>` (an A record, and AAAA if you
have IPv6) at this server's public IP address. This session cannot do this
for you — it's outside your terminal entirely.

Wait for DNS to propagate (`dig <YOUR_DOMAIN>` should return the server IP)
before running Certbot in the next step.

## 15. Adding HTTPS with Certbot

**Path A only** — if you're on Path B, the existing reverse proxy already
handles HTTPS automatically; skip this step.

```bash
sudo certbot --nginx -d <YOUR_DOMAIN>
```

Certbot edits `/etc/nginx/sites-available/lifeassistant.conf` in place to
add the HTTPS server block and a matching HTTP→HTTPS redirect, and sets up
its own renewal timer (`certbot.timer`, installed by the `certbot` package —
verify with `systemctl list-timers | grep certbot`). This command is not
run for you — do it yourself once DNS is live.

After this, set in `.env`:

```env
DJANGO_SECURE_SSL_REDIRECT=True
```

(Already the default in `config/settings/production.py` if unset.)

## 16. Starting services

```bash
sudo systemctl start lifeassistant-web
sudo systemctl start lifeassistant-gmail.timer
sudo systemctl start lifeassistant-reminders.timer
sudo systemctl status lifeassistant-web --no-pager
```

## 17. Checking logs

```bash
sudo journalctl -u lifeassistant-web -f
sudo journalctl -u lifeassistant-gmail.service -n 50 --no-pager
sudo journalctl -u lifeassistant-reminders.service -n 50 --no-pager

# Path A only:
sudo tail -f /var/log/nginx/error.log

# Path B only — the shared proxy's own logs:
docker logs -f <shared-proxy-container-name>
```

## 18. Testing `/health/`

```bash
curl -s https://<YOUR_DOMAIN>/health/
# {"status": "ok", "database": "ok"}
```

If it returns `"database": "unreachable"` (HTTP 503), check `DATABASE_URL`
and Supabase network/allow-list settings before anything else.

## 19. Running the first live email test

1. Authenticate the assistant Google account **against production data**:
   run this locally (a browser is required for the OAuth consent screen),
   pointed at the same Supabase database as production:

   ```bash
   DATABASE_URL="<your Supabase connection string>" python manage.py google_auth
   ```

2. Verify the stored credential exists (and nothing sensitive is exposed):

   ```bash
   DATABASE_URL="<your Supabase connection string>" python manage.py shell -c \
     "from core.models import GoogleCredential; print(list(GoogleCredential.objects.values_list('account_email', 'created_at')))"
   ```

   You should see exactly one row for the assistant's Gmail address. (Do
   not print `.encrypted_data` or `.get_credentials()` output anywhere —
   see "Backups" below for why.)

3. On the server, forward a real appointment email to the assistant address
   from an authorised sender.
4. Watch it get picked up: `sudo journalctl -u lifeassistant-gmail.service -f`
5. Confirm in the web app at `https://<YOUR_DOMAIN>/` (Upcoming) and
   `/calendar/` that the event appears, and in your real Google Calendar.
6. If it lands in Review instead, check `https://<YOUR_DOMAIN>/review/` as a
   staff user.

See `MANUAL_TEST_PHASE_3.md` for the full manual test checklist.

## 20. Updating the application safely later

Use `deploy/update.sh.example` as a starting point (copy it, fill in the
placeholders, keep it out of version control if it ends up containing
anything server-specific). It pulls, installs requirements, migrates,
collects static files, restarts Gunicorn, and checks `/health/` — and stops
on the first error. It never touches `.env` and never runs a destructive
database command.

```bash
cp deploy/update.sh.example update.sh
chmod +x update.sh
nano update.sh   # fill in the placeholders
./update.sh
```

> **Your `update.sh` is a copy, and `git pull` will never update it.** When
> `deploy/update.sh.example` changes in the repo, the running server keeps the
> old copy until you re-run the `cp` above and re-fill the placeholders. This
> has already bitten this project twice: a health-check fix and, worse, the
> missing `export DJANGO_SETTINGS_MODULE=config.settings.production` that made
> every deploy collect static files under *development* settings — shipping a
> whole UI redesign that never appeared, because `staticfiles.json` was never
> rebuilt and `{% static %}` kept resolving to the previous stylesheet. After
> pulling a change to the example, diff the two before assuming you're current:
>
> ```bash
> diff deploy/update.sh.example update.sh
> ```

### Continuous deployment (optional): auto-deploy on push via GitHub Actions

By default, nothing about this deployment is automatic — a `git push` does
nothing to the running server until you SSH in and run `./update.sh`
yourself. `.github/workflows/deploy.yml` adds a GitHub Actions workflow
that runs `./update.sh` over SSH automatically on every push to `main`
(and on demand from the Actions tab). It is **not** active until you add
the secrets below — an empty/missing secret just makes the workflow fail
loudly, it never silently does nothing.

This needs a **new, separate SSH keypair** — not the deploy key you may
have already added to GitHub so the server could clone the repo. That one
lets the *server* pull *from* GitHub; this one lets *GitHub Actions* push
commands *into* the server, the opposite direction, and should not be reused.

1. **Generate the keypair** on the server, as the app user (`<LINUX_USERNAME>`):
   ```bash
   ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_actions_deploy -N ""
   ```

2. **Authorise its public half** to log in as that user:
   ```bash
   cat ~/.ssh/github_actions_deploy.pub >> ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```

3. **Copy the private key** so you can paste it into GitHub:
   ```bash
   cat ~/.ssh/github_actions_deploy
   ```
   Copy the *entire* output, including the `-----BEGIN OPENSSH PRIVATE
   KEY-----` / `-----END...-----` lines.

4. **Add repository secrets** on GitHub: repo → **Settings → Secrets and
   variables → Actions → New repository secret**. Create all four:
   - `ORACLE_SSH_DEPLOY_KEY` — the private key text you just copied
   - `ORACLE_HOST` — this server's public IP or hostname
   - `ORACLE_SSH_USER` — `<LINUX_USERNAME>`
   - `ORACLE_PROJECT_PATH` — `<PROJECT_PATH>`

   Secrets are encrypted at rest and masked in logs — this is safe even on
   a public repository. Never put any of these values directly in the
   workflow file itself.

5. **Delete the private key from the server** — only the GitHub secret
   needs to keep it now:
   ```bash
   shred -u ~/.ssh/github_actions_deploy   # or: rm ~/.ssh/github_actions_deploy
   ```
   (Keep `~/.ssh/github_actions_deploy.pub` and the line it added to
   `authorized_keys` — that's what's actually needed going forward.)

6. **Allow the restart step to run unattended.** `update.sh` calls `sudo
   systemctl restart lifeassistant-web`, and a GitHub Actions session has
   no terminal to answer a sudo password prompt — without this, that step
   just hangs. Install the scoped, single-command sudo rule:
   ```bash
   which systemctl   # confirm the real path first
   sudo visudo -f /etc/sudoers.d/lifeassistant-deploy
   # paste (with the real username and systemctl path):
   #   <LINUX_USERNAME> ALL=(root) NOPASSWD: /usr/bin/systemctl restart lifeassistant-web
   sudo chmod 440 /etc/sudoers.d/lifeassistant-deploy
   ```
   See `deploy/sudoers/lifeassistant-deploy.example` for the same content
   with more context. `visudo` validates syntax before saving — never edit
   a sudoers file with a plain text editor.

7. **Check SSH reachability.** GitHub's hosted Actions runners use
   rotating IPs, not a fixed range you can allow-list — if this server's
   firewall/Security List restricts SSH to specific IPs, this workflow
   will fail to connect. Key-only authentication (already the case here)
   is what keeps SSH safe with port 22 open broadly; if you'd rather not
   widen SSH access at all, a self-hosted Actions runner installed on this
   same server is the alternative (own separate research — not covered
   here).

8. **Test it**: push a trivial commit to `main`, or trigger it manually
   from the repo's **Actions** tab → *Deploy to Oracle* → **Run workflow**.
   Watch the run's logs there, and cross-check with
   `sudo journalctl -u lifeassistant-web -n 50 --no-pager` on the server.

---

## Backups

Deliberately simple — no separate backup service or tooling is introduced.

- **Supabase database backups**: Supabase Pro projects take automatic daily
  backups (Project Settings → Database → Backups). On the Free tier, take
  manual backups periodically:
  ```bash
  pg_dump "<direct-connection-string>" -Fc -f lifeos-backup-$(date +%Y%m%d).dump
  ```
  Store the resulting `.dump` file somewhere other than the Oracle VM
  (e.g. your own machine, or encrypted cloud storage).
- **Exporting individual tables**, if a full dump is overkill:
  ```bash
  psql "<direct-connection-string>" -c "\copy core_task TO 'tasks.csv' CSV HEADER"
  ```
  Substitute the table name (`core_task`, `core_note`, `core_reminder`,
  `core_calendareventrecord`, ...).
- **Protecting the Google OAuth credential**: the refresh token is stored
  Fernet-encrypted in the `core_googlecredential` table, keyed by
  `APP_ENCRYPTION_KEY`. Back up `APP_ENCRYPTION_KEY` (from `.env`) somewhere
  safe and separate from the database backup — a database backup alone is
  useless without it, and losing the key means re-running
  `python manage.py google_auth` to re-authenticate.
- **Backing up the Oracle `.env`**: copy it somewhere safe and encrypted,
  off the VM, whenever it changes:
  ```bash
  scp <LINUX_USERNAME>@<server>:<PROJECT_PATH>/.env ./lifeos-env-backup-$(date +%Y%m%d)
  ```
  Never commit it, never paste its contents into chat/tickets, never log it.
- **Restoring**: `git clone` the repository fresh, restore the saved `.env`
  (including `APP_ENCRYPTION_KEY`), point `DATABASE_URL` at a freshly
  restored Supabase database (`pg_restore` from the `.dump` file, or
  Supabase's own point-in-time restore), then follow steps 3, 5, 8, 10–16
  above again on the (new or same) server.
