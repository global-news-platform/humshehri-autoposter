#!/usr/bin/env python3
"""
Humshehri Facebook Auto-Poster
================================
Fetches newly published articles from humshehri.online (WordPress REST API, with
RSS feed fallback) and posts them to a Facebook Page via the Meta Graph API
with randomized intervals to keep the posting cadence organic. Each post
shares the article's full text as the photo caption with no website link.

Run `python main.py --help` for CLI options.
"""

import argparse
import html
import io
import json
import logging
import os
import random
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GRAPH_API_VERSION = "v20.0"
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ur;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

log = logging.getLogger("humshehri_autoposter")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    facebook_page_id: str
    facebook_page_access_token: str
    rss_feed_url: str
    wp_api_url: str
    schedule_mode: str
    intervals_min: List[int]
    min_interval_min: int
    max_interval_min: int
    poll_interval_min: int
    storage: str
    db_path: Path
    post_with_image: bool
    http_timeout: int
    max_retries: int
    max_post_attempts: int
    log_level: str
    log_file: Path

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
        page_id = os.getenv("FACEBOOK_PAGE_ID", "100071825280252").strip()

        schedule_mode = (os.getenv("SCHEDULE_MODE", "") or "preset").strip().lower()
        if schedule_mode not in ("preset", "random"):
            raise ConfigError(f"Invalid SCHEDULE_MODE '{schedule_mode}' (use 'preset' or 'random')")

        try:
            intervals = [
                int(x)
                for x in (os.getenv("INTERVALS_MIN", "") or "3,5,7,11,13,17,21,27").split(",")
                if x.strip()
            ]
        except ValueError as exc:
            raise ConfigError(f"INTERVALS_MIN must be comma-separated integers: {exc}")

        return cls(
            facebook_page_id=page_id,
            facebook_page_access_token=token,
            rss_feed_url=os.getenv("RSS_FEED_URL", "https://www.humshehri.online/feed/").strip(),
            wp_api_url=os.getenv(
                "WP_API_URL", "https://www.humshehri.online/wp-json/wp/v2/posts"
            ).strip(),
            schedule_mode=schedule_mode,
            intervals_min=intervals,
            min_interval_min=_as_int("MIN_INTERVAL_MIN", 3),
            max_interval_min=_as_int("MAX_INTERVAL_MIN", 30),
            poll_interval_min=_as_int("POLL_INTERVAL_MIN", 30),
            storage=os.getenv("STORAGE", "sqlite").strip().lower(),
            db_path=BASE_DIR / os.getenv("DB_PATH", "posted_articles.db").strip(),
            post_with_image=os.getenv("POST_WITH_IMAGE", "true").strip().lower() in ("1", "true", "yes"),
            http_timeout=_as_int("HTTP_TIMEOUT", 20),
            max_retries=_as_int("MAX_RETRIES", 3),
            max_post_attempts=_as_int("MAX_POST_ATTEMPTS", 3),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            log_file=BASE_DIR / "logs" / "humshehri_autoposter.log",
        )


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got '{raw}'")


def _force_utf8_stream(stream):
    """Ensure console streams can encode non-ASCII text (e.g. Urdu titles).
    On Windows the default encoding is often cp1252, which crashes the console
    logger on the first Urdu log line."""
    if stream is None or not hasattr(stream, "buffer"):
        return stream
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            return stream
        except (AttributeError, ValueError, OSError):
            pass
    try:
        return io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace")
    except Exception:
        return stream


def setup_logging(config: Config) -> None:
    level = getattr(logging, config.log_level, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(_force_utf8_stream(sys.stdout))
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover
        log.warning("Could not set up file logging: %s", exc)


def strip_html(raw: str, max_chars: Optional[int] = None) -> str:
    text = BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)
    text = html.unescape(text)
    text = " ".join(text.split())
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


@dataclass
class Article:
    guid: str
    title: str
    summary: str
    content: str
    link: str
    image_url: Optional[str]
    published_at: Optional[str] = None

    FACEBOOK_MSG_LIMIT = 63206
    # Keep a safety buffer below Facebook's hard limit (Urdu multibyte chars
    # and newlines count differently), and keep content comfortably short.
    MAX_CAPTION_CHARS = 60000

    @property
    def facebook_caption(self) -> str:
        """Full article text (no website link) as the Facebook post body."""
        body = self.content.strip() if self.content else self.summary.strip()
        caption = self.title.strip()
        if body:
            caption = f"{caption}\n\n{body}"
        return caption[: self.MAX_CAPTION_CHARS].rstrip() + (
            "\n…" if len(caption) > self.MAX_CAPTION_CHARS else ""
        )


class Storage:
    """Persists the GUID of every posted article so nothing is ever re-posted."""

    def __init__(self, config: Config):
        self.config = config
        if config.storage == "json":
            self._backend = _JsonStorage(config.db_path.with_suffix(".json"))
        elif config.storage == "sqlite":
            self._backend = _SqliteStorage(config.db_path)
        else:
            raise ConfigError(f"Unknown STORAGE backend '{config.storage}' (use 'sqlite' or 'json')")
        self._backend.initialize()

    def is_posted(self, guid: str) -> bool:
        return self._backend.is_posted(guid)

    def mark_posted(self, guid: str, title: str, link: str) -> None:
        self._backend.mark_posted(guid, title, link)

    def increment_attempts(self, guid: str, title: str, link: str) -> int:
        return self._backend.increment_attempts(guid, title, link)

    def get_meta(self, key: str) -> Optional[str]:
        return self._backend.get_meta(key)

    def set_meta(self, key: str, value: str) -> None:
        self._backend.set_meta(key, value)

    def close(self) -> None:
        self._backend.close()


class _SqliteStorage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = None

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_articles (
                guid        TEXT PRIMARY KEY,
                title       TEXT,
                link        TEXT,
                attempts    INTEGER DEFAULT 0,
                posted_at   TEXT,
                status      TEXT DEFAULT 'pending'
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self._conn.commit()

    def is_posted(self, guid: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM posted_articles WHERE guid = ? AND status IN ('posted', 'skipped')",
            (guid,),
        ).fetchone()
        return row is not None

    def mark_posted(self, guid: str, title: str, link: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO posted_articles (guid, title, link, attempts, posted_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'posted')",
            (guid, title, link, 0, now),
        )
        self._conn.commit()

    def increment_attempts(self, guid: str, title: str, link: str) -> int:
        row = self._conn.execute(
            "SELECT attempts FROM posted_articles WHERE guid = ?", (guid,)
        ).fetchone()
        attempts = (row[0] if row else 0) + 1
        self._conn.execute(
            "INSERT INTO posted_articles (guid, title, link, attempts, posted_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'failed') "
            "ON CONFLICT(guid) DO UPDATE SET attempts = ?, title = excluded.title, link = excluded.link, "
            "status = 'failed'",
            (guid, title, link, attempts, datetime.now(timezone.utc).isoformat(), attempts),
        )
        self._conn.commit()
        return attempts

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()


class _JsonStorage:
    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, Dict] = {}

    def initialize(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        else:
            self._data = {}

    def is_posted(self, guid: str) -> bool:
        rec = self._data.get(guid)
        return bool(rec) and rec.get("status", "posted") in ("posted", "skipped")

    def mark_posted(self, guid: str, title: str, link: str) -> None:
        self._data[guid] = {
            "title": title,
            "link": link,
            "attempts": 0,
            "posted_at": datetime.now(timezone.utc).isoformat(),
            "status": "posted",
        }
        self._save()

    def increment_attempts(self, guid: str, title: str, link: str) -> int:
        rec = self._data.get(guid, {})
        attempts = int(rec.get("attempts", 0)) + 1
        self._data[guid] = {
            "title": title,
            "link": link,
            "attempts": attempts,
            "posted_at": rec.get("posted_at"),
            "status": "failed",
        }
        self._save()
        return attempts

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)

    def get_meta(self, key: str) -> Optional[str]:
        return self._data.get("_meta", {}).get(key)

    def set_meta(self, key: str, value: str) -> None:
        self._data.setdefault("_meta", {})[key] = value
        self._save()

    def close(self) -> None:
        self._save()


class ArticleFetcher:
    """Fetches new articles, trying the RSS feed first and falling back to the
    WordPress REST API (more reliable on this site) when the feed is empty."""

    def __init__(self, config: Config, storage: Storage):
        self.config = config
        self.storage = storage
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    def fetch_new_articles(self, limit: int = 10) -> List[Article]:
        candidates = self._fetch_via_rss(limit)
        if not candidates:
            candidates = self._fetch_via_wp_api(limit)
        if not candidates:
            log.info("No articles returned by any source")
            return []

        articles = [a for a in candidates if not self.storage.is_posted(a.guid)]
        log.info("Fetched %d candidate(s), %d new (not yet posted)", len(candidates), len(articles))
        for a in articles:
            log.info(
                "  -> NEW  [%s] %s", a.guid, a.title
            )
        return articles

    def _fetch_via_rss(self, limit: int) -> List[Article]:
        if feedparser is None:
            return []
        try:
            feed = feedparser.parse(self.config.rss_feed_url)
        except Exception as exc:  # pragma: no cover
            log.warning("RSS feed parsing failed (%s); falling back to WP REST API", exc)
            return []
        entries = getattr(feed, "entries", [])[:limit]
        if not entries:
            log.info("RSS feed returned no entries; falling back to WP REST API")
            return []
        log.info("Fetched %d article(s) from RSS feed", len(entries))
        articles: List[Article] = []
        for entry in entries:
            guid = entry.get("id") or entry.get("link") or ""
            if not guid:
                continue
            summary = strip_html(entry.get("summary", ""), max_chars=400)
            media = entry.get("media_content") or entry.get("media_thumbnail") or []
            image_url = media[0].get("url") if media else None
            articles.append(
                Article(
                    guid=guid,
                    title=strip_html(entry.get("title", "")),
                    summary=summary,
                    content=strip_html(entry.get("summary", ""), max_chars=5000),
                    link=entry.get("link", ""),
                    image_url=image_url,
                    published_at=entry.get("published", entry.get("updated")),
                )
            )
        return articles

    def _fetch_via_wp_api(self, limit: int) -> List[Article]:
        url = f"{self.config.wp_api_url}?per_page={limit}&_fields=" + ",".join(
            ["id", "date", "link", "title", "excerpt", "content", "featured_media"]
        )
        data = self._request_json(url)
        if not data:
            return []
        log.info("Fetched %d article(s) from WP REST API", len(data))
        articles: List[Article] = []
        for post in data:
            pid = post.get("id")
            if not pid:
                continue
            image_url = self._resolve_featured_image(post.get("featured_media"))
            articles.append(
                Article(
                    guid=str(pid),
                    title=strip_html(post.get("title", {}).get("rendered", "")),
                    summary=strip_html(post.get("excerpt", {}).get("rendered", ""), max_chars=400),
                    content=strip_html(post.get("content", {}).get("rendered", ""), max_chars=5000),
                    link=post.get("link", ""),
                    image_url=image_url,
                    published_at=post.get("date_gmt"),
                )
            )
        return articles

    def _resolve_featured_image(self, media_id: Optional[int]) -> Optional[str]:
        if not media_id:
            return None
        url = f"https://www.humshehri.online/wp-json/wp/v2/media/{media_id}?_fields=source_url"
        try:
            data = self._request_json(url)
            if data:
                return data.get("source_url")
        except Exception as exc:  # pragma: no cover
            log.warning("Could not resolve featured image %s: %s", media_id, exc)
        return None

    def _request_json(self, url: str) -> Optional[dict]:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=self.config.http_timeout)
                if resp.status_code == 406:
                    raise requests.HTTPError(
                        f"HTTP 406 (WAF blocked); will retry with a fresh request (attempt {attempt})"
                    )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    backoff = 2 ** attempt
                    log.warning(
                        "Request failed (%s); retrying in %ds (attempt %d/%d)",
                        exc, backoff, attempt, self.config.max_retries,
                    )
                    time.sleep(backoff)
        log.error("Request to %s failed after %d attempts: %s", url, self.config.max_retries, last_error)
        return None

    def close(self) -> None:
        self.session.close()


class FacebookPoster:
    """Publishes articles to the Facebook Page via the Meta Graph API."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()

    def post(self, article: Article) -> str:
        # Only posts articles that have a featured image — articles without an
        # image are skipped entirely (no plain-text/link post).
        if not article.image_url:
            raise ValueError("Article has no image; skipping (per configuration)")
        return self._post_photo(article)

    def _post_photo(self, article: Article) -> str:
        url = f"{GRAPH_BASE_URL}/{self.config.facebook_page_id}/photos"
        params = {
            "url": article.image_url,
            "caption": article.facebook_caption,
            "access_token": self.config.facebook_page_access_token,
        }
        return self._graph_request(url, params, "photo")

    def _graph_request(self, url: str, params: Dict[str, str], kind: str) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self.session.post(url, data=params, timeout=self.config.http_timeout)
                payload = resp.json() if resp.text else {}
                if not resp.ok:
                    err = payload.get("error", {})
                    raise requests.HTTPError(
                        f"Graph API error {err.get('code')}: {err.get('message')}"
                    )
                post_id = payload.get("id") or payload.get("post_id")
                if not post_id:
                    raise requests.HTTPError(f"Graph API returned no id: {payload}")
                return str(post_id)
            except Exception as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    backoff = 2 ** attempt
                    log.warning(
                        "Graph API %s post failed (%s); retrying in %ds (attempt %d/%d)",
                        kind, exc, backoff, attempt, self.config.max_retries,
                    )
                    time.sleep(backoff)
        raise last_error  # type: ignore[misc]

    def close(self) -> None:
        self.session.close()


class Scheduler:
    """Main execution loop with randomized intervals between posts."""

    def __init__(
        self,
        config: Config,
        fetcher: ArticleFetcher,
        poster: FacebookPoster,
        storage: Storage,
        *,
        no_delay: bool = False,
        max_posts: int = 0,
        cron_mode: bool = False,
    ):
        self.config = config
        self.fetcher = fetcher
        self.poster = poster
        self.storage = storage
        self.no_delay = no_delay
        self.max_posts = max_posts
        self.cron_mode = cron_mode
        self._shutdown = False

    def run_once(self, dry_run: bool = False) -> int:
        if self.cron_mode:
            next_ts = self._next_post_allowed_ts()
            if next_ts is not None and time.time() < next_ts:
                log.info(
                    "Cadence gate: next post not before %s; skipping this run",
                    datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M:%S"),
                )
                return 0

        new_articles = self.fetcher.fetch_new_articles()
        if not new_articles:
            return 0

        posted_count = 0
        for article in new_articles:
            if self._shutdown:
                break
            if self.max_posts and posted_count >= self.max_posts:
                break
            log.info("Posting article [%s]: %s", article.guid, article.title)
            try:
                if not article.image_url:
                    log.info(
                        "  SKIPPED [%s]: article has no image (only image articles are posted)",
                        article.guid,
                    )
                    self.storage.mark_posted(article.guid, article.title, article.link)
                    posted_count += 1
                    if self.cron_mode:
                        self._set_cadence_gate()
                    continue
                if dry_run:
                    log.info("  [DRY RUN] would post: %s", article.facebook_caption.replace("\n", " | "))
                else:
                    post_id = self.poster.post(article)
                    log.info("  SUCCESS - posted [%s] -> Facebook post id %s", article.guid, post_id)
                    self.storage.mark_posted(article.guid, article.title, article.link)
                posted_count += 1
                if self.cron_mode:
                    self._set_cadence_gate()
            except Exception as exc:
                log.error("  FAILED to post [%s]: %s", article.guid, exc)
                attempts = self.storage.increment_attempts(article.guid, article.title, article.link)
                if attempts >= self.config.max_post_attempts:
                    log.warning(
                        "  Giving up on [%s] after %d attempts; marking as skipped",
                        article.guid, attempts,
                    )
                    self.storage.mark_posted(article.guid, article.title, article.link)

            if posted_count > 0 and article is not new_articles[-1] and not self.no_delay:
                self._sleep_random_delay()

        return posted_count

    def _next_post_allowed_ts(self) -> Optional[float]:
        raw = self.storage.get_meta("next_post_allowed_at")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _set_cadence_gate(self) -> None:
        delay_sec = self._pick_delay()
        self.storage.set_meta("next_post_allowed_at", str(time.time() + delay_sec))
        log.info(
            "Cadence gate armed: next post in %d min %02d sec",
            delay_sec // 60, delay_sec % 60,
        )

    def run_forever(self, dry_run: bool = False) -> None:
        log.info("Starting humshehri Facebook auto-poster (mode=%s, dry_run=%s)",
                 self.config.schedule_mode, dry_run)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        while not self._shutdown:
            try:
                posted = self.run_once(dry_run)
                if self._shutdown:
                    break
                if posted == 0:
                    delay = self.config.poll_interval_min * 60
                    log.info(
                        "No new articles found; next check in %d min %02d sec",
                        self.config.poll_interval_min, 0,
                    )
                    self._sleep_seconds(delay)
            except KeyboardInterrupt:
                break
            except Exception as exc:  # pragma: no cover
                log.exception("Unexpected error in main loop: %s", exc)
                self._sleep_seconds(min(300, max(60, self.config.poll_interval_min * 60)))
        log.info("Shutting down cleanly")

    def _sleep_random_delay(self) -> None:
        delay_sec = self._pick_delay()
        log.info(
            "Next post in %d min %02d sec",
            delay_sec // 60, delay_sec % 60,
        )
        self._sleep_seconds(delay_sec)

    def _pick_delay(self) -> int:
        if self.config.schedule_mode == "preset" and self.config.intervals_min:
            minutes = random.choice(self.config.intervals_min)
        else:
            minutes = random.randint(self.config.min_interval_min, self.config.max_interval_min)
        return minutes * 60

    def _sleep_seconds(self, seconds: int) -> None:
        step = 1.0
        remaining = seconds
        while remaining > 0 and not self._shutdown:
            time.sleep(min(step, remaining))
            remaining -= step

    def _handle_signal(self, *_args) -> None:  # type: ignore[no-untyped-def]
        log.info("Signal received; finishing current post then stopping…")
        self._shutdown = True


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auto-post humshehri.online articles to a Facebook Page."
    )
    parser.add_argument("--once", action="store_true",
                        help="Fetch and post all new articles once, then exit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and log articles/posts without actually posting to Facebook.")
    parser.add_argument("--list-new", action="store_true",
                        help="Only list new articles that would be posted, then exit.")
    parser.add_argument("--cron", action="store_true",
                        help="Cron-friendly mode: persist the randomized cadence between runs "
                             "(combine with --once and --max-posts 1, e.g. on GitHub Actions).")
    parser.add_argument("--max-posts", type=int, default=0,
                        help="Post at most N articles per run (0 = unlimited).")
    parser.add_argument("--no-delay", action="store_true",
                        help="Do not sleep between posts within a single run.")
    return parser


def main() -> int:
    args = build_cli().parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config)

    if not config.facebook_page_access_token and not (args.dry_run or args.list_new):
        log.error(
            "FACEBOOK_PAGE_ACCESS_TOKEN is not set. "
            "Copy .env.example to .env and fill in your token, "
            "or run with --dry-run / --list-new for a safe test."
        )
        return 1

    storage = Storage(config)
    fetcher = ArticleFetcher(config, storage)
    poster = FacebookPoster(config)
    scheduler = Scheduler(
        config,
        fetcher,
        poster,
        storage,
        no_delay=args.no_delay,
        max_posts=args.max_posts,
        cron_mode=args.cron,
    )

    try:
        if args.list_new:
            articles = fetcher.fetch_new_articles()
            for a in articles:
                print(f"[{a.guid}] {a.title}\n    {a.link}\n    image: {a.image_url}\n")
            return 0
        if args.once:
            scheduler.run_once(dry_run=args.dry_run)
            return 0
        scheduler.run_forever(dry_run=args.dry_run)
        return 0
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 0
    finally:
        fetcher.close()
        poster.close()
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
