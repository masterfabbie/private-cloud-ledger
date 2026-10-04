<p align="center"><img src="logo.png" alt="Proud Ledger – Private Cloud Ledger" width="320"></p>

# Proud Ledger

Proud Ledger is a private cloud ledger: a self-hosted personal finance tracker. You upload the CSV exports from your bank, and it gives you:
- charts,
- budgets,
- automatic categorization,
- subscription detection.

It supports multiple users, and each user's data is private. Everything runs in one Docker container with a SQLite database.

It replaces the old single-file version, which is kept at `legacy/financetracker_rework.html`.

## Features

- **CSV import** for most German and international bank exports, including Sparkasse, DKB, ING, Volksbank, comdirect and N26.
  - Detects UTF-8 or Windows-1252 encoding, the delimiter, and metadata lines before the header row.
  - Understands German number and date formats such as `1.234,56`, `12,50-`, `100,00 S` and `01.05.25`.
  - Suggests the column mapping and remembers it for each account.
  - Has a "Check parsing" dry run.
- **Duplicate detection:** re-importing overlapping exports never creates doubles, and genuinely identical transactions within one file are kept.
- **Undo** for a whole import from the import history.
- **Multiple accounts** with opening balances, a balance-over-time chart, and automatic detection of transfers between your own accounts.
- **Credit cards:** give a credit card account the text your bank uses for the card settlement (e.g. "KREDITKARTENABRECHNUNG").
  - The settlement on your checking account and the matching payment on the card statement are counted as transfers, so card purchases are not counted twice.
  - The Accounts page shows each settlement and whether the card statement has the matching payment.
- **Split transactions:** distribute one booking, such as an Amazon order or a supermarket receipt, over several categories.
  - The editor shows the remaining amount, and "= rest" fills the remaining amount into a row.
  - You can paste the items as lines (`Kaffeebohnen 12,99`). Categories are suggested by your rules.
  - Statistics, budgets, filters and exports use the parts.
- **Categories** with colours, and **rules** for auto-categorization:
  - A rule can match on payer, description or IBAN, using contains, equals or regex, and on the amount (exactly, between, at least or at most). Either part can be used alone.
  - While you edit a rule, a live preview shows which transactions it matches and warns when an earlier rule would assign them to a different category. One checkbox applies the rule to those transactions right away.
  - When you change a category in the list, the app offers to create a rule and apply it to similar transactions.
- **Budgets** per category per month, with progress bars and over-budget warnings.
- **Subscriptions and recurring payments:**
  - Detected automatically for weekly, monthly, quarterly and yearly intervals.
  - Shows your fixed costs per month and year.
  - The dashboard forecasts the payments still due this month and your month-end balance.
- **Dashboard:**
  - Income, expense and balance cards.
  - A monthly line chart with a category filter.
  - An expenses-by-category pie chart.
  - A balance chart.
  - Filters for account, year and month.
- **Transactions:** search, filters, inline category editing, tags, notes, manual entry and pagination.
- **Export** to CSV (semicolon-separated with comma decimals, like the old version), to Excel (`.xlsx`), and as a full JSON backup that can be **restored** under Settings.
- **Bank sync via FinTS/HBCI.** Fetch transactions directly from German banks such as Sparkassen, Volksbanken, DKB, ING and comdirect, including pushTAN app confirmation. Sync manually or automatically every few hours.
- **Single sign-on** with authentik or any other OpenID Connect provider. Users are created on first login, and admin rights can follow a group.
- **User management:**
  - The admin creates, deactivates, promotes and deletes users and resets passwords.
  - Users change their own passwords.
  - Self-registration is optional.

## Quick start (Docker)

### Prebuilt image from Docker Hub

Ready-made images are published at [`drsmee/proud-ledger`](https://hub.docker.com/r/drsmee/proud-ledger). Pick a version from the [Tags](https://hub.docker.com/r/drsmee/proud-ledger/tags) page. Images are currently built for `linux/amd64`.

Create a folder with a `.env` (copy [`.env.example`](.env.example) and set at least `ADMIN_PASSWORD`) and this `docker-compose.yml`:

```yaml
services:
  app:
    image: drsmee/proud-ledger:<tag>   # e.g. a version from the Tags page
    container_name: proud-ledger
    restart: unless-stopped
    env_file: .env
    environment:
      DATABASE_URL: sqlite:////data/finance.db
    ports:
      - "8000:8000"
    volumes:
      - ft-data:/data

volumes:
  ft-data:
```

```bash
docker compose up -d
```

### Build it yourself

```bash
git clone https://github.com/masterfabbie/private-cloud-ledger.git && cd private-cloud-ledger
cp .env.example .env        # then edit ADMIN_PASSWORD
docker compose up -d --build
```

Open http://localhost:8000 and log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`. The admin is created only on the first start, when the database has no users yet. After that you can change the password under **Settings**.

The data lives in the Docker volume `ft-data`, in the file `/data/finance.db` inside the container.

### Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | First admin's username (first start only) |
| `ADMIN_PASSWORD` | – | First admin's password (first start only) |
| `COOKIE_SECURE` | `false` | Set to `true` when served over HTTPS |
| `ALLOW_REGISTRATION` | `false` | Show a "Create an account" link on the login page |
| `SESSION_DAYS` | `14` | How long a login lasts |
| `MAX_UPLOAD_MB` | `10` | Maximum CSV size |
| `PUBLIC_URL` | – | Public address, e.g. `https://ledger.example.com` (needed for SSO behind a proxy) |
| `PASSWORD_LOGIN` | `true` | Allow username/password login |
| `OIDC_*` | – | Single sign-on, see below |
| `FINTS_PRODUCT_ID` | – | FinTS product registration number; enables bank sync |
| `BANK_SYNC_INTERVAL_HOURS` | `6` | Automatic bank sync interval; `0` turns it off |

### Single sign-on (authentik and other OIDC providers)

Proud Ledger can log users in through any OpenID Connect provider. The steps for authentik:

1. In authentik, go to **Applications → Providers → Create** and choose **OAuth2/OpenID Provider**.
   - **Client type:** Confidential.
   - **Redirect URIs:** `https://ledger.example.com/api/auth/oidc/callback`, using your own address.
   - **Scopes:** keep the defaults `openid`, `email` and `profile`. authentik's `profile` scope already includes the user's `groups`.
2. Create an **Application** that uses this provider, for example with the slug `proud-ledger`. Use its bindings to control who may log in.
3. Optionally, create a group such as `ledger-admins` for the people who should manage users in Proud Ledger.
4. Add the values to `.env` and restart:

   ```env
   PUBLIC_URL=https://ledger.example.com
   COOKIE_SECURE=true
   OIDC_ISSUER_URL=https://auth.example.com/application/o/proud-ledger/
   OIDC_CLIENT_ID=<client id from the provider>
   OIDC_CLIENT_SECRET=<client secret from the provider>
   OIDC_DISPLAY_NAME=authentik
   OIDC_ADMIN_GROUP=ledger-admins
   ```

The login page then shows a **Log in with authentik** button. How users are handled:
- **First login:** a Proud Ledger user is created with default categories and an account.
- **Identity:** users are matched by the provider's user ID (`sub`), so renaming someone in authentik keeps their data.
- **Admin rights:** with `OIDC_ADMIN_GROUP` set, they are synced from the group on every login. Without it, you manage admins on the Admin page. If the database has no users at all, the first SSO user becomes admin.
- **Existing local accounts:** set `OIDC_LINK_EXISTING_USERS=true` to attach SSO logins to local users with the same username. This is off by default, because it trusts the usernames your provider sends.
- **Password login:** stays available as a fallback. Set `PASSWORD_LOGIN=false` to allow single sign-on only.

For another provider, use its issuer URL, the one whose `/.well-known/openid-configuration` exists. Adjust `OIDC_USERNAME_CLAIM` and `OIDC_GROUPS_CLAIM` if the provider names those claims differently. For example, Keycloak needs a "groups" mapper.

Log out in Proud Ledger ends only the Proud Ledger session, not your authentik session.

### Bank sync (FinTS/HBCI)

Proud Ledger can fetch transactions directly from banks that offer FinTS with PIN/TAN. That includes most Sparkassen and Volks- und Raiffeisenbanken, as well as DKB, ING, comdirect, Consorsbank and many others. Many app-only banks such as N26 don't offer FinTS; keep using CSV import for those.

**Setup (once per server):**
1. Apply for a **FinTS product registration** with the Deutsche Kreditwirtschaft. It's free; see [fints.org](https://www.fints.org) and the registration form at [hbci-zka.de](https://www.hbci-zka.de/register/prod_register.htm). Banks may reject unregistered software.
2. Put the number in `.env` as `FINTS_PRODUCT_ID`.
3. To allow stored PINs and automatic sync, also set `SECRET_KEY`, a long random string. Stored PINs are encrypted with it, so keep it secret and don't change it, or stored PINs have to be entered again.

**Connecting a bank** is done on the **Accounts** page with **Connect a bank**:
- **What to enter:** bank code (BLZ), online-banking login name, the bank's FinTS server address and your PIN. Your bank lists the server address in its online-banking help; Sparkassen use addresses like `https://banking-xx….de/fints30`.
- **TAN method:** chosen once on the first connection, e.g. **pushTAN** for the S-pushTAN app. When the bank asks for a confirmation, the dialog waits until you confirm in the app. TANs you type in (chipTAN manuell, SMS) and photoTAN images are supported too. Optical flicker codes are not.
- **Linking accounts:** link each bank account to a Proud Ledger account, or create a new one. **Sync from** starts the day after your newest existing transaction, so CSV history and bank sync don't overlap. For an empty account it starts 90 days back. Going further back usually needs a TAN.

**Automatic sync:**
- **Turning it on:** store the PIN and tick **Sync automatically**. Synced connections are then checked every `BANK_SYNC_INTERVAL_HOURS`.
- **When the bank asks for a TAN:** many banks only ask for one every 90 days for reading transactions. When they do, the automatic sync stops and the connection shows **Needs your confirmation** until you click **Sync now**.
- **Wrong PIN:** if the bank rejects the PIN, the stored PIN is deleted and automatic sync turned off immediately, so retries can't lock your online banking.

What else to know:
- **Duplicates:** synced transactions go through the same duplicate detection, rules, transfer detection and subscription detection as CSV imports. Each sync appears in the import history and can be undone there.
- **Credentials and backups:** bank credentials are never part of the JSON backup.

### HTTPS / reverse proxy

Run the app behind a reverse proxy that provides TLS, and set `COOKIE_SECURE=true`. Example `Caddyfile`:

```
finance.example.com {
    reverse_proxy localhost:8000
}
```

The app runs a single worker on purpose. SQLite, the pending-upload store and the login rate limiter all assume one process, which is plenty for a household.

### Backups

```bash
# consistent copy of the database, then copy it out of the container
docker compose exec app sqlite3 /data/finance.db ".backup /data/backup.db"
docker compose cp app:/data/backup.db ./finance-backup.db
```

Each user can also download a JSON backup of their own data under **Settings**, and restore it there with **Restore from backup**. A restore works like this:
- **Check first:** the file is checked completely before anything changes, and you see how many accounts, transactions and other items it contains compared with your current data.
- **Replace:** restoring replaces all of your own data. Other users are not affected.
- **New server or user:** this is also how you move your data to a new server or another user account. Log in as the target user and restore the file there.
- **Not included:** saved CSV column settings are not part of the backup.

### Updating

```bash
APP_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build   # migrations run automatically on start
```

The version and commit are shown at the bottom of every page, for example `Proud Ledger v2.3.1 · caf5a03`. The version comes from `pyproject.toml`. The commit comes from the `APP_COMMIT` build argument, and is left out when the argument isn't set. When you build the image yourself, use `docker build --build-arg APP_COMMIT=$(git rev-parse --short HEAD) -t <user>/proud-ledger .`

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ADMIN_PASSWORD=admin123 DATABASE_URL=sqlite:///./data/finance.db alembic upgrade head
ADMIN_PASSWORD=admin123 uvicorn app.main:app --reload
```

The API docs are at http://localhost:8000/api/docs.

### Project layout

```
app/
  main.py, config.py, db.py, models.py, schemas.py, auth.py
  routers/     REST endpoints (/api/...)
  services/    csv_parser, importer, rules, recurring, stats, queries
  static/      frontend (vanilla JS modules + Chart.js, no build step)
alembic/       database migrations
tests/         pytest suite
```

To change the schema, edit `app/models.py`, then run `alembic revision --autogenerate -m "describe change"` and commit the new file in `alembic/versions/`.

### Importing data from the old HTML version

The old version kept its data only in the browser. Its **Export CSV** file (`Date;Description;Payer/Payee;Amount;Type;Category;Tags`) can be imported directly. The columns are recognised automatically, and the old category keys (`food`, `monthly-bills`, …) are mapped to the new categories.
