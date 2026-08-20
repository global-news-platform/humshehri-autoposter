# Humshehri Facebook Auto-Poster

A production-ready Python bot that automatically fetches newly published
articles from **[humshehri.pk](https://humshehri.pk/)** and publishes them to
the **Humshehri Facebook Page** with **randomized, human-feeling intervals** so
your page never looks spammy.

The bot parses the site's RSS feed **and** its WordPress REST API (auto-falls
back when the feed is empty or blocked), extracts title, summary, featured
image and the article link, and posts each article to Facebook via the **Meta
Graph API** with a photo attached.

---

## Features

- **Two article sources** — WordPress REST API first, RSS feed as fallback
  (the `/feed/` endpoint on humshehri.pk is WAF-protected, so the bot uses the
  reliable `wp-json/wp/v2/posts` API automatically).
- **Image extraction** — resolves each article's featured image through the
  WordPress media API.
- **Duplicate prevention** — posted article IDs/GUIDs are stored in SQLite
  (or JSON); nothing is ever re-posted, even across restarts.
- **Randomized scheduling** — pick a random delay from a preset list
  (`3, 5, 7, 11, 13, 17, 21, 27` minutes) **or** a random integer between 3 and
  30 minutes.
- **Robust error handling** — automatic retries with exponential backoff,
  photo-post failure falls back to a link post, permanently broken articles
  are skipped after a configurable number of attempts.
- **Clear logging** — console + rotating file logs showing fetched articles,
  time remaining until the next post, and successful Facebook post IDs.
- **Safe to test** — `--dry-run` and `--list-new` modes that never touch
  Facebook.
- **Cloud cron support** — `--once --cron --max-posts 1 --no-delay` posts a
  single article per run with the randomized cadence persisted in the database,
  designed for always-on scheduling on GitHub Actions.

---

## Project Structure

```
.
├── main.py                   # Core application (extract, schedule, post, log)
├── requirements.txt          # Python dependencies
├── .env.example              # Template for your credentials (copy to .env)
├── README.md                 # This guide
├── .github/workflows/        # Cloud cron (GitHub Actions) - runs 24/7 free
│   └── autopost.yml
├── posted_articles.db        # Committed to the repo so the cloud bot
│                             # remembers what was already posted (SQLite)
└── logs/                     # Created automatically (rotating log files)
```

---

## Prerequisites

- **Python 3.9 or newer**
- A **Facebook Page** you administer
- A Meta (Facebook) developer account

Check your Python version:

```bash
python --version
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy the template and open it:

```bash
cp .env.example .env
```

On Windows:

```cmd
copy .env.example .env
```

Edit `.env` and set at least `FACEBOOK_PAGE_ACCESS_TOKEN`:

```dotenv
FACEBOOK_PAGE_ID=100071825280252
FACEBOOK_PAGE_ACCESS_TOKEN=EAAG...your-long-lived-token...ZD
```

The `FACEBOOK_PAGE_ID` already defaults to `100071825280252` (Humshehri's
page), so you normally only need to add your token.

> **Security:** never commit `.env` to version control. The repo's `.gitignore`
> should include it. The bot loads the file with `python-dotenv` at startup.

---

## Getting a non-expiring Facebook Page Access Token

The Graph API short-lived tokens (1–2 hours) and even standard long-lived
tokens (60 days) expire. For an always-running bot you need a **long-lived Page
access token** generated from a long-lived User token.

### Step 1 — Create a Facebook App

1. Go to **[developers.facebook.com](https://developers.facebook.com/)** and
   sign in with the account that **administers the Humshehri Page**.
2. Click **My Apps → Create App**.
3. Choose **"Business"** as the app type (needed for Page management access).
4. Name it (e.g. *Humshehri Auto-Poster*), click **Create App**.
5. In the dashboard you will see your **App ID** and **App Secret**.

### Step 2 — Add the "Pages" product

1. In the left sidebar click **Add Product** (or under *App settings*).
2. Find **Pages** and click **Set Up**.

### Step 3 — Generate a long-lived user token

1. In the **Pages** product, open the **Tools** section, then **Graph API
   Explorer**.
2. In the top-right corner, select your **Humshehri Facebook App**.
3. Click **Generate Access Token** and allow the Facebook login dialog.
4. Under **Add a Permission**, add and grant these scopes:
   - `pages_read_engagement`
   - `pages_manage_posts`
   - `pages_show_list`
5. (Optional, only if using the **Get Token** helper) keep the token selected.
6. In the **Token type** dropdown choose **Long-lived Access Token** and click
   **Extend Access Token**. This gives you a user token valid for ~60 days.

### Step 4 — Exchange the long-lived user token for a permanent Page token

The quickest way is the Graph API Explorer again:

1. In the Explorer, set the endpoint to `me/accounts` and click **Submit**.
   This lists all the Pages your user manages.
2. Find the entry for **Humshehri** (page id `100071825280252`) and copy its
   `access_token` field — **this is the Page Access Token**.
3. Page access tokens generated this way from a long-lived user token are
   **non-expiring** unless you change your Facebook password or revoke access.

> **Verify it never expires:** the token will have no `expires_at` field, or
> `expires_at` will be `0`.

### Step 5 — Confirm the token on your Page

1. With the Page token, run:

   ```bash
   curl "https://graph.facebook.com/v20.0/me?access_token=YOUR_PAGE_TOKEN"
   ```

   The `name` returned should be the **Humshehri Page**, not your personal
   profile.

2. Paste the token into `.env`:

   ```dotenv
   FACEBOOK_PAGE_ACCESS_TOKEN=YOUR_PAGE_TOKEN
   ```

> **Troubleshooting:** if the token shows your personal name instead of the
> page, you used a User token, not a Page token. Repeat Step 4 and use the
> token from `me/accounts`. If a permission is missing, review Step 3 scopes
> and re-generate the token.

---

## Usage

### Test that fetching works (no Facebook call)

```bash
python main.py --list-new
```

This prints every new article the bot found and would post, without touching
Facebook.

### Dry-run a full cycle (fetch + simulate posting)

```bash
python main.py --once --dry-run
```

### Post everything currently un-posted once, then exit

```bash
python main.py --once
```

### Run continuously (recommended for production)

```bash
python main.py
```

The bot now runs forever:

1. Fetches new articles from humshehri.pk.
2. Posts each one to the page (photo post, or link post if no image).
3. Waits a **random** interval before the next post (default preset:
   3–27 minutes, or random 3–30 minutes in `random` mode).
4. When no new articles exist, it re-checks every `POLL_INTERVAL_MIN`
   (default 30) minutes.

Press **Ctrl+C** to stop gracefully after the current post finishes.

### Run from a cloud cron (e.g. GitHub Actions)

Instead of `run_forever`, let an external scheduler invoke the bot on a timer.
Each run posts **at most one** article and records the next allowed posting
time in the database, so the randomized cadence survives between runs:

```bash
python main.py --once --cron --max-posts 1 --no-delay
```

This is exactly what `.github/workflows/autopost.yml` does (see
*Deploying on a server → Option D*).

---

## Scheduling explained

Two modes, configured with `SCHEDULE_MODE` in `.env`:

| Mode     | Behaviour                                                        |
|----------|------------------------------------------------------------------|
| `preset` | `random.choice(INTERVALS_MIN)` e.g. `[3, 5, 7, 11, 13, 17, 21, 27]` minutes |
| `random` | `random.randint(MIN_INTERVAL_MIN, MAX_INTERVAL_MIN)` minutes, default 3–30 |

Example: with the default preset, the bot might wait 7 min, then 21 min, then
3 min — a natural, non-uniform cadence that looks human.

---

## Logging

Logs are printed to the console **and** written to `logs/humshehri_autoposter.log`
(rotating at 5 MB, 3 backups). Every cycle shows:

```
2026-08-01 12:00:01 | INFO  | Fetched 10 candidate(s), 3 new (not yet posted)
2026-08-01 12:00:01 | INFO  |   -> NEW  [78956] "پنجاب دی ویل" پنج دریائی سرزمین کی تاریخ...
2026-08-01 12:00:02 | INFO  | Posting article [78956]: ...
2026-08-01 12:00:04 | INFO  |   SUCCESS - posted [78956] -> Facebook post id 1234567890
2026-08-01 12:00:04 | INFO  | Next post in 17 min 00 sec
```

Set `LOG_LEVEL=DEBUG` in `.env` for more verbose output.

---

## Deploying on a server

For 24/7 operation **even when your computer is off**, use GitHub Actions: it
runs the bot on GitHub's cloud servers on a timer, so news keeps getting posted
no matter what happens to your machine. A VPS or a home machine that stays on
is only needed if you want to keep full control of the schedule.

### Option D — GitHub Actions (recommended, free)

The repository ships with `.github/workflows/autopost.yml`. It runs the bot
every 15 minutes in the cloud and automatically commits the SQLite database
back to the repo, so nothing is ever re-posted and the randomized posting
cadence is preserved across runs.

**1. Create the repository and push your code**

```bash
git init
git add .
git commit -m "Initial commit"
# create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

Make sure `posted_articles.db` (which contains the already-posted articles from
your local runs) is included in that first commit.

**2. Add the Facebook token as a secret**

1. On github.com open **Your repo → Settings → Secrets and variables →
   Actions**.
2. Click **New repository secret**:
   - **Name:** `FACEBOOK_PAGE_ACCESS_TOKEN`
   - **Value:** your long-lived Page access token (see the token guide above).
3. (Optional) Under **Variables**, add `FACEBOOK_PAGE_ID` if your page id
   differs from the default `100071825280252`.

**3. Start it**

The `schedule` trigger is already in the workflow file, so it starts running on
its own after the push. You can also trigger a manual run right away: open the
**Actions** tab → **Humshehri Auto-Poster** → **Run workflow**.

Each run posts **at most one** article (keeping the randomized 3–27 min
cadence), so this is not spammy.

> **Free-tier note:** public repositories get unlimited Actions minutes; for a
> **private** repository the free allowance is 2000 minutes/month. The default
> 15-minute schedule stays comfortably inside that. If your repo is public and
> you want even faster cadence, change `'*/15 * * * *'` to `'*/5 * * * *'` in
> `.github/workflows/autopost.yml`.

---

### Option A — systemd (Linux server)

Create `/etc/systemd/system/humshehri-autoposter.service`:

```ini
[Unit]
Description=Humshehri Facebook Auto-Poster
After=network.target

[Service]
WorkingDirectory=/opt/humshehri-autoposter
ExecStart=/usr/bin/python3 /opt/humshehri-autoposter/main.py
Restart=always
RestartSec=30
EnvironmentFile=/opt/humshehri-autoposter/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now humshehri-autoposter
sudo systemctl status humshehri-autoposter   # check it is running
journalctl -u humshehri-autoposter -f         # follow the logs
```

### Option B — Windows Task Scheduler

1. Open **Task Scheduler → Create Basic Task**.
2. Trigger: **When the computer starts** (or "Daily", repeat).
3. Action: **Start a program** → `python.exe`, argument `main.py`, start-in the
   project folder.
4. Check **"Run whether user is logged on or not"** so it keeps running.

### Option C — Supervisor / screen / tmux

```bash
pip install supervisor
# then configure a [program:humshehri] entry with autostart=true, autorestart=true
```

---

## Common issues

| Problem | Fix |
|---|---|
| `HTTP 406` from the feed | Expected — the RSS feed is WAF-blocked. The bot automatically falls back to the WordPress REST API. |
| `Graph API error 190 (expired token)` | Token expired. Re-generate a Page token as in the guide; check it has no `expires_at`. |
| `Graph API error 200 (permission)` | Missing `pages_manage_posts` scope, or the token is a User token, not a Page token. |
| `(#100) Invalid image url` | The featured image could not be fetched; the bot already falls back to a text+link post. |
| Article not appearing on the page | The bot may be mid-interval. Wait for the next randomized post time or check the log file. |
| Duplicate posts | Should not happen — the SQLite `posted_articles` table prevents it. Keep `posted_articles.db` in the project folder. |

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `FACEBOOK_PAGE_ID` | `100071825280252` | Numeric Page ID. |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | *(empty)* | Non-expiring Page access token. |
| `RSS_FEED_URL` | `https://humshehri.pk/feed/` | RSS source (fallback). |
| `WP_API_URL` | `https://humshehri.pk/wp-json/wp/v2/posts` | WordPress REST API source. |
| `SCHEDULE_MODE` | `preset` | `preset` or `random`. |
| `INTERVALS_MIN` | `3,5,7,11,13,17,21,27` | Preset delays in minutes. |
| `MIN_INTERVAL_MIN` / `MAX_INTERVAL_MIN` | `3` / `30` | Random delay bounds. |
| `POLL_INTERVAL_MIN` | `30` | Re-check delay when no new articles. |
| `STORAGE` | `sqlite` | `sqlite` or `json`. |
| `DB_PATH` | `posted_articles.db` | Database file path. |
| `POST_WITH_IMAGE` | `true` | Attach featured image when available. |
| `HTTP_TIMEOUT` / `MAX_RETRIES` | `20` / `3` | Network tuning. |
| `MAX_POST_ATTEMPTS` | `3` | Attempts before skipping a broken article. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## Disclaimer

Automated posting must comply with **Meta's Platform Policies** and your
website's terms of service. Posting at high frequency may lead to page-level
restrictions. The default intervals are deliberately conservative.
