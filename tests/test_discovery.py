"""Offline regression tests for the RSS + WordPress-API discovery pipeline.

No Facebook / no live network. Uses canned candidates + canned article pages and
a scratch SQLite database to verify the user-accepted discovery rules:

  A. a mis-numbered/stale RSS guid (e.g. ?p=1587, which actually points at a
     different article) is NOT trusted: the real post id, canonical URL and
     title are resolved from the final article page itself
  B. a brand-new article the RSS feed never lists is discovered via the
     WordPress REST API alone (runs alongside RSS, not just as a fallback)
  C. a second API-only article is discovered as genuinely eligible
  D. a WordPress "-2"-duplicate copy of an already-posted story is caught by
     the exact content fingerprint (never reposted under the new post id)
  E. an article found with no usable image FAILS under REQUIRE_ARTICLE_IMAGE=true
  F. the same article arriving from BOTH RSS and the API is merged and
     processed exactly once (API metadata wins, discovery sources recorded)

Usage:
    python tests/test_discovery.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import replace

import main

PASSED = 0
FAILED = 0


def check(label, condition):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")


TITLE_1621 = "نےپال اور چین میں شدید بارشیں، ڈوبنے سے اموات"
BODY_1621 = (
    "پاکستان کے پڑوسی ملک نیپال میں طوفانی بارشوں کے بعد سیلاب نے تباہی مچا دی ہے۔\n\n"
    "علاقے میں سینکڑوں گھر ڈوب گئے جبکہ درجنوں افراد لاپتہ ہیں۔\n\n"
    "مقامی حکام کے مطابق امدادی ٹیموں کو مصیبت زدہ علاقوں میں پہنچا دیا گیا ہے اور بچاؤ کا کام جاری ہے۔"
)
CANON_1621 = "https://www.humshehry.online/nepal-china-flash-floods/"
SHORT_1621 = "https://www.humshehry.online/?p=1621"

TITLE_1588 = "امریکہ میں کافی کی قیمتیں ریکارڈ سطح پر"
BODY_1588 = (
    "دنیا بھر میں کافی کی قیمتیں گزشتہ چالیس برسوں کی بلند ترین سطح پر پہنچ گئی ہیں۔\n\n"
    "تجزیہ کاروں کا کہنا ہے کہ موسمیاتی تبدیلی اور فصل کی کم پیداوار اس کا اہم سبب ہے۔\n\n"
    "اس سے صارفین کے روزمرہ اخراجات میں واضح اضافہ ہوا ہے اور دنیا کی معروف کافی چینز نے قیمتیں بڑھا دی ہیں۔"
)
CANON_1588 = "https://www.humshehry.online/us-coffee-prices-record/"

TITLE_1589 = "ناروے کے بادشاہ کا علاج جاری"
BODY_1589 = (
    "ناروے کے بادشاہ ہارلڈ پنجم اسپتال میں زیر علاج ہیں۔\n\n"
    "شاہی محل نے بیان میں کہا کہ بادشاہ کی طبیعت جانچی جا رہی ہے لیکن انہیں جلد صحتیاب ہونے کے آثار ہیں۔\n\n"
    "وزیراعظم نے بادشاہ کی جلد صحتیابی کے لیے نیک تمناؤں کا اظہار کیا ہے جبکہ ملک بھر میں دعائیں مانگی جا رہی ہیں۔"
)
CANON_1589 = "https://www.humshehry.online/norway-king-hospital/"
SHORT_1590 = "https://www.humshehry.online/?p=1590"

IMG = "https://www.humshehry.online/wp-content/uploads/2026/08/66820135_10158496140574584_8521089955512836096_n.jpg"


def page_html(pid, title, body, canonical, with_image=True):
    """Canned article page. The pid is exposed BOTH via <link rel=shortlink>
    (?p=<pid>) and the postid-<pid> body class, exactly like the live site."""
    paras = body.split("\n\n")
    content = "".join(f"<p>{p}</p>" for p in paras)
    img = (f'<meta property="og:image" content="{IMG}"/>') if with_image else ""
    return f"""<html><head>
<title>{title} | Humshehri</title>
<link rel="shortlink" href="https://www.humshehry.online/?p={pid}"/>
<link rel="canonical" href="{canonical}"/>
<meta property="og:title" content="{title}"/>
{img}
</head><body class="postid-{pid}">
<nav>Home | News | Contact</nav>
<div class="entry-content">{content}</div>
</body></html>"""


def make_config(tmp: Path) -> main.Config:
    cfg = main.Config.from_env()
    return replace(
        cfg,
        storage="sqlite",
        db_path=tmp / "discovery.db",
        rss_feed_url="https://www.humshehry.online/feed/",
        wp_api_url="https://www.humshehry.online/wp-json/wp/v2/posts",
        enable_wp_api_discovery=True,
        wp_api_lookback=20,
        strict_article_extraction=True,
        min_article_chars=120,
        require_article_image=True,
        post_with_image=True,
    )


def rss_candidate(guid, link, title, body):
    return main.Article(
        guid=guid, title=title, summary="", content=body, link=link,
        image_candidates=[],
        source_hosts=["www.humshehry.online", "humshehry.online"],
        published_at=None, add_hashtags=False, max_hashtags=6,
        source="rss", sources=["rss"],
        api_post_id=main.ArticleFetcher._post_id_from_guid(guid),
    )


def api_candidate(pid, link, title, body):
    return main.Article(
        guid=link, title=title, summary="", content=body, link=link,
        image_candidates=[IMG],
        source_hosts=["www.humshehry.online", "humshehry.online"],
        published_at=None, add_hashtags=False, max_hashtags=6,
        source="wp-api", sources=["wp-api"], api_post_id=str(pid),
    )


def new_fetcher(tmp: Path):
    config = make_config(tmp)
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    return config, storage, fetcher


# ---------------------------------------------------------------------------
# A. Mis-numbered RSS guid -> real post identity resolved from the article page
# ---------------------------------------------------------------------------
def test_a_rss_guid_not_trusted(config):
    flow = "A: stale RSS guid ?p=1587 is resolved to the real post (1621) from its page"
    print(f"\n=== {flow} ===")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    # The RSS feed carries an OLD/mis-numbered guid: it labels this entry
    # "?p=1587" with a Norway headline, but the URL actually resolves to the
    # real Nepal article (post 1621). Only the final page is authoritative.
    stale_guid = "https://www.humshehry.online/?p=1587"
    fetcher._fetch_via_rss = lambda limit=10: [
        rss_candidate(stale_guid, stale_guid, "ناروے کا بادشاہ اسپتال میں", BODY_1621)
    ]
    fetcher._fetch_via_wp_api = lambda limit=20: []
    fetcher._fetch_html = lambda url: page_html(1621, TITLE_1621, BODY_1621, CANON_1621)

    reports = fetcher.evaluate_candidates(10)
    check("one candidate evaluated", len(reports) == 1)
    r = reports[0]
    check("status ELIGIBLE (not tied to the bogus RSS id)", r["status"] == "ELIGIBLE")
    check("real post id 1621 resolved from the page (not 1587)",
          r["post_id"] == "1621")
    check("canonical URL resolved from the page", r["canonical"] == CANON_1621)
    check("page title replaced the bogus RSS title", r["title"] == TITLE_1621)
    check("article guid became the canonical URL", r["article"].guid == CANON_1621)
    check("page image candidate present", len(r["article"].image_candidates) > 0)
    check("body is the real article text",
          r["article"].content == BODY_1621)
    check("content fingerprint computed", len(r["content_hash"]) == 64)
    fetcher.close()
    storage.close()


# ---------------------------------------------------------------------------
# B. New article discovered from the WordPress API alone (RSS never lists it)
# ---------------------------------------------------------------------------
def test_b_api_only_discovery(config):
    flow = "B: new post 1588 found via the WP REST API only (feed misses it)"
    print(f"\n=== {flow} ===")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    fetcher._fetch_via_rss = lambda limit=10: []
    fetcher._fetch_via_wp_api = lambda limit=20: [
        api_candidate(1588, CANON_1588, TITLE_1588, BODY_1588)
    ]
    fetcher._fetch_html = lambda url: page_html(1588, TITLE_1588, BODY_1588, CANON_1588)

    reports = fetcher.evaluate_candidates(10)
    check("one candidate evaluated", len(reports) == 1)
    r = reports[0]
    check("status ELIGIBLE", r["status"] == "ELIGIBLE")
    check("discovered by the API alone", r["sources"] == ["wp-api"])
    check("post id 1588", r["post_id"] == "1588")
    check("title kept verbatim (exact copy)", r["title"] == TITLE_1588)
    check("body the exact article text", r["article"].content == BODY_1588)
    check("image candidate present", len(r["article"].image_candidates) > 0)
    fetcher.close()
    storage.close()


# ---------------------------------------------------------------------------
# C. Second API-only article is genuinely eligible
# ---------------------------------------------------------------------------
def test_c_second_api_only_discovery(config):
    flow = "C: new post 1589 found via the API alone and eligible"
    print(f"\n=== {flow} ===")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    fetcher._fetch_via_rss = lambda limit=10: []
    fetcher._fetch_via_wp_api = lambda limit=20: [
        api_candidate(1589, CANON_1589, TITLE_1589, BODY_1589)
    ]
    fetcher._fetch_html = lambda url: page_html(1589, TITLE_1589, BODY_1589, CANON_1589)

    reports = fetcher.evaluate_candidates(10)
    check("one candidate evaluated", len(reports) == 1)
    r = reports[0]
    check("status ELIGIBLE", r["status"] == "ELIGIBLE")
    check("post id 1589", r["post_id"] == "1589")
    fetcher.close()
    storage.close()


# ---------------------------------------------------------------------------
# D. Posted "-2" duplicate caught by the exact content fingerprint
# ---------------------------------------------------------------------------
def test_d_content_duplicate_detected(config):
    flow = ("D: WP API returns the SAME Norway story twice (1589 + a '-2' copy that "
            "'?p=1590' was posted under) -> duplicate is never reposted")
    print(f"\n=== {flow} ===")
    storage = main.Storage(config)
    # The story was already posted as ?p=1590 (and its content fingerprint was
    # backfilled from the API).
    storage.mark_posted(SHORT_1590, TITLE_1589, SHORT_1590)
    fetcher = main.ArticleFetcher(config, storage)
    fetcher._fetch_via_rss = lambda limit=10: []
    fetcher._fetch_via_wp_api = lambda limit=20: [
        api_candidate(1589, CANON_1589, TITLE_1589, BODY_1589),
        api_candidate(1590, CANON_1589, TITLE_1589, BODY_1589),
    ]
    fetcher._fetch_html = lambda url: page_html(1589, TITLE_1589, BODY_1589, CANON_1589)

    reports = fetcher.evaluate_candidates(10)
    check("both candidates were evaluated", len(reports) == 2)
    by_pid = {r["post_id"]: r for r in reports}
    check("1589 reported", "1589" in by_pid)
    check("1590 reported", "1590" in by_pid)
    check("1589 is DUPLICATE (same content as already-posted 1590)",
          by_pid["1589"]["status"] == "DUPLICATE")
    check("1589 duplicate_of names 1590",
          "1590" in (by_pid["1589"].get("duplicate_of") or ""))
    check("1590 itself is SKIPPED_POSTED (already in the DB)",
          by_pid["1590"]["status"] == "SKIPPED_POSTED")
    check("1589 was marked processed (never discovered again)",
          storage.is_posted(CANON_1589) or storage.is_posted(SHORT_1621.replace("1621", "1589")))
    fetcher.close()
    storage.close()


# ---------------------------------------------------------------------------
# E. No usable image fails under REQUIRE_ARTICLE_IMAGE=true
# ---------------------------------------------------------------------------
def test_e_no_image_fails_strict(config):
    flow = "E: article with no image anywhere -> FAILED (never eligible)"
    print(f"\n=== {flow} ===")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    fetcher._fetch_via_rss = lambda limit=10: []

    def no_image_api(limit=20):
        a = api_candidate(1588, CANON_1588, TITLE_1588, BODY_1588)
        a.image_candidates = []  # WP API / feed carried no image either
        return [a]

    fetcher._fetch_via_wp_api = no_image_api
    fetcher._fetch_html = lambda url: page_html(1588, TITLE_1588, BODY_1588, CANON_1588, with_image=False)

    reports = fetcher.evaluate_candidates(10)
    check("one candidate evaluated", len(reports) == 1)
    r = reports[0]
    check("no image candidate survived hydration (control)",
          len(r["article"].image_candidates) == 0)
    check("status FAILED (image requirement not met)", r["status"] == "FAILED")
    check("reason says no article image found",
          "no article image" in (r.get("reason") or ""))
    check("failed article was NOT marked as posted",
          not storage.is_posted(CANON_1588))
    fetcher.close()
    storage.close()


def test_e2_source_image_survives(config):
    flow = ("E2: API declared an image even though the page parser found none -> "
            "image kept (WP featured media is authoritative), article eligible")
    print(f"\n=== {flow} ===")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    fetcher._fetch_via_rss = lambda limit=10: []
    fetcher._fetch_via_wp_api = lambda limit=20: [
        api_candidate(1588, CANON_1588, TITLE_1588, BODY_1588)
    ]
    fetcher._fetch_html = lambda url: page_html(1588, TITLE_1588, BODY_1588, CANON_1588, with_image=False)

    reports = fetcher.evaluate_candidates(10)
    check("one candidate evaluated", len(reports) == 1)
    r = reports[0]
    check("API-declared image survived hydration", r["article"].image_candidates == [IMG])
    check("status ELIGIBLE (image present from the source)", r["status"] == "ELIGIBLE")
    fetcher.close()
    storage.close()


# ---------------------------------------------------------------------------
# F. Same article from RSS AND API is merged -> processed exactly once
# ---------------------------------------------------------------------------
def test_f_rss_and_api_merged(config):
    flow = ("F: post 1588 arrives from BOTH the RSS feed and the API -> processed "
            "once, API metadata wins, both sources recorded")
    print(f"\n=== {flow} ===")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    # RSS entry has the real guid but a WRONG/truncated label and no image.
    fetcher._fetch_via_rss = lambda limit=10: [
        rss_candidate("https://www.humshehry.online/?p=1588", "https://www.humshehry.online/?p=1588",
                      "کافی", BODY_1588)
    ]
    fetcher._fetch_via_wp_api = lambda limit=20: [
        api_candidate(1588, CANON_1588, TITLE_1588, BODY_1588)
    ]
    fetcher._fetch_html = lambda url: page_html(1588, TITLE_1588, BODY_1588, CANON_1588)

    reports = fetcher.evaluate_candidates(10)
    check("exactly one candidate processed (merged, not twice)",
          len(reports) == 1)
    r = reports[0]
    check("status ELIGIBLE", r["status"] == "ELIGIBLE")
    check("sources recorded as rss + wp-api", set(r["sources"]) == {"rss", "wp-api"})
    check("API metadata won (full title, not the RSS label)",
          r["title"] == TITLE_1588)
    check("post id 1588", r["post_id"] == "1588")
    check("canonical resolved", r["canonical"] == CANON_1588)
    fetcher.close()
    storage.close()


def run():
    with tempfile.TemporaryDirectory() as tmp:
        base_config = make_config(Path(tmp))
        test_a_rss_guid_not_trusted(replace(base_config, db_path=Path(tmp) / "a.db"))
        test_b_api_only_discovery(replace(base_config, db_path=Path(tmp) / "b.db"))
        test_c_second_api_only_discovery(replace(base_config, db_path=Path(tmp) / "c.db"))
        test_d_content_duplicate_detected(replace(base_config, db_path=Path(tmp) / "d.db"))
        test_e_no_image_fails_strict(replace(base_config, db_path=Path(tmp) / "e.db"))
        test_e2_source_image_survives(replace(base_config, db_path=Path(tmp) / "e2.db"))
        test_f_rss_and_api_merged(replace(base_config, db_path=Path(tmp) / "f.db"))

    print()
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(run())