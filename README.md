# Humshehri Facebook Auto-Poster

A production-ready Python bot that automatically fetches newly published
articles from **[humshehri.online](https://humshehri.online/)** and posts
them to the **Humshehri Facebook Page** with **randomized, human-feeling
intervals** so your page never looks spammy. Each post is an **exact copy** of
a published article: the **original title** and the **complete original body**
(Urdu/English text, punctuation, numbers, links and paragraph breaks preserved
verbatim) attached as the caption to the article's **own image** — with **no AI
rewriting**, **no truncation** and **no logo
fallback**. The full article is shared on the page, not a link back to the
site. When hashtags are enabled they are appended **after** the article text
as a separate layer — they never alter the exact copy.

The bot parses the site's RSS feed **and** its WordPress REST API (the API runs
alongside the feed, so articles the feed misses or mis-numbers are still found)
to discover articles, then opens each article's **full page** to copy the exact
title, body and featured image. It posts each article to Facebook via the
**Meta Graph API** with the article's own image attached and the full text as
the caption, from one authoritative source.

---

## Features

- **Two discovery sources, merged** — the RSS feed **and** the WordPress REST
  API are both fetched on every run (the pretty `/feed/` and `/wp-json/` paths
  on humshehri.online are 404-protected by its server, so the bot fetches the
  query-parameter equivalents `?feed=rss2` and `?rest_route=/wp/v2/posts`
  instead, and its guids are sometimes stale/mis-numbered, so the
  bot never trusts the feed alone). Candidates are merged by post id / URL; the
  **real post id, canonical URL and title are resolved from the article page
  itself** after redirects, so a mis-numbered feed entry can never publish the
  wrong headline. The same article arriving from both sources is processed
  once.
- **Exact content-fingerprint dedupe** — every candidate's content is reduced to
  a SHA-256 fingerprint of its normalized title + body (whitespace/case only,
  never fuzzy). WordPress "-2" duplicate copies of the same story are detected
  purely by that fingerprint, so the same article is never reposted under a
  different post id.
- **Exact-copy extraction from the full article page** — the article's own page
  is the authoritative source. The bot opens it, finds the article container
  (`.entry-content` etc., excluding navigation, menus, sidebars, ad blocks and
  the site header/footer) and copies the **original title + full body
  verbatim**: paragraphs, heading levels, lists, Urdu + English text, numbers,
  punctuation and article URLs are all preserved. Nothing is rewritten,
  summarized or truncated. Hashtags, when enabled, are appended only after the
article text — never inserted between paragraphs.
- **Robust image extraction** — resolves each article's featured image with a
  fallback chain: RSS/media → `og:image` → JSON-LD → `twitter:image` → article
  `<img>`. If the feed has no image, the bot opens the article page itself to
  find the real photo (logos/pixels/ads are filtered out). The image and the
  text always come from the **same article**.
- **Native photo posts, no link previews** — the bot **downloads the image
  file** and uploads the actual bytes to Facebook's `/photos` endpoint
  (multipart `source`). Facebook never fetches the website, so there is no
  `Invalid image url` failure, no link preview and no auto-added link. WebP
  images are re-encoded to JPEG in memory if Pillow is installed.
- **No rewriting, no truncation** — the caption is exactly `title + "\n\n" +
  body`. URLs inside the article text are preserved; only the website's own
  navigation/chrome is excluded. There is no 5000-character cap; as a safety
  measure an article whose full text would exceed Facebook's hard limit
  (~63,206 characters) is skipped rather than silently cut off.
- **Relevant hashtags appended, exact copy preserved** — `ADD_HASHTAGS=true`
  (the default) appends a 3-7 hashtag mix of broad category and specific topic
  tags (Urdu + English, keyword-matched to the article) **after the original
  text**, on their own line. Hashtags are sanitized (no spaces/URLs/duplicates
  or stray prose), deduplicated case-insensitively, and dropped one at a time
  if the combined caption nears Facebook's character limit — the article text
  is never truncated, and if the article alone exceeds the platform limit the
  post fails safely. Cap the block with `MAX_HASHTAGS`.
- **Own image required, never a logo fallback** — every post uses the article's
  **own image**. By default (`REQUIRE_ARTICLE_IMAGE=true`) an article whose
  image cannot be found or downloaded is safety-skipped — it is **never** posted
  image-less and the brand logo is **never** used as a substitute. Set
  `REQUIRE_ARTICLE_IMAGE=false` if you'd rather such articles go out as
  text-only posts.
- **Duplicate prevention** — posted article IDs/GUIDs are stored in SQLite
  (or JSON); nothing is ever re-posted, even across restarts.
- **Randomized scheduling** — pick a random delay from a preset list
  (`3, 5, 7, 11, 13, 17, 21, 27` minutes) **or** a random integer between 3 and
  30 minutes.
- **Robust error handling** — automatic retries with exponential backoff,
  photo-post failures are retried, permanently broken articles are skipped
  after a configurable number of attempts.
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

`--dry-run` also downloads each article's image (without posting) and prints a
per-article diagnostic block showing the canonical URL, source, extraction
method, title, text/paragraph counts, chosen image and the exact caption
(start and end), so you can confirm the copy is exact before it ever goes
live.

### Test extraction on a real article (no Facebook call, no posting)

```bash
python main.py --test-extraction https://www.humshehri.online/?p=1327
```

Reports the canonical URL, page title, body length/paragraph count, the first
12 lines and every image candidate found for the URL — RSS/media if present,
then `og:image`, `twitter:image`, JSON-LD and the article's `<img>` — and
test-downloads each one. Pass several URLs to check multiple articles.
`python tests/test_extraction.py`, `python tests/test_image_pipeline.py` and
`python tests/test_hashtags.py` run offline suites covering exact-copy text
extraction, image priority, no-logo-fallback, hashtag generation/sanitization
and hashtag appending after the exact article
behavior.

### Post everything currently un-posted once, then exit

```bash
python main.py --once
```

### Run continuously (recommended for production)

```bash
python main.py
```

The bot now runs forever:

1. Fetches new articles from humshehri.online.
2. Posts each one as a native photo with the article's **exact copy** as the
   caption (original title + full body, no website link, no rewriting, no URL
   scrubbing), using the article's **own image** — never a logo fallback (see
   `REQUIRE_ARTICLE_IMAGE`).
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
| `(#100) Invalid image url` | Older versions passed an image *URL* to Facebook. Today the bot downloads the image itself and uploads the file, so this is no longer produced. If a download fails, the bot logs the reason and moves to the next image candidate. |
| Article skipped but it has an image on the site | Strict mode (`PREFER_FULL_ARTICLE_PAGE`/`STRICT_ARTICLE_EXTRACTION`/`REQUIRE_ARTICLE_IMAGE`) skips any article whose full page text or own image can't be verified, rather than posting wrong/partial content. Check the log for the exact extraction/validation reason. |
| Article not appearing on the page | The bot may be mid-interval. Wait for the next randomized post time or check the log file. |
| Duplicate posts | Should not happen — the SQLite `posted_articles` table prevents it. Keep `posted_articles.db` in the project folder. |
| Caption too long for Facebook | The full text of any article larger than Facebook's ~63,206-character message limit is skipped whole (never truncated). |

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `FACEBOOK_PAGE_ID` | `100071825280252` | Numeric Page ID. |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | *(empty)* | Non-expiring Page access token. |
| `RSS_FEED_URL` | `https://humshehri.online/?feed=rss2` | RSS discovery source. |
| `WP_API_URL` | `https://humshehri.online/?rest_route=/wp/v2/posts` | WordPress REST API discovery source (runs alongside RSS). |
| `ENABLE_WP_API_DISCOVERY` | `true` | Fetch candidates from the WP API too (not just as an RSS fallback). |
| `WP_API_LOOKBACK` | `20` | API `per_page` lookback for supplementary discovery. |
| `SCHEDULE_MODE` | `preset` | `preset` or `random`. |
| `INTERVALS_MIN` | `3,5,7,11,13,17,21,27` | Preset delays in minutes. |
| `MIN_INTERVAL_MIN` / `MAX_INTERVAL_MIN` | `3` / `30` | Random delay bounds. |
| `POLL_INTERVAL_MIN` | `30` | Re-check delay when no new articles. |
| `STORAGE` | `sqlite` | `sqlite` or `json`. |
| `DB_PATH` | `posted_articles.db` | Database file path. |
| `POST_WITH_IMAGE` | `true` | Post each article as a native photo (image + exact full-text caption). No website link, no link preview. |
| `REQUIRE_ARTICLE_IMAGE` | `true` | Publish only when the article's own image is usable; otherwise safety-skip (`false` → post text-only if the image is missing). |
| `USE_AI_REWRITING` | `false` | Enforced behavior — article text is never rewritten. Kept for clarity/forward-compat. |
| `PREFER_FULL_ARTICLE_PAGE` | `true` | Copy text straight from the article's full page; `false` uses the RSS/API excerpt. |
| `STRICT_ARTICLE_EXTRACTION` | `true` | Fail-safe: skip when the page can't be fetched or the text is too short (navigation-like) instead of posting wrong/empty text. |
| `MIN_ARTICLE_CHARS` | `120` | Minimum body length (chars) required in strict mode. |
| `ADD_HASHTAGS` | `true` | Append keyword-relevant (Urdu + English) hashtags **after** the exact article text on their own line. The article itself is never modified. |
| `MAX_HASHTAGS` | `7` | Maximum hashtags per post when enabled (3-7 performs best; excess is trimmed, not the article). |
| `HTTP_TIMEOUT` / `MAX_RETRIES` | `20` / `3` | Network tuning. |
| `MAX_POST_ATTEMPTS` | `3` | Attempts before skipping a broken article. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## Disclaimer

Automated posting must comply with **Meta's Platform Policies** and your
website's terms of service. Posting at high frequency may lead to page-level
restrictions. The default intervals are deliberately conservative.
