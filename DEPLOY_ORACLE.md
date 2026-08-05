# Deploying to Oracle (Phase 3)

Exact-but-editable instructions for putting the Family Assistant on an
Oracle Cloud (or any similar Linux) VM, fronted by Nginx with HTTPS, using
Supabase Postgres as the database. Every `<PLACEHOLDER>` is something you
choose — this guide does not invent a domain, username, or path for you.

This guide is **not executed for you**. Nothing here was run against your
Oracle instance or Supabase project during this coding session — you SSH in
and run each step yourself.

Placeholders used throughout:

- `<LINUX_USERNAME>` — a dedicated non-root user, e.g. `lifeassistant`
- `<PROJECT_PATH>` — where you clone the repo, e.g. `/home/<LINUX_USERNAME>/lifeos`
- `<VENV_PATH>` — usually `<PROJECT_PATH>/.venv`
- `<ENV_FILE_PATH>` — usually `<PROJECT_PATH>/.env`
- `<GIT_REMOTE_URL>` — your repository's clone URL
- `<YOUR_DOMAIN>` — the domain you point at this server, e.g. `family.example.com`

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

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv build-essential libpq-dev \
    nginx certbot python3-certbot-nginx git curl
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

This writes into `<PROJECT_PATH>/staticfiles/`, which the example Nginx
config serves directly (see step 13).

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

## 13. Configuring Nginx

```bash
sudo cp deploy/nginx/lifeassistant.conf.example /etc/nginx/sites-available/lifeassistant.conf
sudo nano /etc/nginx/sites-available/lifeassistant.conf   # fill in <YOUR_DOMAIN> and <PROJECT_PATH>
sudo ln -s /etc/nginx/sites-available/lifeassistant.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 14. Configuring DNS

At your DNS provider, point `<YOUR_DOMAIN>` (an A record, and AAAA if you
have IPv6) at this server's public IP address. This session cannot do this
for you — it's outside your terminal entirely.

Wait for DNS to propagate (`dig <YOUR_DOMAIN>` should return the server IP)
before running Certbot in the next step.

## 15. Adding HTTPS with Certbot

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
sudo tail -f /var/log/nginx/error.log
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
