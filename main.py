#!/usr/bin/env python3
"""
Humshehri Facebook Auto-Poster
================================
Fetches newly published articles from humshehri.online (WordPress REST API +
RSS feed discovery, merged) and posts them to a Facebook Page via the Meta
Graph API with randomized intervals to keep the posting cadence organic.

Discovery combines the RSS feed with the WordPress REST API (which runs
alongside RSS, so articles the feed misses or mis-numbers are still found).
Candidates are merged by post id / URL, real post id and canonical URL are
resolved from the article page itself, and duplicate WordPress copies are
caught with an exact content fingerprint (SHA-256 of the normalized title +
body) — nothing is ever reposted under a different post id.

Each post reproduces the source article EXACTLY: the original title and the
original body text (paragraph order, Urdu/English, numbers, punctuation) are
extracted from the actual article page and posted as the photo caption, along
with the article's own image, downloaded and uploaded as a real file. No AI
rewriting, summarising, translation, shortening or URL scrubbing; no logo or
unrelated fallback image. When enabled, relevant hashtags are generated from
curated topic rules and appended as a separate layer AFTER the exact article
text — they never alter the title, body or image.

Run `python main.py --help` for CLI options.
"""

import argparse
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import signal
import unicodedata
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GRAPH_API_VERSION = "v25.0"
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
    fallback_image_url: str
    add_hashtags: bool
    max_hashtags: int
    require_article_image: bool
    use_ai_rewriting: bool
    prefer_full_article_page: bool
    strict_article_extraction: bool
    min_article_chars: int
    enable_wp_api_discovery: bool
    wp_api_lookback: int
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
            rss_feed_url=os.getenv("RSS_FEED_URL", "https://humshehri.online/?feed=rss2").strip(),
            wp_api_url=os.getenv(
                "WP_API_URL", "https://humshehri.online/?rest_route=/wp/v2/posts"
            ).strip(),
            schedule_mode=schedule_mode,
            intervals_min=intervals,
            min_interval_min=_as_int("MIN_INTERVAL_MIN", 3),
            max_interval_min=_as_int("MAX_INTERVAL_MIN", 30),
            poll_interval_min=_as_int("POLL_INTERVAL_MIN", 30),
            storage=os.getenv("STORAGE", "sqlite").strip().lower(),
            db_path=BASE_DIR / os.getenv("DB_PATH", "posted_articles.db").strip(),
            post_with_image=os.getenv("POST_WITH_IMAGE", "true").strip().lower() in ("1", "true", "yes"),
            fallback_image_url=os.getenv(
                "FALLBACK_IMAGE_URL",
                "https://humshehri.online/wp-content/uploads/2026/08/logo-humshehri.png",
            ).strip(),
            add_hashtags=_as_bool("ADD_HASHTAGS", True),
            max_hashtags=_as_int("MAX_HASHTAGS", 7),
            require_article_image=_as_bool("REQUIRE_ARTICLE_IMAGE", True),
            use_ai_rewriting=_as_bool("USE_AI_REWRITING", False),
            prefer_full_article_page=_as_bool("PREFER_FULL_ARTICLE_PAGE", True),
            strict_article_extraction=_as_bool("STRICT_ARTICLE_EXTRACTION", True),
            min_article_chars=_as_int("MIN_ARTICLE_CHARS", 120),
            enable_wp_api_discovery=_as_bool("ENABLE_WP_API_DISCOVERY", True),
            wp_api_lookback=_as_int("WP_API_LOOKBACK", 20),
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


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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


_IMAGE_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

_HOST_RE = re.compile(r'^(https?://)([^/]+)(/.*)?$', re.IGNORECASE)


def _alternate_host_url(url: str) -> Optional[str]:
    """Return a variant of url switching between `www.` and the bare host.

    The WordPress site is served from both `www.humshehri.online` and
    `humshehri.online`, and occasionally one host is unreachable from a given
    network while the other works. Trying both makes fetching robust.
    """
    m = _HOST_RE.match(url)
    if not m:
        return None
    scheme, host, path = m.group(1), m.group(2), m.group(3) or ""
    if host.lower().startswith("www."):
        new_host = host[4:]
    else:
        new_host = "www." + host
    if new_host.lower() == host.lower():
        return None
    return f"{scheme}{new_host}{path}"


def _first_image_url(raw: str) -> Optional[str]:
    """Return the first <img> URL in an HTML string, if any."""
    if not raw:
        return None
    match = _IMAGE_SRC_RE.search(raw)
    if match:
        src = html.unescape(match.group(1)).strip()
        return src or None
    return None


# --- Robust image extraction -------------------------------------------------
# The image and the article text are two fully separate pieces of data. The
# article text (title + body) becomes the Facebook caption and the *image file*
# is uploaded to Facebook. We never hand Facebook a source-URL to fetch.

_IMAGE_BLOCKLIST_KEYWORDS = (
    "logo", "avatar", "favicon", "1x1", "1px", "pixel", "spacer",
    "placeholder", "transparent", "sprite", "advert", "/ads/", "tracking",
    # IAB standard web-ad banner dimensions (leaderboard, medium rectangle,
    # skyscraper, etc.) so ad slots are never mistaken for the article photo.
    "728x90", "970x250", "300x250", "300x300", "320x50",
    "160x600", "468x60", "336x280", "120x600", "leaderboard", "banner",
)

_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # Meta's photo upload size limit

# Meta's documented maximum length of a photo caption / post message. The bot
# never silently truncates an article: if the caption exceeds this limit the
# article fails safely and a clear reason is logged.
FACEBOOK_MSG_LIMIT = 63206


def _normalize_image_src(src: Optional[str], base_url: str) -> Optional[str]:
    """Resolve protocol-relative and root-relative image URLs to absolute."""
    src = (src or "").strip()
    if not src or src.lower().startswith("data:"):
        return None
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = urljoin(base_url, src)
    if not re.match(r"^https?://", src, re.IGNORECASE):
        return None
    return src


def _looks_like_real_photo(url: Optional[str], img=None) -> bool:
    """Heuristic so logos, avatars, tracking pixels and ad sprites are not
    mistaken for the article's actual image."""
    url = (url or "").lower()
    if not url or url.startswith("data:"):
        return False
    path = url.split("?")[0].split("#")[0]
    if path.endswith(".svg"):
        return False
    if any(k in url for k in _IMAGE_BLOCKLIST_KEYWORDS):
        return False
    if img is not None:
        try:
            w, h = img.get("width"), img.get("height")
            if w and h and int(w) < 80 and int(h) < 80:
                return False
        except (TypeError, ValueError):
            pass
    return True


def _rss_image_candidates(entry, raw_content: str, base_url: str) -> List[str]:
    """Collect image candidates that live in the feed itself
    (media:content / media:thumbnail / enclosure / <img> in the body)."""
    out: List[str] = []

    def add(src: Optional[str]) -> None:
        src = _normalize_image_src(src, base_url)
        if src and _looks_like_real_photo(src) and src not in out:
            out.append(src)

    media = entry.get("media_content") or entry.get("media_thumbnail") or []
    if isinstance(media, dict):
        media = [media]
    for item in media:
        if isinstance(item, dict):
            kind = str(item.get("type", "")).lower()
            medium = str(item.get("medium", "")).lower()
            if not kind or kind.startswith("image/") or medium == "image":
                add(item.get("url"))
    for enc in entry.get("enclosures") or []:
        if isinstance(enc, dict) and str(enc.get("type", "")).startswith("image/"):
            add(enc.get("href") or enc.get("url"))
    img = entry.get("image")
    if isinstance(img, str):
        add(img)
    elif isinstance(img, dict):
        add(img.get("url"))
    if not out:
        add(_first_image_url(raw_content or ""))
    return out


def _is_descendant(node, ancestor) -> bool:
    node = getattr(node, "parent", None)
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _ranked_article_images(soup, base_url: str) -> List[Tuple[int, object, str, bool]]:
    """Rank <img> elements by how likely they are the article's main image.
    Each item is (score, img, src, in_article)."""
    all_imgs = soup.find_all("img")
    positions = {id(img): i for i, img in enumerate(all_imgs)}
    container = soup.find("article") or soup.find("main")
    scored: List[Tuple[int, object, str, bool]] = []
    for img in all_imgs:
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        src = _srcset_url(img.get("srcset"), src)
        src = _normalize_image_src(src, base_url)
        if not src or not _looks_like_real_photo(src, img):
            continue
        classes = set(img.get("class") or [])
        src_lower = src.lower()
        in_article = _is_descendant(img, container) if container is not None else False
        is_wp_upload = "/wp-content/uploads/" in src_lower
        has_signal_class = any(
            k in c for c in classes
            for k in ("wp-post-image", "attachment-", "featured", "post-image",
                      "entry-image", "alignnone", "aligncenter", "size-full", "hero")
        )
        score = int(in_article) + int(is_wp_upload) + int(has_signal_class)
        scored.append((score, img, src, in_article))
    scored.sort(key=lambda t: (-t[0], positions.get(id(t[1]), len(scored))))
    return scored


def _srcset_url(srcset: Optional[str], fallback: str) -> str:
    """Pick the largest candidate from an img srcset attribute."""
    if not srcset:
        return fallback
    best = ""
    best_w = 0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        try:
            w = int(bits[1].rstrip("w")) if len(bits) > 1 and bits[1].endswith("w") else 0
        except ValueError:
            w = 0
        if w > best_w:
            best_w, best = w, url
    return best or fallback


def _iter_jsonld_objects(data):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _iter_jsonld_objects(value)
    elif isinstance(data, (list, tuple)):
        for item in data:
            yield from _iter_jsonld_objects(item)


def _extract_html_image_candidates(html_text: str, base_url: str,
                                   article_only: bool = False) -> List[Tuple[str, str]]:
    """Extract ordered image candidates from the article page itself.

    Order: og:image → JSON-LD image → twitter:image → main article <img> →
    other plausible <img>. Each candidate is a (url, source-label) pair.

    When ``article_only`` is True, only images that truly belong to the article
    are returned (og/twitter/JSON-LD, or an <img> inside the article container);
    unrelated sidebar/related-news/logo/ad thumbnails are excluded.
    """
    soup = BeautifulSoup(html_text or "", "html.parser")
    out: List[Tuple[str, str]] = []

    def add(src: Optional[str], label: str) -> None:
        src = _normalize_image_src(src, base_url)
        if not src or not _looks_like_real_photo(src):
            return
        if any(url == src for url, _ in out):
            return
        out.append((src, label))

    def jsonld_images() -> List[str]:
        urls: List[str] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = (script.string or script.get_text() or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            for node in _iter_jsonld_objects(data):
                img = node.get("image") if isinstance(node, dict) else None
                if isinstance(img, str):
                    urls.append(img)
                elif isinstance(img, dict):
                    urls.append(img.get("contentUrl") or img.get("url"))
                elif isinstance(img, list):
                    for item in img:
                        if isinstance(item, str):
                            urls.append(item)
                        elif isinstance(item, dict):
                            urls.append(item.get("contentUrl") or item.get("url"))
        return urls

    # Open Graph image
    for meta in soup.find_all("meta"):
        props = str(meta.get("property") or "").lower()
        name = str(meta.get("name") or "").lower()
        if props.startswith("og:image") or name.startswith("og:image"):
            add(meta.get("content"), "og:image")
    # JSON-LD image fields (second priority)
    for src in jsonld_images():
        add(src, "json-ld:image")
    # Twitter card image
    for meta in soup.find_all("meta", attrs={"name": "twitter:image"}):
        add(meta.get("content"), "twitter:image")
    for meta in soup.find_all("meta", attrs={"property": "twitter:image"}):
        add(meta.get("content"), "twitter:image")

    # Main article <img> elements (only inside the article container when
    # article_only), then any other plausible <img> for the non-article_only
    # case.
    ranked = _ranked_article_images(soup, base_url)
    in_article_imgs = [t for t in ranked if t[3]]
    if article_only:
        for _score, _img, src, _in in in_article_imgs:
            add(src, "article:<img>")
        return out
    for score, _img, src, _in in ranked:
        if score > 0:
            add(src, "article:<img>")
    for score, _img, src, _in in ranked:
        if score == 0:
            add(src, "article:<img>")
    return out


# --- Exact article-text extraction -----------------------------------------
# The full article page is the authoritative source. We identify the actual
# article container and reproduce its text as faithfully as possible: same
# paragraphs, headings, lists, numbers, quotes, Urdu and English — no
# rewriting, summarising, shortening, translation, hashtagging or URL
# scrubbing of genuine article content. HTML markup is dropped only because
# Facebook needs plain text; the written content itself is untouched.

_BLOCK_TAGS = {
    "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "figure", "figcaption", "table",
    "thead", "tbody", "tfoot", "tr", "td", "th", "dl", "dt", "dd",
    "details", "summary", "hr", "address",
}

_NOISE_TAG_NAMES = {
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "form", "button", "input", "select", "textarea", "iframe", "label",
    "svg", "canvas", "template", "object", "embed", "ins", "video", "audio",
    "figure", "picture", "map", "area",
}

# Class/id keywords for UI chrome (menus, widgets, ads, social, comments,
# cookies, related stories...) that must never leak into the article text.
_NOISE_ATTR_KEYWORDS = (
    "menu", "navigation", "nav-", "cookie", "newsletter", "subscribe",
    "social", "share", "sharing", "related", "recommend", "comment",
    "comment-", "sidebar", "widget", "advert", "ad-", "-ad", "ads",
    "adslot", "googlead", "sponsored", "promo", "promotion", "breadcrumb",
    "pagination", "byline", "author-box", "author-info", "author_-",
    "tag-cloud", "tags-links", "popular", "trending", "categories", "rand-",
    "colophon", "footer-", "header-", "secondary", "sidebar-", "skip",
    "screen-reader", "visually-hidden", "hidden", "block-editor",
)


def _is_noise_element(tag) -> bool:
    """True when a tag is website furniture rather than article content."""
    if not isinstance(tag, Tag):
        return False
    name = (tag.name or "").lower()
    if name in _NOISE_TAG_NAMES:
        return True
    attrs = " ".join(
        [str(tag.get("id") or ""), " ".join(tag.get("class") or [])]
    ).lower()
    for kw in _NOISE_ATTR_KEYWORDS:
        if kw in attrs:
            return True
    return False


def _pick_largest_container(elements: List[Tag]) -> Optional[Tag]:
    best, best_len = None, -1
    for el in elements:
        length = len(el.get_text(" ", strip=True))
        if length > best_len:
            best, best_len = el, length
    return best


def _find_article_container(soup) -> Optional[Tag]:
    """Locate the element holding the article's own text.

    Specific article-content containers (`.entry-content`, `.post-content`,
    `itemprop=articleBody`, ...) are preferred so the <h1> title and byline
    outside them never leak into the body. Only when none exist do we widen
    to semantic <article>/<main> and finally to <body> with every noise node
    removed by ``_block_text``. The largest candidate wins so a nested short
    excerpt div never wins over the full story.
    """
    specific_selectors = [
        "div.entry-content",
        "div.post-content",
        "div.article-content",
        "div.post-body",
        "div.single-content",
        "div.story-content",
        "div[itemprop='articleBody']",
        "div[itemprop='ArticleBody']",
    ]
    candidates: List[Tag] = []
    for sel in specific_selectors:
        for el in soup.select(sel):
            if el not in candidates:
                candidates.append(el)
    if not candidates:
        for tag in soup.find_all(["div", "section"]):
            cls = " ".join(tag.get("class") or []).lower()
            if any(k in cls for k in (
                    "entry-content", "post-content", "article-content",
                    "post-body", "single-content", "story-body", "article-body",)):
                if tag not in candidates:
                    candidates.append(tag)
    if candidates:
        return _pick_largest_container(candidates)

    semantic_selectors = ["article", "main", "div[role='main']"]
    for sel in semantic_selectors:
        for el in soup.select(sel):
            if el not in candidates:
                candidates.append(el)
    if candidates:
        return _pick_largest_container(candidates)
    return soup.find("body")


def _trim_text(text: str) -> str:
    text = (text or "").replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t\r\x0c]+", " ", text)
    return text.strip()


def _inline_text(el) -> str:
    """Text of an inline run, flattening nested inline tags; ``<br>`` becomes
    a newline so forced line breaks survive."""
    parts: List[str] = []
    for child in el.children:
        if isinstance(child, NavigableString):
            text = str(child).replace("\u00a0", " ").replace("\u200b", "")
            if text:
                parts.append(text)
        elif isinstance(child, Tag):
            if child.name == "br":
                parts.append("\n")
            elif child.name in ("script", "style", "noscript") or _is_noise_element(child):
                continue
            elif child.name in _BLOCK_TAGS:
                parts.append("\n\n")
                parts.append(_block_text(child))
            else:
                parts.append(_inline_text(child))
    return "".join(parts)


def _list_text(el) -> str:
    lines: List[str] = []
    for child in el.children:
        if isinstance(child, Tag) and child.name == "li":
            inner = _trim_text(_block_text(child))
            if inner:
                lines.append(inner)
    return "\n".join(lines)


def _table_text(el) -> str:
    rows: List[str] = []
    for tr in el.find_all("tr"):
        cells = [
            _trim_text(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])
        ]
        cells = [c for c in cells if c]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _block_text(el) -> str:
    """Reproduce the visible text of a block element.

    Block-level structure is preserved: each paragraph/heading/list becomes a
    block separated by a blank line, exactly matching the page's reading order.
    UI/navigation/ad/cookie/social/comment/related-story nodes are dropped so
    only the article itself survives.
    """
    if not isinstance(el, Tag):
        return _trim_text(str(el)) if el is not None else ""
    if el.name in ("script", "style", "noscript") or _is_noise_element(el):
        return ""
    if el.name in ("ul", "ol"):
        return _list_text(el)
    if el.name == "table":
        return _table_text(el)

    blocks: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        text = _trim_text("".join(buf))
        if text:
            blocks.append(text)
        buf.clear()

    for child in el.children:
        if isinstance(child, NavigableString):
            buf.append(str(child))
        elif isinstance(child, Tag):
            if child.name == "br":
                buf.append("\n")
            elif child.name in ("script", "style", "noscript") or _is_noise_element(child):
                continue
            elif child.name in _BLOCK_TAGS:
                flush()
                sub = _block_text(child)
                if sub:
                    blocks.append(sub)
            else:
                buf.append(_inline_text(child))
    flush()
    return "\n\n".join(blocks)


def _normalize_url(url: Optional[str], base_url: str) -> str:
    url = (url or "").strip()
    if not url or url.lower().startswith("data:"):
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = urljoin(base_url, url)
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return ""
    return url


def _extract_canonical_url(soup, base_url: str) -> str:
    link = soup.find("link", rel="canonical")
    if link is not None:
        url = _normalize_url(link.get("href"), base_url)
        if url:
            return url
    for meta in soup.find_all("meta"):
        props = str(meta.get("property") or "").lower()
        name = str(meta.get("name") or "").lower()
        if props == "og:url" or name == "og:url":
            url = _normalize_url(meta.get("content"), base_url)
            if url:
                return url
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _iter_jsonld_objects(data):
            if not isinstance(node, dict):
                continue
            mop = node.get("mainEntityOfPage")
            if isinstance(mop, dict):
                url = _normalize_url(mop.get("url"), base_url)
                if url:
                    return url
            if isinstance(mop, str):
                url = _normalize_url(mop, base_url)
                if url:
                    return url
            url = _normalize_url(node.get("url"), base_url)
            if url:
                return url
    return ""


def _extract_page_post_id(html_text: str) -> str:
    """Resolve the ACTUAL WordPress post id from an article page.

    The RSS feed on this site has been observed serving stale/mis-numbered
    guids, so the post id taken from the final page itself (after redirects)
    is authoritative. Read from <link rel="shortlink"> (?p=<id>) or the
    postid-<id> body class.
    """
    soup = BeautifulSoup(html_text or "", "html.parser")
    for link in soup.find_all("link", rel="shortlink"):
        m = re.search(r"[?&]p=(\d+)", link.get("href") or "")
        if m:
            return m.group(1)
    body = soup.find("body")
    if body is not None:
        for cls in body.get("class") or []:
            m = re.fullmatch(r"postid-(\d+)", cls or "")
            if m:
                return m.group(1)
    return ""


def _clean_site_title(title: str) -> str:
    """Strip a trailing '| Site Name' / '– Site Name' style suffix."""
    return re.split(r"\s[|\u2013\u2014]\s", _trim_text(title))[0].strip()


def _extract_page_title(soup) -> str:
    for meta in soup.find_all("meta"):
        props = str(meta.get("property") or "").lower()
        name = str(meta.get("name") or "").lower()
        if props == "og:title" or name == "og:title":
            title = _trim_text(meta.get("content") or "")
            if title:
                return title
    h1 = soup.find("h1")
    if h1 is not None:
        title = _trim_text(h1.get_text(" ", strip=True))
        if title:
            return title
    title_tag = soup.find("title")
    if title_tag is not None:
        return _clean_site_title(title_tag.get_text(" ", strip=True))
    return ""


def _extract_article_from_page(
    html_text: str, base_url: str
) -> Tuple[str, str, str, List[Tuple[str, str]]]:
    """Pull canonical URL, page title, exact article body and image candidates
    from an article page. The returned body preserves paragraphs/lists/breaks."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    canonical = _extract_canonical_url(soup, base_url)
    page_title = _extract_page_title(soup)
    container = _find_article_container(soup)
    body = _block_text(container) if container is not None else ""
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    images = _extract_html_image_candidates(html_text, base_url, article_only=True)
    return canonical, page_title, body, images


def _paragraphs(text: str) -> List[str]:
    return [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def _paragraph_count(text: str) -> int:
    return len(_paragraphs(text))


def _normalize_for_hash(text: str) -> str:
    """Normalize text ONLY for duplicate-detection hashing.

    The original article content is never modified — this transform is applied
    exclusively when computing the content fingerprint. It collapses CRLF/LF,
    trims, decodes HTML entities and collapses ALL runs of whitespace (spaces,
    tabs, newlines, NBSP) to single spaces so that byte-identical articles
    produce the same fingerprint regardless of paragraph/whitespace layout.
    """
    if not text:
        return ""
    text = html.unescape(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def content_fingerprint(title: str, body: str) -> str:
    """Deterministic identity for an article's content.

    SHA-256 of the normalized title + body. No AI, no semantic similarity and
    no probabilistic guessing — two copies of the same story always produce the
    same fingerprint, and different stories (almost certainly) do not.
    """
    key = (_normalize_for_hash(title) + "\n" + _normalize_for_hash(body)).strip()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ExtractionError(ValueError):
    """Raised when the full article (text, and image when required) could not
    be faithfully extracted. The scheduler logs the reason and moves on."""


def validate_article(article: "Article", config: Config) -> List[str]:
    """Return a list of problems that make an article unpublishable.

    Empty title/body, invalid/missing article URL, invalid canonical URL,
    a body too short to be a real article (guards against menu/navigation
    text being mistaken for an article), and — in strict mode — a missing
    article image.
    """
    problems: List[str] = []
    if not article.title or not article.title.strip():
        problems.append("title is empty")
    if not article.link or not re.match(r"^https?://", article.link, re.IGNORECASE):
        problems.append("article URL is missing or invalid")
    if article.canonical_url and not re.match(
        r"^https?://", article.canonical_url, re.IGNORECASE
    ):
        problems.append("canonical URL is invalid")
    body = (article.content or "").strip()
    if not body:
        problems.append("article body is empty")
    elif config.strict_article_extraction and len(body) < config.min_article_chars:
        problems.append(
            f"article body too short ({len(body)} chars < "
            f"{config.min_article_chars}) — looks like navigation text, not an article"
        )
    if config.require_article_image and not article.image_candidates:
        problems.append("no article image found (REQUIRE_ARTICLE_IMAGE=true)")
    return problems


def fetch_image_bytes(
    session: requests.Session,
    url: str,
    referer: Optional[str] = None,
    timeout: int = 20,
    max_retries: int = 3,
) -> Tuple[bytes, str]:
    """Download an image as raw bytes with our own session. Returns
    (image_bytes, content_type). Never relies on Facebook fetching the URL."""
    last_error: Optional[Exception] = None
    candidate_urls = [url]
    alt = _alternate_host_url(url)
    if alt and alt != url:
        candidate_urls.append(alt)
    for current in candidate_urls:
        for attempt in range(1, max_retries + 1):
            try:
                headers = dict(BROWSER_HEADERS)
                if referer:
                    headers["Referer"] = referer
                resp = session.get(current, headers=headers, timeout=timeout)
                if resp.status_code == 406:
                    raise requests.HTTPError("HTTP 406 (WAF blocked)")
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                body = resp.content
                if not content_type.startswith("image/"):
                    raise ValueError(f"not an image (Content-Type '{content_type or 'missing'}')")
                if not body:
                    raise ValueError("empty image body")
                if len(body) > _MAX_IMAGE_BYTES:
                    raise ValueError(f"image too large ({len(body)} bytes; limit {_MAX_IMAGE_BYTES})")
                if body[:1] in (b"<", b"%", b"{"):  # HTML / token / JSON error page
                    raise ValueError("response body is HTML/JSON, not image data")
                return body, content_type or "image/jpeg"
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
    if last_error is None:
        raise requests.ConnectionError(f"no candidates to download for {url}")
    raise last_error


def _extract_hosts(*urls: str) -> List[str]:
    """Hostnames (www and bare) of the source site, kept for metadata."""
    hosts: set = set()
    for u in urls:
        m = _HOST_RE.match(u or "")
        if m:
            host = m.group(2).lower()
            hosts.add(host)
            hosts.add(host[4:] if host.startswith("www.") else "www." + host)
    return sorted(hosts)


# --- Hashtag generation -----------------------------------------------------
# Facebook's SEO sweet spot is a small batch of RELEVANT hashtags: research (and
# Meta's own guidance) shows ~3-6 hashtags outperform both none and dozens (the
# opposite of Instagram). Each article's title/summary/body is scanned for Urdu
# and English keywords and matched against curated topic rules. Hashtags are a
# mix of broad category (#Pakistan, #News) and specific topic (#WomenInSports,
# #CounterTerrorism) so posts stay searchable, on-topic and engagement-friendly.
# Facebook/Instagram fully support Urdu-script hashtags.

@dataclass(frozen=True)
class HashtagRule:
    """A topic rule: when any `keywords` appear in an article, its `hashtags`
    become candidates for that post. Higher `priority` breaks score ties."""

    keywords: Tuple[str, ...]
    hashtags: Tuple[str, ...]
    priority: int = 0


_HASHTAG_RULES: List[HashtagRule] = [
    # --- Pakistan & news category ---
    HashtagRule(
        keywords=("پاکستان", "پاکستانی", "اسلامی جمہوریہ", "pakistan"),
        hashtags=("#Pakistan", "#پاکستان"),
        priority=100,
    ),
    HashtagRule(
        keywords=("خبر", "خبریں", "نیوز", "تازہ ترین", "اہم خبر", "news"),
        hashtags=("#News", "#PakistanNews"),
        priority=50,
    ),
    # --- Politics & governance ---
    HashtagRule(
        keywords=(
            "سیاست", "سیاسی", "politics", "الیکشن", "election", "حکومت", "government",
            "وزیر اعظم", "وزیراعظم", "وزیر", "پارلیمنٹ", "parliament", "صوبہ", "صوبے",
            "اسمبلی", "وزیراعلیٰ", "وزیر اعلٰی", "چیف منسٹر", "سیاستدان", "ممبر پارلیمنٹ",
        ),
        hashtags=("#Politics", "#سیاست"),
        priority=90,
    ),
    HashtagRule(
        keywords=("پی ٹی آئی", "pti"),
        hashtags=("#PTI",),
        priority=40,
    ),
    HashtagRule(
        keywords=("مسلم لیگ", "نواز شریف", "nawaz sharif", "nawaz shrif"),
        hashtags=("#PMLN", "#NawazSharif"),
        priority=40,
    ),
    HashtagRule(
        keywords=("پیپلز پارٹی", "بلاول بھٹو", "ppp", "bilawal"),
        hashtags=("#PPP",),
        priority=40,
    ),
    # --- Provinces & regions ---
    HashtagRule(
        keywords=("پنجاب", "پنجابی", "punjab"),
        hashtags=("#Punjab", "#پنجاب"),
        priority=85,
    ),
    HashtagRule(
        keywords=("لاہور", "lahore"),
        hashtags=("#Lahore", "#لاہور"),
        priority=40,
    ),
    HashtagRule(
        keywords=("خیبر پختونخوا", "پشاور", "khyber pakhtunkhwa", "peshawar"),
        hashtags=("#KPK", "#Peshawar"),
        priority=40,
    ),
    HashtagRule(
        keywords=("سندھ", "sindh", "کراچی", "karachi"),
        hashtags=("#Sindh", "#Karachi"),
        priority=40,
    ),
    HashtagRule(
        keywords=("بلوچستان", "balochistan", "کوئٹہ", "quetta"),
        hashtags=("#Balochistan", "#Quetta"),
        priority=40,
    ),
    HashtagRule(
        keywords=("کشمیر", "کشمیری", "kashmir"),
        hashtags=("#Kashmir", "#کشمیر"),
        priority=90,
    ),
    # --- International / world ---
    HashtagRule(
        keywords=(
            "عالمی", "بین الاقوامی", "بین‌الاقوامی", "دنیا", "انٹرنیشنل", "گلوبل",
            "international", "world",
        ),
        hashtags=("#WorldNews", "#GlobalAffairs"),
        priority=70,
    ),
    HashtagRule(
        keywords=("امریکہ", "امریکا", "امریکی", "america", "usa", "امریکن"),
        hashtags=("#USA", "#America"),
        priority=50,
    ),
    HashtagRule(
        keywords=("ٹرمپ", "trump", "ڈونلڈ"),
        hashtags=("#Trump",),
        priority=30,
    ),
    HashtagRule(
        keywords=("ایران", "ایرانی", "iran"),
        hashtags=("#Iran", "#ایران"),
        priority=50,
    ),
    HashtagRule(
        keywords=("بھارت", "ہندوستان", "انڈیا", "بھارتی", "india", "ہریانہ"),
        hashtags=("#India", "#بھارت"),
        priority=50,
    ),
    HashtagRule(
        keywords=("چین", "چینی", "تبت", "china", "tibet"),
        hashtags=("#China", "#Tibet"),
        priority=50,
    ),
    HashtagRule(
        keywords=("جاپان", "جاپانی", "japan"),
        hashtags=("#Japan", "#جاپان"),
        priority=50,
    ),
    HashtagRule(
        keywords=("نیپال", "نیپالی", "nepal"),
        hashtags=("#Nepal", "#نیپال"),
        priority=50,
    ),
    HashtagRule(
        keywords=("برطانیہ", "برطانوی", "لندن", "uk", "britain", "british", "انگلینڈ"),
        hashtags=("#UK", "#Britain"),
        priority=50,
    ),
    HashtagRule(
        keywords=("کینیڈا", "کینیڈین", "canada"),
        hashtags=("#Canada", "#کینیڈا"),
        priority=50,
    ),
    HashtagRule(
        keywords=("افغانستان", "افغان", "afghanistan"),
        hashtags=("#Afghanistan", "#افغانستان"),
        priority=50,
    ),
    HashtagRule(
        keywords=("اقوام متحدہ", "یو این", "united nations"),
        hashtags=("#UnitedNations",),
        priority=30,
    ),
    HashtagRule(
        keywords=("ایشیا", "asia"),
        hashtags=("#Asia",),
        priority=20,
    ),
    # --- Economy / finance / business ---
    HashtagRule(
        keywords=(
            "معیشت", "اقتصادی", "معاشی", "بینک", "بینکنگ", "مالی", "منافع", "ٹیکس",
            "روپے", "سٹیٹ بینک", "زر مبادلہ", "ایکونومی", "بزنس", "economy",
            "finance", "banking", "bank", "business", "investment",
        ),
        hashtags=("#Economy", "#معیشت"),
        priority=70,
    ),
    HashtagRule(
        keywords=("روپے", "ڈالر", "اسٹاک مارکیٹ", "سٹاک مارکیٹ", "شئیر", "share market"),
        hashtags=("#StockMarket", "#Finance"),
        priority=30,
    ),
    # --- Education ---
    HashtagRule(
        keywords=(
            "تعلیم", "تعلیمی", "یونیورسٹی", "یونیورسٹیز", "جامعہ", "جامعات",
            "رینکنگ", "اسکول", "سکول", "کالج", "طلبہ", "طالبعلم", "education",
            "university", "universities", "ranking", "school", "college", "students",
        ),
        hashtags=("#Education", "#تعلیم"),
        priority=70,
    ),
    HashtagRule(
        keywords=("اعلیٰ تعلیم", "ہائر ایجوکیشن", "higher education"),
        hashtags=("#HigherEducation", "#Universities"),
        priority=20,
    ),
    # --- Sports & cricket ---
    HashtagRule(
        keywords=(
            "کرکٹ", "کھیل", "میچ", "ٹرافی", "ٹورنامنٹ", "ورلڈکپ", "ایشیا کپ",
            "کپ", "باؤلنگ", "بیٹنگ", "cricket", "sports", "match", "tournament",
            "trophy", "wicket", "ورلڈ کپ",
        ),
        hashtags=("#Sports", "#Cricket"),
        priority=70,
    ),
    HashtagRule(
        keywords=("ایشیا کپ", "اسیا کپ", "asia cup"),
        hashtags=("#AsiaCup", "#AsiaCup2026"),
        priority=30,
    ),
    HashtagRule(
        keywords=("ویمنز", "خواتین", "women", "خواتین کرکٹ", "ویمنز کرکٹ"),
        hashtags=("#WomensCricket", "#WomenInSports"),
        priority=60,
    ),
    # --- Health, hospitals & accidents ---
    HashtagRule(
        keywords=(
            "صحت", "ہسپتال", "اسپتال", "ڈاکٹر", "طبی", "علاج", "مریض", "میڈیکل",
            "health", "hospital", "hospitals", "doctor", "medical",
        ),
        hashtags=("#Health", "#صحت"),
        priority=70,
    ),
    HashtagRule(
        keywords=("زخمی", "سیکرٹری صحت", "صحت عامہ", "victims", "injured"),
        hashtags=("#Healthcare", "#Hospitals"),
        priority=20,
    ),
    HashtagRule(
        keywords=("آگ", "آگ لگنے", "fire", "حادثہ", "دھماکہ", "accident"),
        hashtags=("#Fire", "#آگ"),
        priority=40,
    ),
    # --- Natural disasters ---
    HashtagRule(
        keywords=(
            "سیلاب", "بارش", "طوفان", "زلزلہ", "ہلاکتیں", "تباہ کن", "امداد", "بچاؤ",
            "بچاؤ", "flood", "floods", "earthquake", "storm", "disaster", "rescue",
        ),
        hashtags=("#Floods", "#سیلاب"),
        priority=60,
    ),
    # --- Security & counterterrorism ---
    HashtagRule(
        keywords=(
            "دہشت", "دہشت گرد", "دہشت گردی", "سیکورٹی", "سیکیورٹی", "آپریشن",
            "سی ٹی ڈی", "اسلحہ", "گرفتار", "security", "terrorism", "counterterrorism",
            "operation", "arrest",
        ),
        hashtags=("#Security", "#CounterTerrorism"),
        priority=60,
    ),
    # --- Technology & cybersecurity ---
    HashtagRule(
        keywords=(
            "ٹیکنالوجی", "سائبر", "سائبر حملہ", "ہیکنگ", "ہیک", "ڈیٹا", "انٹرنیٹ",
            "موبائل", "سمارٹ", "ایپ", "ٹیلیکام", "technology", "tech", "cyber",
            "hacking", "hacker", "data", "internet", "mobile", "digital",
        ),
        hashtags=("#Technology", "#CyberSecurity"),
        priority=60,
    ),
    # --- Aviation ---
    HashtagRule(
        keywords=(
            "پرواز", "ہوائی اڈہ", "جہاز", "فلائٹ", "ایئرپورٹ", "فضائی", "لینڈنگ",
            "flight", "airline", "airport", "aviation",
        ),
        hashtags=("#Aviation", "#AirTravel"),
        priority=40,
    ),
    # --- Culture & history ---
    HashtagRule(
        keywords=(
            "تاریخ", "ثقافت", "ورثہ", "تہذیب", "مقبرہ", "قلعہ",
            "history", "culture", "heritage",
        ),
        hashtags=("#History", "#تاریخ"),
        priority=40,
    ),
]

# Always-added brand/fallback hashtags, used to top up posts that matched few
# topics so every post still carries a small, relevant, searchable tag set.
_FALLBACK_HASHTAGS = ("#Humshehri", "#PakistanNews", "#News")

# A single hashtag token longer than this is junk (an embedded sentence/URL),
# never a meaningful tag.
_MAX_HASHTAG_LEN = 120

# Trailing decoration sometimes glued to a tag by human/AI/AI-adjacent output:
# punctuation, quotes, markdown markers, bullet/numbering remnants. Stripped
# from the end of a harvested "#tag" span before validation.
_TAG_TRAIL_JUNK = ".,;:!?()[]{}<>\"'`*_$^~=&|%«»„“”‘’·…"


def _clean_tag_token(match: str) -> str:
    """Trim a raw '#...#...' span (e.g. '#Pakistan,' or '**#Lahore***') down to
    its bare '#tag' form by dropping trailing punctuation/markdown."""
    token = match.strip()
    while token and token[-1] in _TAG_TRAIL_JUNK:
        token = token[:-1].rstrip()
    return token


def _valid_hashtag(tag: str) -> bool:
    """True only for a meaningful, safely-postable hashtag: starts with '#',
    has non-empty alphanumeric (incl. Urdu/Urdu-script) content, and contains
    no spaces, line breaks, URLs or stray symbols."""
    if not tag.startswith("#"):
        return False
    body = tag[1:]
    if not body or len(tag) > _MAX_HASHTAG_LEN:
        return False
    if "http" in tag.lower():
        return False
    if any(ch.isspace() for ch in body):
        return False
    return all(ch.isalnum() or ch in ("_", "-") for ch in body)


def _sanitize_hashtags(candidates: "Iterable[str]") -> "List[str]":
    """Reduce arbitrary candidate text to a list of well-formed #hashtags.

    Only the '#tag' tokens survive. Accidental prose ("Here are some hashtags:"),
    bullet points ('* #Lahore'), numbering ('1. #News'), quotation marks, markdown,
    quotation marks, markdown, any URL- or newline-bearing candidate, and empty
    strings are all dropped. Duplicates are removed case-insensitively (the
    first casing wins: '#Pakistan #pakistan #Pakistan' -> '#Pakistan')."""
    seen: set = set()
    out: List[str] = []
    for cand in candidates or ():
        if not cand or "\n" in cand or "\r" in cand:
            continue
        if "http" in cand.lower():  # "someone said #News" is fine; urls are not
            continue
        for match in re.findall(r"#[^\s]+", cand):
            tag = _clean_tag_token(match)
            if tag and _valid_hashtag(tag):
                key = tag[1:].lower()
                if key not in seen:
                    seen.add(key)
                    out.append(tag)
    return out


def _is_urdu_letter(ch: str) -> bool:
    """True when a single char is a letter (Arabic/Urdu or otherwise). Used to
    make sure Urdu keywords match whole words and not substrings of longer
    words (e.g. پاکستان won't match پاکستانیت)."""
    return bool(ch) and unicodedata.category(ch) in ("Lo", "Lu", "Ll", "Lm", "Lt", "Nl")


def _keyword_count(text: str, keyword: str) -> int:
    """Count whole-word occurrences of `keyword` in `text`. Latin keywords use
    regex word boundaries; Urdu keywords match when surrounded by non-letters
    (space, punctuation, ZWNJ, digits, start/end of text) so that, e.g.,
    `کشمیر` still matches inside `کشمیرِ عظیم` but not inside `کشمیری`."""
    if not text or not keyword:
        return 0
    keyword = keyword.strip()
    if not keyword:
        return 0
    if all(c.isascii() for c in keyword):
        pattern = re.compile(rf"\b{re.escape(keyword.lower())}\b")
        return len(pattern.findall(text.lower()))
    count = 0
    index = 0
    while True:
        pos = text.find(keyword, index)
        if pos < 0:
            break
        before = text[pos - 1] if pos > 0 else ""
        after_pos = pos + len(keyword)
        after = text[after_pos] if after_pos < len(text) else ""
        if not _is_urdu_letter(before) and not _is_urdu_letter(after):
            count += 1
        index = pos + 1
    return count


def _select_hashtags(article: "Article") -> List[str]:
    """Rank the curated topic rules against the article and return the
    sanitized, deduplicated hashtag list, capped at `article.max_hashtags`.

    Fills up to 3 brand/news fallback tags when the article matched few or no
    topics, so the block is never empty (but always genuinely on-topic)."""
    max_tags = max(1, int(getattr(article, "max_hashtags", 6)))
    title = article.title or ""
    full = " ".join(filter(None, (article.title or "", article.summary or "", article.content or "")))

    scored: List[Tuple[int, int, HashtagRule]] = []
    for rule in _HASHTAG_RULES:
        title_hits = sum(_keyword_count(title, kw) for kw in rule.keywords)
        total_hits = title_hits * 3 + sum(_keyword_count(full, kw) for kw in rule.keywords)
        if total_hits:
            scored.append((total_hits, rule.priority, rule))
    scored.sort(key=lambda item: (-item[0], -item[1]))

    raw: List[str] = []
    for _hits, _prio, rule in scored:
        for tag in rule.hashtags:
            raw.append(tag)
            if len(raw) >= max_tags:
                break
        if len(raw) >= max_tags:
            break

    target = min(max_tags, 3)
    for tag in _FALLBACK_HASHTAGS:
        if len(raw) >= target:
            break
        raw.append(tag)

    chosen = _sanitize_hashtags(raw)
    if len(chosen) < target:
        for tag in _FALLBACK_HASHTAGS:
            if len(chosen) >= target:
                break
            clean = _sanitize_hashtags([tag])
            if clean and clean[0] not in chosen:
                chosen.append(clean[0])
    return chosen[:max_tags]


def generate_hashtags(article: "Article") -> str:
    """Build a single space-separated hashtag block (sanitized, deduplicated,
    relevance-ranked) to be appended after the exact article text when
    ADD_HASHTAGS=true. Hashtags never modify the article itself."""
    return " ".join(_select_hashtags(article))


class NoUsableImageError(ValueError):
    """Raised when an article has no image that could be found/downloaded.
    The scheduler treats this as a permanent skip, not a post failure."""


class OversizedCaptionError(ValueError):
    """Raised when the exact article text exceeds Facebook's caption limit.
    The article is never silently truncated; it fails safely instead."""


@dataclass
class Article:
    guid: str
    title: str
    summary: str
    content: str
    link: str
    image_candidates: List[str] = field(default_factory=list)
    source_hosts: List[str] = field(default_factory=list)
    published_at: Optional[str] = None
    add_hashtags: bool = False
    max_hashtags: int = 6
    canonical_url: str = ""
    source: str = ""
    sources: List[str] = field(default_factory=list)
    api_post_id: str = ""
    content_hash: str = ""
    extraction_method: str = ""
    image_method: str = ""
    paragraph_count: int = 0

    FACEBOOK_MSG_LIMIT = 63206

    @property
    def image_url(self) -> Optional[str]:
        """First (best) image candidate; kept for backwards compatibility."""
        return self.image_candidates[0] if self.image_candidates else None

    @property
    def text_character_count(self) -> int:
        return len(self.content or "")

    @property
    def facebook_caption(self) -> str:
        """The exact article: original title followed by the original body.

        Nothing is rewritten, shortened, translated, URL-scrubbed or hashtagged.
        Hashtags are a separate Facebook-post layer appended AFTER this text by
        `build_caption_with_hashtags()` when ADD_HASHTAGS=true; they never alter
        it. Paragraph breaks are preserved from the source article; no artificial
        character limit is applied here.
        """
        body = self.content.strip() if self.content else self.summary.strip()
        caption = self.title.strip()
        if body:
            caption = f"{caption}\n\n{body}"
        return caption.strip()


def build_caption_with_hashtags(
    article: "Article", limit: int = FACEBOOK_MSG_LIMIT
) -> str:
    """Assemble the FINAL Facebook caption for an article.

    Layout (no AI intro/summary/promo, no reordering):
        [EXACT ORIGINAL TITLE]
        [blank line]
        [EXACT ORIGINAL BODY]
        [blank line]
        [3-7 generated hashtags]

    The article text (title + body) is never modified: if the combined caption
    approaches `limit`, hashtags are dropped one at a time; if the article alone
    already exceeds the platform limit, it fails safely with
    OversizedCaptionError instead of being truncated. If hashtag generation
    fails, the exact article is returned as-is (never modified).
    """
    base = article.facebook_caption
    if len(base) > limit:
        raise OversizedCaptionError(
            f"exact article text is {len(base)} characters, which exceeds "
            f"Facebook's {limit}-character caption limit; "
            f"refusing to truncate the article"
        )
    if not article.add_hashtags:
        return base
    try:
        tags = _select_hashtags(article)
    except Exception as exc:  # pragma: no cover
        log.warning(
            "  Hashtag generation failed for [%s]; posting the exact article "
            "without hashtags: %s",
            article.guid, exc,
        )
        tags = []
    block = " ".join(tags)
    while block and len(base) + 2 + len(block) > limit:
        tags = tags[:-1]
        block = " ".join(tags)
    if not block:
        return base
    return f"{base}\n\n{block}"


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

    def store_content_hash(self, post_id: str, content_hash: str, title: str, status: str) -> None:
        self._backend.store_content_hash(post_id, content_hash, title, status)

    def find_duplicate_content(
        self, content_hash: str, exclude_post_id: Optional[str] = None
    ) -> Optional[Tuple[str, str, str]]:
        """Return (post_id, title, status) of a previously stored post carrying
        the same content fingerprint (excluding the post being examined), or
        None. Posted rows are preferred so already-published content is never
        re-posted via a different WordPress post id."""
        return self._backend.find_duplicate_content(content_hash, exclude_post_id)

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
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content_hashes (
                post_id      TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                title        TEXT,
                status       TEXT DEFAULT 'candidate',
                updated_at   TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_hashes_hash ON content_hashes (content_hash)"
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

    def store_content_hash(self, post_id: str, content_hash: str, title: str, status: str) -> None:
        self._conn.execute(
            "INSERT INTO content_hashes (post_id, content_hash, title, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(post_id) DO UPDATE SET content_hash = excluded.content_hash, "
            "title = excluded.title, status = excluded.status, updated_at = excluded.updated_at",
            (post_id, content_hash, title, status, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def find_duplicate_content(
        self, content_hash: str, exclude_post_id: Optional[str] = None
    ) -> Optional[Tuple[str, str, str]]:
        if exclude_post_id:
            row = self._conn.execute(
                "SELECT post_id, title, status FROM content_hashes "
                "WHERE content_hash = ? AND post_id != ? "
                "ORDER BY CASE status WHEN 'posted' THEN 0 ELSE 1 END LIMIT 1",
                (content_hash, exclude_post_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT post_id, title, status FROM content_hashes "
                "WHERE content_hash = ? ORDER BY CASE status WHEN 'posted' THEN 0 ELSE 1 END LIMIT 1",
                (content_hash,),
            ).fetchone()
        return tuple(row) if row else None

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

    def store_content_hash(self, post_id: str, content_hash: str, title: str, status: str) -> None:
        hashes = self._data.setdefault("_content_hashes", {})
        hashes[str(post_id)] = {
            "content_hash": content_hash,
            "title": title,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def find_duplicate_content(
        self, content_hash: str, exclude_post_id: Optional[str] = None
    ) -> Optional[Tuple[str, str, str]]:
        hashes = self._data.get("_content_hashes", {})
        best: Optional[Tuple[str, str, str]] = None
        for pid, rec in hashes.items():
            if rec.get("content_hash") != content_hash:
                continue
            if exclude_post_id and pid == exclude_post_id:
                continue
            if best is None or (rec.get("status") == "posted" and best[2] != "posted"):
                best = (pid, rec.get("title", ""), rec.get("status", "candidate"))
        return best

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
    """Discovers article URLs from the RSS feed and the WordPress REST API
    (merged, so the API runs alongside RSS — never a replacement — and the same
    article is never processed twice) and then hydrates each one from its own
    article page.

    The RSS/API data is used for *discovery and metadata only*: titles, links
    and any image the feed carries. The full article page remains the
    authoritative source of the article body, canonical URL, real post id and
    image, so the published text is exactly what the website shows. The RSS
    post ids are never trusted as identity — the actual post id is resolved
    from the final article page (after redirects). Duplicate WordPress copies
    of the same story are detected by an exact content fingerprint.
    """

    def __init__(self, config: Config, storage: Storage):
        self.config = config
        self.storage = storage
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.source_hosts = _extract_hosts(config.rss_feed_url, config.wp_api_url)

    def fetch_new_articles(self, limit: int = 10) -> List[Article]:
        """Discover + evaluate candidates from RSS and the WP REST API and
        return the ones that are genuinely new, non-duplicate and valid."""
        reports = self.evaluate_candidates(limit)
        return [r["article"] for r in reports if r["status"] == "ELIGIBLE"]

    # ------------------------------------------------------------------
    # Discovery: RSS + WordPress REST API, merged into one candidate list
    # ------------------------------------------------------------------
    def evaluate_candidates(self, limit: int = 10) -> List[dict]:
        """Run the full discovery pipeline against both sources.

        Returns a report per candidate with keys:
          article, post_id, title, url, canonical, sources, content_hash,
          status ('ELIGIBLE'|'DUPLICATE'|'FAILED'|'SKIPPED_POSTED'),
          reason, duplicate_of.

        No posts are made here; used by both the live path and --list-new.
        """
        pending = self._collect_candidates(limit)
        if not pending:
            log.info("No articles returned by any source")
            return []

        seen_guids: set = set()
        seen_hashes: Dict[str, Tuple[str, str]] = {}
        seen_ids: Dict[str, str] = {}

        reports: List[dict] = []
        for article in pending:
            report = self._process_candidate(
                article, seen_guids, seen_hashes, seen_ids
            )
            reports.append(report)
        return reports

    def _collect_candidates(self, limit: int) -> List[Article]:
        """Fetch candidates from RSS and the WP REST API and merge them into a
        single de-duplicated list. The API runs alongside RSS (not just as a
        fallback) so articles the feed misses or mis-numbers are still found."""
        rss = self._fetch_via_rss(limit)
        api: List[Article] = []
        api_by_pid: Dict[str, Article] = {}
        if self.config.enable_wp_api_discovery:
            api = self._fetch_via_wp_api(self.config.wp_api_lookback)
            api_by_pid = {a.api_post_id: a for a in api if a.api_post_id}
            self._backfill_api_hashes(api)

        merged = self._merge_candidates(rss, api_by_pid)
        rss_n, api_n, merged_n = len(rss), len(api), len(merged)
        log.info(
            "Discovered %d candidate(s) (rss=%d, api=%d, merged=%d)",
            merged_n, rss_n, api_n, merged_n,
        )
        return merged

    def _merge_candidates(
        self, rss: List[Article], api_by_pid: Dict[str, Article]
    ) -> List[Article]:
        """Merge RSS + API candidates so the same article is only processed once.

        Matching keys (exact, never fuzzy): the WordPress post id when the RSS
        guid carries a ?p= id that exists in the API, or the article URL.
        When an RSS entry matches an API post, the API post's (accurate)
        metadata wins and the RSS discovery source is recorded alongside it.
        """
        merged: List[Article] = []
        used_api: set = set()
        api_by_url: Dict[str, Article] = {}
        for api_a in api_by_pid.values():
            url_norm = self._normalized_link(api_a.link)
            if url_norm:
                api_by_url.setdefault(url_norm, api_a)

        for rss_a in rss:
            match = self._match_api_candidate(rss_a, api_by_pid, api_by_url)
            if match is not None:
                match.sources = sorted(set(match.sources) | {"rss"})
                match.source = "+".join(match.sources)
                used_api.add(match.api_post_id)
                merged.append(match)
            else:
                merged.append(rss_a)

        for api_a in api_by_pid.values():
            if api_a.api_post_id not in used_api:
                merged.append(api_a)
        return merged

    def _match_api_candidate(
        self,
        rss_a: Article,
        api_by_pid: Dict[str, Article],
        api_by_url: Dict[str, Article],
    ) -> Optional[Article]:
        pid = self._post_id_from_guid(rss_a.guid)
        if pid and pid in api_by_pid:
            return api_by_pid[pid]
        url_norm = self._normalized_link(rss_a.link)
        return api_by_url.get(url_norm)

    @staticmethod
    def _post_id_from_guid(guid: str) -> str:
        m = re.search(r"[?&]p=(\d+)$", guid or "")
        if m:
            return m.group(1)
        if re.fullmatch(r"\d+", guid or ""):
            return guid
        return ""

    def _normalized_link(self, url: str) -> str:
        return re.sub(r"/+$", "", url or "").strip()

    def _post_guid_forms(self, post_id: str) -> List[str]:
        """All '?p=' URL spellings that could identify post_id in the DB."""
        forms = {f"https://humshehri.online/?p={post_id}"}
        for url in (self.config.rss_feed_url, self.config.wp_api_url):
            m = _HOST_RE.match(url)
            if not m:
                continue
            scheme, host = m.group(1), m.group(2)
            forms.add(f"{scheme}{host}/?p={post_id}")
            forms.add(f"{scheme}{('www.' + host) if not host.startswith('www.') else host[4:]}/?p={post_id}")
        return sorted(forms)

    def _is_posted_pid(self, post_id: str) -> bool:
        return any(self.storage.is_posted(f) for f in self._post_guid_forms(post_id))

    def _backfill_api_hashes(self, api_articles: List[Article]) -> None:
        """Store content fingerprints for every post the API returns so that a
        new post (e.g. 1589) can be recognized as the same story as an
        already-posted one (e.g. 1590) purely by content hash."""
        for a in api_articles:
            if not a.api_post_id:
                continue
            h = content_fingerprint(a.title, a.content)
            posted = self.storage.is_posted(a.guid) or self._is_posted_pid(a.api_post_id)
            self.storage.store_content_hash(
                a.api_post_id, h, a.title, "posted" if posted else "candidate"
            )

    def _mark_processed(self, article: Article, post_id: str) -> None:
        """Record a duplicate candidate as already processed so later runs
        skip it cheaply (before any page fetch). The canonical URL is stored
        plus every ?p= form that aliases the same post."""
        forms = {article.guid}
        if post_id:
            forms.update(self._post_guid_forms(post_id))
        for form in forms:
            if form:
                self.storage.mark_posted(form, article.title, article.link)

    def _process_candidate(
        self, article: Article, seen_guids: set,
        seen_hashes: Dict[str, Tuple[str, str]], seen_ids: Dict[str, str],
    ) -> dict:
        post_id = article.api_post_id or self._post_id_from_guid(article.guid)
        url = article.link
        sources = list(article.sources or (["rss"] if article.source == "rss" else ["wp-api"]))

        def report(status: str, reason: str = "", dup_of: str = "") -> dict:
            return {
                "article": article,
                "post_id": post_id,
                "title": article.title,
                "url": url,
                "canonical": article.canonical_url,
                "sources": sources,
                "content_hash": article.content_hash,
                "status": status,
                "reason": reason,
                "duplicate_of": dup_of,
            }

        # Cheap gate before any network work: this post's identity was already
        # published (its ?p= id exists in the DB), so skip it.
        if post_id and self._is_posted_pid(post_id):
            return report("SKIPPED_POSTED", f"already posted (?p={post_id})", post_id)
        if article.api_post_id and self.storage.is_posted(article.guid):
            return report("SKIPPED_POSTED", "already posted", article.api_post_id)

        try:
            self._hydrate_article(article)
        except Exception as exc:
            self._record_extraction_failure(article)
            return report("FAILED", str(exc))

        # Resolved, authoritative identity after a full-page fetch.
        post_id = article.api_post_id or self._post_id_from_guid(article.guid)
        if article.canonical_url:
            article.guid = article.canonical_url
        elif post_id:
            article.guid = f"{self._post_guid_forms(post_id)[0]}"
        title = article.title
        article.content_hash = content_fingerprint(title, article.content or "")
        h = article.content_hash

        if article.guid in seen_guids:
            return report("DUPLICATE", "same article already seen in this run", article.guid)
        seen_guids.add(article.guid)

        # Content-level duplicate checks (exact fingerprint, never fuzzy).
        if h in seen_hashes:
            prev_pid, prev_title = seen_hashes[h]
            dup_of = f"post {prev_pid or '?'} ({prev_title}) [same run]"
            log.info(
                "  DUPLICATE content detected; skipping article. "
                "article=%s hash=%s title=%r matches=%s",
                post_id or article.guid, h, title, dup_of,
            )
            self._mark_processed(article, post_id)
            return report("DUPLICATE", "same content as another candidate this run", dup_of)

        row = self.storage.find_duplicate_content(h, exclude_post_id=post_id or None)
        if row:
            prev_pid, prev_title, prev_status = row
            dup_of = f"post {prev_pid} ({prev_title}) [posted earlier]"
            log.info(
                "  DUPLICATE content detected; skipping article. "
                "article=%s hash=%s title=%r matches=%s",
                post_id or article.guid, h, title, dup_of,
            )
            self._mark_processed(article, post_id)
            return report("DUPLICATE", "same content as an already-posted article", dup_of)

        identity = post_id or article.canonical_url or article.link
        if identity and identity in seen_ids:
            dup_of = seen_ids[identity]
            return report("DUPLICATE", "same article already seen in this run", dup_of)
        seen_ids[identity] = f"post {post_id or '?'} ({title})"

        # Register the fingerprint now that identity dups have been cleared.
        seen_hashes[h] = (post_id, title)
        if post_id:
            self.storage.store_content_hash(post_id, h, title, "candidate")

        problems = validate_article(article, self.config)
        if problems:
            for problem in problems:
                log.error(
                    "  EXTRACTION FAILED [%s] %s: %s", article.guid, title, problem,
                )
            self._record_extraction_failure(article)
            return report("FAILED", "; ".join(problems))

        self._log_article_summary(article)
        return report("ELIGIBLE")

    def _record_extraction_failure(self, article: Article) -> None:
        """Mark an article that could not be faithfully extracted. It is
        never posted with a partial or fabricated body."""
        attempts = self.storage.increment_attempts(
            article.guid, article.title, article.link
        )
        log.warning(
            "  Marking [%s] as failed (%d attempt%s so far); not publishable",
            article.guid, attempts, "" if attempts == 1 else "s",
        )
        if attempts >= self.config.max_post_attempts:
            log.warning(
                "  Giving up on [%s] after %d failed extractions; marking as skipped",
                article.guid, attempts,
            )
            self.storage.mark_posted(article.guid, article.title, article.link)

    def _hydrate_article(self, article: Article) -> None:
        """Fetch the article page once and use it for the canonical URL, the
        body text and the image — so text and image provably come from the
        same article."""
        if not article.link:
            raise ExtractionError("article has no URL to fetch")

        html_text = self._fetch_html(article.link)
        if not html_text:
            if self.config.strict_article_extraction:
                raise ExtractionError("could not fetch the article page")
            log.warning(
                "  [%s] article page unavailable; using feed/API body (non-strict mode)",
                article.guid,
            )
            article.extraction_method = "source-body-fallback"
            article.image_candidates = self._merge_image_candidates(article, [])
            self._finalize_article_stats(article)
            return

        canonical, page_title, body, page_images = _extract_article_from_page(
            html_text, article.link
        )
        article.canonical_url = canonical or ""

        # The RSS feed's post ids are NOT trusted as authoritative: the actual
        # WordPress post id is read from the FINAL page itself (after any
        # redirects), e.g. via <link rel="shortlink" href="?p=NNN">.
        hint_post_id = article.api_post_id
        resolved_pid = _extract_page_post_id(html_text)
        if resolved_pid:
            article.api_post_id = resolved_pid

        if body:
            article.content = body
            article.extraction_method = "full-article-page"
        else:
            if self.config.strict_article_extraction:
                raise ExtractionError(
                    "article page fetched but no article body could be extracted "
                    "(container not found or only navigation text present)"
                )
            log.warning(
                "  [%s] no article body on page; using feed/API body (non-strict mode)",
                article.guid,
            )
            article.extraction_method = "source-body-fallback"

        # Title: the feed/API title IS the source article's own title, so it
        # is kept verbatim and the page title is only used when the source
        # gave none. EXCEPTION: if the page resolved a different WordPress
        # post id than the feed suggested (stale/mis-numbered guid linking to
        # another post), the feed's title describes the wrong article and must
        # not ride on this body — the page title is used instead.
        if not article.title.strip() and page_title:
            article.title = page_title
        elif (
            page_title
            and resolved_pid
            and hint_post_id
            and resolved_pid != hint_post_id
        ):
            log.warning(
                "  [%s] feed post id %r strapped to %r; using the page title "
                "instead of the feed label",
                article.guid, hint_post_id, resolved_pid,
            )
            article.title = page_title

        # Image candidates: existing source candidates first (WP featured
        # media / RSS media), then the page's og:image → JSON-LD → twitter →
        # article <img> chain. All of them originate from this article's page
        # or its own feed entry, so the image can never come from another
        # article.
        article.image_candidates = self._merge_image_candidates(article, page_images)
        self._finalize_article_stats(article)

    def _finalize_article_stats(self, article: Article) -> None:
        article.paragraph_count = _paragraph_count(article.content or "")

    def _merge_image_candidates(
        self, article: Article, page_images: List[Tuple[str, str]]
    ) -> List[str]:
        merged: List[Tuple[str, str]] = []
        seen: set = set()
        for url in article.image_candidates:
            if url not in seen:
                seen.add(url)
                merged.append((url, "source-media/feeds"))
        for url, label in page_images:
            if url not in seen:
                seen.add(url)
                merged.append((url, label))
        article.image_method = merged[0][1] if merged else ""
        return [url for url, _ in merged]

    def resolve_image_candidates(self, article: Article, force_html: bool = False) -> List[str]:
        """Return the ordered list of image candidates for an article.

        Source candidates (RSS media / WP API featured media) win; if there is
        none, or force_html is set, the article's HTML page is fetched and its
        og:image → JSON-LD → twitter:image → main <img> chain is used.
        """
        if not force_html:
            current = [u for u in (article.image_candidates or []) if u]
            if current:
                return current
        html_text = self._fetch_html(article.link)
        if not html_text:
            return []
        extracted = _extract_html_image_candidates(html_text, article.link,
                                                   article_only=True)
        urls = [u for u, _ in extracted]
        article.image_candidates = self._merge_image_candidates(article, extracted)
        return urls

    def _fetch_html(self, url: str) -> Optional[str]:
        """Fetch an article page's HTML, trying the alternate host on failure."""
        candidate_urls = [url]
        alt = _alternate_host_url(url)
        if alt and alt != url:
            candidate_urls.append(alt)
        last_error: Optional[Exception] = None
        for current in candidate_urls:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    resp = self.session.get(current, timeout=self.config.http_timeout)
                    if resp.status_code == 406:
                        raise requests.HTTPError("HTTP 406 (WAF blocked)")
                    resp.raise_for_status()
                    return resp.text
                except Exception as exc:
                    last_error = exc
                    if attempt < self.config.max_retries:
                        time.sleep(2 ** attempt)
        log.warning("Could not fetch article page for extraction: %s (%s)", url, last_error)
        return None

    def _clean_body_text(self, text: str) -> str:
        """Plain-text conversion of a feed/API body. Only strips HTML markup;
        the written content is never rewritten or truncated."""
        return strip_html(text or "")

    def _log_article_summary(self, article: Article) -> None:
        body = article.content or ""
        log.info("  ARTICLE [%s] ready to post", article.guid)
        log.info("    article URL:     %s", article.link)
        log.info("    canonical URL:   %s", article.canonical_url or "(none)")
        log.info("    source:          %s", article.source or "unknown")
        log.info("    extraction:      %s", article.extraction_method or "unknown")
        log.info("    title:           %s", article.title)
        log.info("    text characters: %d", len(body))
        log.info("    paragraphs:      %d", article.paragraph_count)
        log.info("    image method:    %s", article.image_method or "(none)")
        log.info("    image url:       %s", article.image_url or "(none)")

    def _fetch_via_rss(self, limit: int) -> List[Article]:
        if feedparser is None:
            return []
        feed_urls = [self.config.rss_feed_url]
        alt = _alternate_host_url(self.config.rss_feed_url)
        if alt and alt != self.config.rss_feed_url:
            feed_urls.append(alt)
        for feed_url in feed_urls:
            try:
                # Fetch over the shared requests.Session (which verifies the
                # site's TLS cert correctly) instead of letting feedparser open
                # its own socket. feedparser's bundled opener can reject the
                # site's cert chain as expired even when requests accepts it.
                resp = self.session.get(feed_url, timeout=self.config.http_timeout)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
            except Exception as exc:  # pragma: no cover
                log.warning("RSS feed parsing failed for %s (%s)", feed_url, exc)
                continue
            entries = getattr(feed, "entries", [])[:limit]
            if entries:
                log.info("Fetched %d article(s) from RSS feed (%s)", len(entries), feed_url)
                return self._build_from_rss(entries)
            log.info("RSS feed returned no entries (%s)", feed_url)
        return []

    def _build_from_rss(self, entries) -> List[Article]:
        articles: List[Article] = []
        for entry in entries:
            guid = entry.get("id") or entry.get("link") or ""
            if not guid:
                continue
            raw_content = entry.get("content", [{}])
            if isinstance(raw_content, list):
                raw_content = raw_content[0].get("value", "") if raw_content else ""
            else:
                raw_content = raw_content.get("value", "") if isinstance(raw_content, dict) else str(raw_content)
            # Image candidates carried in the feed itself. The article text and
            # the image stay separate: text -> caption, image -> uploaded file.
            image_candidates = _rss_image_candidates(entry, raw_content, self.config.rss_feed_url)
            articles.append(
                Article(
                    guid=guid,
                    title=strip_html(entry.get("title", "")),
                    summary=strip_html(entry.get("summary", ""), max_chars=400),
                    content=self._clean_body_text(raw_content or entry.get("summary", "")),
                    link=entry.get("link", ""),
                    image_candidates=image_candidates,
                    source_hosts=self.source_hosts,
                    published_at=entry.get("published", entry.get("updated")),
                    add_hashtags=self.config.add_hashtags,
                    max_hashtags=self.config.max_hashtags,
                    source="rss",
                    sources=["rss"],
                    api_post_id=self._post_id_from_guid(guid),
                )
            )
        return articles

    def _fetch_via_wp_api(self, limit: int) -> List[Article]:
        base = self.config.wp_api_url
        sep = "&" if "?" in base else "?"
        url = f"{base}{sep}per_page={limit}&_fields=" + ",".join(
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
            rendered = post.get("content", {}).get("rendered", "")
            candidates: List[str] = []
            for u in (self._resolve_featured_image(post.get("featured_media")),
                      _first_image_url(rendered)):
                u = _normalize_image_src(u, self.config.wp_api_url)
                if u and _looks_like_real_photo(u) and u not in candidates:
                    candidates.append(u)
            articles.append(
                Article(
                    guid=post.get("link", ""),
                    title=strip_html(post.get("title", {}).get("rendered", "")),
                    summary=strip_html(post.get("excerpt", {}).get("rendered", ""), max_chars=400),
                    content=self._clean_body_text(rendered),
                    link=post.get("link", ""),
                    image_candidates=candidates,
                    source_hosts=self.source_hosts,
                    published_at=post.get("date_gmt"),
                    add_hashtags=self.config.add_hashtags,
                    max_hashtags=self.config.max_hashtags,
                    source="wp-api",
                    sources=["wp-api"],
                    api_post_id=str(pid),
                )
            )
        return articles

    def _resolve_featured_image(self, media_id: Optional[int]) -> Optional[str]:
        if not media_id:
            return None
        base = self.config.wp_api_url.rstrip("/")
        if "?" in base:
            host, _, query = base.partition("?")
            route = query.split("&", 1)[0]
            if route.startswith("rest_route="):
                route = route[len("rest_route="):]
            route = route.rstrip("/")
            if route.endswith("/posts"):
                route = route[: -len("/posts")]
            url = f"{host.rstrip('/')}/?rest_route={route}/media/{media_id}&_fields=source_url"
        else:
            if base.endswith("/posts"):
                base = base[: -len("/posts")]
            url = f"{base}/media/{media_id}?_fields=source_url"
        try:
            data = self._request_json(url)
            if data:
                return data.get("source_url")
        except Exception as exc:  # pragma: no cover
            log.warning("Could not resolve featured image %s: %s", media_id, exc)
        return None

    def _request_json(self, url: str) -> Optional[dict]:
        candidate_urls = [url]
        alt = _alternate_host_url(url)
        if alt and alt != url:
            candidate_urls.append(alt)
        last_error: Optional[Exception] = None
        for current_url in candidate_urls:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    resp = self.session.get(current_url, timeout=self.config.http_timeout)
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
                            "Request to %s failed (%s); retrying in %ds (attempt %d/%d)",
                            current_url, exc, backoff, attempt, self.config.max_retries,
                        )
                        time.sleep(backoff)
            if len(candidate_urls) > 1:
                log.warning(
                    "All attempts to %s failed (%s); trying alternate host %s",
                    current_url, last_error, _alternate_host_url(current_url),
                )
        log.error("Request to %s failed after all attempts: %s", url, last_error)
        return None

    def close(self) -> None:
        self.session.close()


class FacebookPoster:
    """Publishes articles to the Facebook Page via the Meta Graph API.

    Images are never given to Facebook as a URL: the bot downloads the file
    itself and uploads the raw bytes with a native Facebook Photo post (the
    /<page>/photos endpoint with multipart 'source'). The article text is the
    caption; no source link is ever passed.
    """

    def __init__(self, config: Config, image_extractor=None):
        self.config = config
        self.session = requests.Session()
        self.image_extractor = image_extractor

    def post(self, article: Article) -> str:
        # FINAL caption: the exact article (title + body verbatim) followed,
        # when enabled, by a blank line and the generated hashtags. Hashtags
        # are reduced if the combined text nears the platform limit; the
        # article itself is never truncated.
        caption = build_caption_with_hashtags(article, FACEBOOK_MSG_LIMIT)
        if article.add_hashtags:
            tail = caption[len(article.facebook_caption):].strip("\n")
            log.info(
                "  Hashtags for [%s]: %s",
                article.guid,
                " ".join(tail.split()) if tail else "(none - exact article only)",
            )

        if not self.config.post_with_image:
            return self._post_text(article, caption)

        candidates = list(article.image_candidates or [])
        if not candidates and self.image_extractor:
            try:
                candidates = self.image_extractor(article) or []
            except Exception as exc:  # pragma: no cover
                log.warning("Image extraction fallback failed for [%s]: %s", article.guid, exc)
                candidates = []
        # Try every candidate in order (WP featured, RSS media, og:image,
        # JSON-LD, twitter:image, article <img>…). If a download fails we log
        # the exact reason and move on to the next candidate.
        image_bytes, content_type, used_url = None, "image/jpeg", None
        for url in candidates:
            try:
                image_bytes, content_type = fetch_image_bytes(
                    self.session, url, article.link,
                    self.config.http_timeout, self.config.max_retries,
                )
                used_url = url
                break
            except Exception as exc:
                log.warning(
                    "  image download failed [%s] %s: %s", article.guid, url, exc
                )

        # Salvage pass: every listed candidate failed to download, so pull
        # candidates straight from the article's HTML page (og:image etc).
        if image_bytes is None and self.image_extractor:
            log.warning(
                "  no source image could be downloaded for [%s]; extracting from article HTML",
                article.guid,
            )
            try:
                extra = self.image_extractor(article, force_html=True) or []
            except Exception as exc:  # pragma: no cover
                log.warning("  HTML image salvage failed for [%s]: %s", article.guid, exc)
                extra = []
            for url in extra:
                if url in candidates:
                    continue
                try:
                    image_bytes, content_type = fetch_image_bytes(
                        self.session, url, article.link,
                        self.config.http_timeout, self.config.max_retries,
                    )
                    used_url = url
                    break
                except Exception as exc:
                    log.warning("  image download failed [%s] %s: %s", article.guid, url, exc)

        # No article image could be found/downloaded. The site logo is NEVER
        # substituted automatically: the original article image comes first
        # and only. When REQUIRE_ARTICLE_IMAGE is false the post is demoted to
        # a text-only post (still the exact article text). Otherwise we fail
        # safely and let the scheduler skip the article.
        if image_bytes is None:
            if not self.config.require_article_image:
                log.warning(
                    "  [%s] no downloadable article image; posting text-only "
                    "(REQUIRE_ARTICLE_IMAGE=false)",
                    article.guid,
                )
                return self._post_text(article, caption)
            raise NoUsableImageError(
                f"no usable article image for [{article.guid}] across all "
                f"extraction methods (REQUIRE_ARTICLE_IMAGE=true)"
            )

        log.info(
            "  Using article image (%d bytes, %s) from %s", len(image_bytes), content_type, used_url
        )
        return self._post_photo(article, image_bytes, content_type, caption)

    def _post_text(self, article: Article, caption: str) -> str:
        """Publish the exact article text without an image (only used when the
        article image cannot be found AND REQUIRE_ARTICLE_IMAGE=false)."""
        url = f"{GRAPH_BASE_URL}/{self.config.facebook_page_id}/feed"
        params = {
            "message": caption,
            "access_token": self.config.facebook_page_access_token,
        }
        return self._graph_request(url, params, "text")

    def _post_photo(self, article: Article, image_bytes: bytes, content_type: str, caption: str) -> str:
        # Native photo upload: multipart 'source' carries the actual image
        # file. No 'url' parameter, so Facebook never fetches the website and
        # no link preview can appear.
        image_bytes, content_type = self._prepare_photo_bytes(image_bytes, content_type)
        url = f"{GRAPH_BASE_URL}/{self.config.facebook_page_id}/photos"
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}.get(content_type, "jpg")
        files = {
            "source": (
                f"humshehri_{article.guid}.{ext}",
                io.BytesIO(image_bytes),
                content_type or "image/jpeg",
            )
        }
        params = {
            "caption": caption,
            "access_token": self.config.facebook_page_access_token,
        }
        return self._graph_request(url, params, "photo", files=files)

    @staticmethod
    def _prepare_photo_bytes(image_bytes: bytes, content_type: str) -> Tuple[bytes, str]:
        """Re-encode formats Meta's /photos endpoint may reject (e.g. WebP)
        in-memory to JPEG. If Pillow is unavailable or conversion fails we hand
        back the original bytes (best effort) so nothing is invented."""
        if content_type in ("image/jpeg", "image/png", "image/gif"):
            return image_bytes, content_type
        try:
            from PIL import Image
        except ImportError:
            log.warning("Pillow not installed; uploading '%s' image as-is", content_type)
            return image_bytes, content_type
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            log.info("Converted %s image to JPEG for Facebook upload", content_type)
            return buf.getvalue(), "image/jpeg"
        except Exception as exc:
            log.warning("Image conversion failed (%s); uploading original '%s'", exc, content_type)
            return image_bytes, content_type

    def validate_image(self, article: Article):
        """Download-check the article's best image without posting anything.
        Used by --dry-run so the pipeline can be tested safely."""
        candidates = list(article.image_candidates or [])
        if not candidates and self.image_extractor:
            try:
                candidates = self.image_extractor(article) or []
            except Exception:  # pragma: no cover
                candidates = []
        for url in candidates:
            try:
                image_bytes, content_type = fetch_image_bytes(
                    self.session, url, article.link,
                    self.config.http_timeout, self.config.max_retries,
                )
                return url, len(image_bytes), content_type
            except Exception as exc:
                log.warning("  [DRY RUN] image check failed [%s] %s: %s", article.guid, url, exc)
        return None

    def _graph_request(self, url: str, params: Dict[str, str], kind: str, files=None) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self.session.post(url, data=params, files=files, timeout=self.config.http_timeout)
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
                if dry_run:
                    self._dry_run_article(article)
                else:
                    post_id = self.poster.post(article)
                    log.info("  SUCCESS - posted [%s] -> Facebook post id %s", article.guid, post_id)
                    self.storage.mark_posted(article.guid, article.title, article.link)
                posted_count += 1
                if self.cron_mode:
                    self._set_cadence_gate()
            except (NoUsableImageError, OversizedCaptionError, ExtractionError) as exc:
                # The article cannot be faithfully posted (missing/invalid
                # image, exact text over Facebook's limit, or extraction
                # failed). Report the reason clearly and skip it permanently
                # instead of publishing something incomplete or fabricated.
                log.warning(
                    "  SKIPPED [%s] %s: %s", article.guid, article.title, exc,
                )
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

    def _dry_run_article(self, article: Article) -> None:
        """Show exactly what the bot extracted and would publish, without
        touching Facebook. No secrets are ever printed."""
        caption = build_caption_with_hashtags(article, FACEBOOK_MSG_LIMIT)
        tags: List[str] = []
        if article.add_hashtags:
            try:
                tags = _select_hashtags(article)
            except Exception as exc:  # pragma: no cover
                log.warning("  [DRY RUN] Hashtag generation failed: %s", exc)
        log.info("  [DRY RUN] ============ extracted article ============")
        log.info("  [DRY RUN] ARTICLE URL:          %s", article.link)
        log.info("  [DRY RUN] CANONICAL URL:        %s", article.canonical_url or "(none)")
        log.info("  [DRY RUN] SOURCE:               %s", article.source or "unknown")
        log.info("  [DRY RUN] EXTRACTION METHOD:    %s", article.extraction_method or "unknown")
        log.info("  [DRY RUN] ARTICLE TITLE:        %s", article.title)
        log.info(
            "  [DRY RUN] ARTICLE BODY:         %d characters, %d paragraphs "
            "(verbatim inside CAPTION below)",
            article.text_character_count, article.paragraph_count,
        )

        problems = validate_article(article, self.config)
        if problems:
            log.warning("  [DRY RUN] VALIDATION FAILED: %s", "; ".join(problems))

        img = self.poster.validate_image(article)
        if img:
            log.info("  [DRY RUN] IMAGE URL:          %s", img[0])
            log.info("  [DRY RUN] IMAGE STATUS:       OK (%d bytes, %s)", img[1], img[2])
        else:
            log.warning("  [DRY RUN] IMAGE URL:          (none found)")
            log.warning("  [DRY RUN] IMAGE STATUS:       FAILED - no downloadable article image")

        log.info(
            "  [DRY RUN] HASHTAGS:             %s",
            " ".join(tags) if tags else "(none)",
        )
        log.info("  [DRY RUN] FINAL CAPTION CHARACTER COUNT: %d", len(caption))
        log.info(
            "  [DRY RUN] CAPTION (%d chars, would post verbatim):",
            len(caption),
        )
        log.info("  [DRY RUN] ---- caption begin ----")
        for line in caption.splitlines():
            log.info("  %s", line)
        log.info("  [DRY RUN] ---- caption end ----")

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
    parser.add_argument("--test-extraction", nargs="+", metavar="URL",
                        help="Run the full extraction pipeline (canonical URL, exact title, "
                             "article body + image candidates from the article page) against "
                             "one or more article URLs, test-download each image candidate, "
                             "and report. Never touches Facebook.")
    return parser


def run_image_extraction_tests(config: Config, fetcher: ArticleFetcher, urls: List[str]) -> int:
    """Offline test harness (no Facebook calls).

    For each article URL it extracts the canonical URL, the page title and the
    exact article body, then reports every image candidate found — RSS/media if
    present, then the article page's og:image → JSON-LD → twitter:image → <img>
    chain — and test-downloads each one so you can confirm the whole pipeline
    without publishing anything.
    """
    # Map article URLs -> matching feed entries so RSS-carried images are tested too.
    feed_by_url: Dict[str, dict] = {}
    if feedparser is not None:
        try:
            resp = fetcher.session.get(config.rss_feed_url, timeout=config.http_timeout)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            feed_by_url = {
                (entry.get("link") or "").split("#")[0]: entry
                for entry in getattr(feed, "entries", [])
            }
        except Exception as exc:  # pragma: no cover
            log.warning("Could not read RSS feed for test (%s); using article-page extraction only", exc)

    for url in urls:
        print("=" * 80)
        print("ARTICLE:", url)
        canonical, page_title, body, page_images = _extract_article_from_page(
            fetcher._fetch_html(url) or "", url
        )
        print("CANONICAL URL:", canonical or "(not found)")
        print(f"PAGE TITLE:   {page_title or '(not found)'}")
        print(f"BODY:         {len(body)} characters, {_paragraph_count(body)} paragraphs")
        print("FIRST 12 LINES:")
        for line in body.splitlines()[:12]:
            print("   |", line)
        if len(body.splitlines()) > 12:
            print(f"   | ... ({len(body.splitlines())} lines total)")
        merged: List[Tuple[str, str]] = []

        entry = feed_by_url.get(url.split("#")[0])
        if entry:
            raw_content = entry.get("content", [{}])
            if isinstance(raw_content, list):
                raw_content = raw_content[0].get("value", "") if raw_content else ""
            else:
                raw_content = raw_content.get("value", "") if isinstance(raw_content, dict) else str(raw_content)
            for src in _rss_image_candidates(entry, raw_content, config.rss_feed_url):
                merged.append((src, "RSS/media"))
            print("  (feed entry found: RSS candidates below marked 'RSS/media')")

        for src, label in page_images:
            merged.append((src, label))

        seen: set = set()
        ordered: List[Tuple[str, str]] = []
        for src, label in merged:
            if src not in seen:
                seen.add(src)
                ordered.append((src, label))

        if not ordered:
            print("  >> NO VALID IMAGE FOUND")
            print("     (no RSS image, og:image, JSON-LD image, twitter:image or article <img>)")
            print()
            continue

        for i, (src, label) in enumerate(ordered, 1):
            print(f"  [{i}] ({label}) {src}")
            try:
                data, ctype = fetch_image_bytes(
                    fetcher.session, src, url, config.http_timeout, config.max_retries
                )
                print(f"      -> OK  {len(data)} bytes, {ctype}")
            except Exception as exc:
                print(f"      -> FAILED: {exc}")
            print()
    return 0


def main() -> int:
    args = build_cli().parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config)

    if not config.facebook_page_access_token and not (
        args.dry_run or args.list_new or args.test_extraction
    ):
        log.error(
            "FACEBOOK_PAGE_ACCESS_TOKEN is not set. "
            "Copy .env.example to .env and fill in your token, "
            "or run with --dry-run / --list-new / --test-extraction for a safe test."
        )
        return 1

    storage = Storage(config)
    fetcher = ArticleFetcher(config, storage)
    # The poster needs the fetcher as a fallback so that, when every feed image
    # fails to download, it can pull og:image/twitter/JSON-LD/<img> candidates
    # straight from the article's HTML page.
    poster = FacebookPoster(config, image_extractor=fetcher.resolve_image_candidates)
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
        if args.test_extraction:
            return run_image_extraction_tests(config, fetcher, args.test_extraction)
        if args.list_new:
            reports = fetcher.evaluate_candidates()
            if not reports:
                print("No articles returned by any source.")
                return 0
            print("= DISCOVERY REPORT (no posts are ever made in this mode) =")
            for r in reports:
                article = r["article"]
                pid = r["post_id"] or "-"
                src = "+".join(r["sources"]) or "-"
                title = r["title"] or "(no title)"
                img = "none"
                if article.image_candidates:
                    img = f"ok ({article.image_candidates[0]})"
                print(f"  {pid:<8} | {src:<8} | {r['status']:<15} | {title[:52]}")
                if r["url"]:
                    print(f"            | url:     {r['url']}")
                if r["canonical"]:
                    print(f"            | canonical: {r['canonical']}")
                if r["content_hash"]:
                    print(f"            | content: {r['content_hash']}")
                if img != "none":
                    print(f"            | image:   {img}")
                if r["duplicate_of"]:
                    print(f"            | duplicate of: {r['duplicate_of']}")
                if r["reason"]:
                    print(f"            | reason:  {r['reason']}")
                print()
            new = [r for r in reports if r["status"] == "ELIGIBLE"]
            print(
                f"SUMMARY: {len(reports)} candidate(s) checked; "
                f"{len(new)} eligible, "
                f"{sum(1 for r in reports if r['status'] == 'DUPLICATE')} duplicate, "
                f"{sum(1 for r in reports if r['status'] == 'FAILED')} failed extraction/validation, "
                f"{sum(1 for r in reports if r['status'] == 'SKIPPED_POSTED')} already posted."
            )
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
