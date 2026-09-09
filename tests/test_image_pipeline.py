"""Offline tests for the image pipeline (no Facebook / no live network).

Runs against canned HTML / fake sessions so it is safe to execute anywhere.
It covers the required cases:

  1. RSS contains an image.
  2. RSS has no image but the article HTML has og:image.
  3. The image is available only in the article HTML (<img>).
  4. Article with no valid image (candidates rejected).
  5. Successful native Facebook photo post (multipart 'source', no 'url').
  6. No downloadable article image -> strict mode refuses to post; the
     site logo is never substituted; text-only posting when
     REQUIRE_ARTICLE_IMAGE=false.
  7. The caption is exactly the extracted article text (no hashtags,
     rewrites, URL scrubbing or truncation).
  8. Failed Facebook upload.

Usage:
    python tests/test_image_pipeline.py
"""

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ---------------------------------------------------------------------------
# Fake HTTP bits
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = content
        self._text = payload if isinstance(payload, str) else json.dumps(payload or {})

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if not self.ok:
            from requests import HTTPError
            raise HTTPError(f"{self.status_code} Client Error")

    def json(self):
        return json.loads(self._text)


class FakeSession:
    def __init__(self, image_resp, post_resp=None):
        self.image_resp = image_resp
        self.post_resp = post_resp or FakeResponse(200, {"id": "12345_67890"})
        self.recorded = []

    def get(self, url, **kwargs):
        return self.image_resp

    def post(self, url, data=None, files=None, **kwargs):
        self.recorded.append({"url": url, "data": data, "files": files})
        return self.post_resp

    def close(self):
        pass


GIF = (b"GIF89a" + b"\x00" * 16)  # tiny dummy image body


def image_response():
    return FakeResponse(200, headers={"Content-Type": "image/gif"}, content=GIF)


def html_response():
    return FakeResponse(200, headers={"Content-Type": "text/html"},
                        content=b"<html><body>WAF error page</body></html>")


# ---------------------------------------------------------------------------
# 1. RSS contains an image
# ---------------------------------------------------------------------------
def test_rss_image():
    print("Test 1: RSS contains an image")
    entry = {"link": "https://s.example/1",
             "media_content": [{"type": "image/jpeg",
                                "url": "https://s.example/a.jpg"}],
             "content": [{"value": "<p>hello</p>"}]}
    got = main._rss_image_candidates(entry, "", "")
    check("media:content image is found", got == ["https://s.example/a.jpg"])

    entry = {"link": "https://s.example/2", "enclosures": []}
    body = '<p><img src="https://s.example/wp-content/uploads/2026/08/main.jpg"></p>'
    got = main._rss_image_candidates(entry, body, "")
    check("first <img> in RSS content is used as fallback",
          got == ["https://s.example/wp-content/uploads/2026/08/main.jpg"])

    entry = {"link": "https://s.example/3",
             "media_thumbnail": [{"url": "https://s.example/thumb.jpg"}]}
    got = main._rss_image_candidates(entry, "", "")
    check("media:thumbnail image is found", got == ["https://s.example/thumb.jpg"])


# ---------------------------------------------------------------------------
# 2. RSS has no image, article HTML has og:image
# ---------------------------------------------------------------------------
OG_HTML = """<html><head>
<meta property="og:image" content="https://www.humshehry.online/wp-content/uploads/2026/08/og.webp"/>
<meta name="twitter:image" content="https://www.humshehry.online/wp-content/uploads/2026/08/tw.webp"/>
<script type="application/ld+json">{"@type":"NewsArticle","image":{"@type":"ImageObject","url":"https://www.humshehry.online/wp-content/uploads/2026/08/ld.webp"}}</script>
</head><body>
<header><img src="https://www.humshehry.online/wp-content/themes/x/images/logo.png"/></header>
<article><figure><img src="https://www.humshehry.online/wp-content/uploads/2026/08/main.webp" width="640" height="360"/></figure></article>
</body></html>"""


def test_resolve_with_og_image(config):
    print("Test 2: RSS has no image, article HTML has og:image")
    got = main._extract_html_image_candidates(OG_HTML, "https://www.humshehry.online/?p=1")
    order = [u for u, _ in got]
    check("og:image candidate found", "https://www.humshehry.online/wp-content/uploads/2026/08/og.webp" in order)
    check("og:image comes before twitter/jsonld/img",
          order.index("https://www.humshehry.online/wp-content/uploads/2026/08/og.webp") == 0)
    check("logo <img> excluded", not any("logo" in u for u in order))
    check("twitter:image also collected",
          "https://www.humshehry.online/wp-content/uploads/2026/08/tw.webp" in order)
    check("json-ld image also collected",
          "https://www.humshehry.online/wp-content/uploads/2026/08/ld.webp" in order)
    check("article <img> collected last",
          order[-1] == "https://www.humshehry.online/wp-content/uploads/2026/08/main.webp")

    # Full fetcher-level flow: empty RSS candidates -> article HTML resolution.
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    fetcher._fetch_html = lambda url: OG_HTML  # stop the real network call
    article = main.Article(guid="x", title="t", summary="s", content="c", link="https://www.humshehry.online/?p=1")
    resolved = fetcher.resolve_image_candidates(article)
    check("resolve_image_candidates fills candidates from article HTML",
          resolved and resolved[0] == "https://www.humshehry.online/wp-content/uploads/2026/08/og.webp")
    fetcher.close()
    storage.close()


# ---------------------------------------------------------------------------
# 3. Image available only in the article HTML (<img>, no meta tags)
# ---------------------------------------------------------------------------
def test_html_only_image():
    print("Test 3: image only in article HTML")
    html = """<html><body>
    <div class="entry-content">
      <img src="/wp-content/uploads/2026/08/body.jpg" alt="photo"/>
    </div>
    </body></html>"""
    got = main._extract_html_image_candidates(html, "https://www.humshehry.online/article-xyz")
    check("relative img URL is resolved to absolute",
          got == [("https://www.humshehry.online/wp-content/uploads/2026/08/body.jpg", "article:<img>")])


# ---------------------------------------------------------------------------
# 4. No valid image
# ---------------------------------------------------------------------------
def test_no_image():
    print("Test 4: no valid image")
    html = """<html><head></head><body>
    <p>only text, no media</p>
    <img src="https://www.humshehry.online/wp-content/themes/x/images/logo.png"/>
    <img src="https://px.example/t/1x1.gif" width="1" height="1"/>
    </body></html>"""
    got = main._extract_html_image_candidates(html, "https://www.humshehry.online/?p=none")
    check("logo + tracking pixel are rejected -> empty candidates", got == [])


# ---------------------------------------------------------------------------
# 5. Successful native Facebook photo post (multipart source, no url param)
# ---------------------------------------------------------------------------
def test_native_photo_post(config):
    print("Test 5: successful native Facebook photo post")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    article = main.Article(
        guid="42", title="عنوان", summary="", content="مکمل متن https://www.humshehry.online/xyz",
        link="https://www.humshehry.online/?p=42",
        image_candidates=["https://cdn.example/img/main.jpg"],
        source_hosts=["www.humshehry.online", "humshehry.online"],
    )
    session = FakeSession(image_response())
    poster = main.FacebookPoster(config, image_extractor=fetcher.resolve_image_candidates)
    poster.session = session
    post_id = poster.post(article)
    check("returns Facebook post id", post_id == "12345_67890")
    rec = session.recorded[0]
    data, files = rec["data"], rec["files"]
    check("uses /photos endpoint", rec["url"].endswith("/photos"))
    check("access token sent", data.get("access_token") == config.facebook_page_access_token)
    check("caption sent", data.get("caption") == article.facebook_caption)
    # URLs that are part of the article body are preserved verbatim (not scrubbed).
    check("genuine content URL preserved in caption",
          "https://www.humshehry.online/xyz" in data.get("caption", ""))
    check("caption is exactly title + article body (nothing auto-added)",
          data.get("caption") == "عنوان\n\nمکمل متن https://www.humshehry.online/xyz")
    check("no 'url' parameter passed (no link share/preview)", "url" not in data)
    check("native upload: 'source' file present", "source" in files)
    fname, fobj, ctype = files["source"]
    check("file named for article", fname == "humshehri_42.gif" and ctype == "image/gif")
    check("uploaded bytes match downloaded image", fobj.getvalue() == GIF)
    fetcher.close()
    storage.close()


def test_caption_preserves_article_text():
    print("Caption: exact article text, no hashtags/rewrites/truncation")
    text = ("اسلام آباد میں آج اہم اجلاس ہوا۔\n\n"
            "دوسری پیراگراف: 2026 میں 12,345 روپے، \"قیمت\" اور 3.5 فیصد۔\n\n"
            "مزید: پاکستان کی معیشت ۱۴ سال میں 22.4% بڑھی (2015 تا 2026).")
    # No hashtags by default (ADD_HASHTAGS=false default).
    article = main.Article(
        guid="x", title="اجلاس ہوا", summary="", content=text,
        link="https://www.humshehry.online/?p=123", add_hashtags=False,
    )
    caption = article.facebook_caption
    check("no hashtags added by default", "#" not in caption)
    check("title preserved", caption.startswith("اجلاس ہوا"))
    check("article body preserved verbatim", text in caption)
    check("paragraph breaks preserved", caption == f"اجلاس ہوا\n\n{text}")
    check("no artificial truncation", len(caption) == len(f"اجلاس ہوا\n\n{text}"))


# ---------------------------------------------------------------------------
# 6. Failed image download / upload
# ---------------------------------------------------------------------------
def test_download_failure(config):
    print("Test 6a: all image downloads fail -> NoUsableImageError (no logo fallback)")

    def no_image_session(url):
        return html_response()

    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    article = main.Article(
        guid="43", title="t", summary="", content="c",
        link="https://www.humshehry.online/?p=43",
        image_candidates=["https://cdn.example/img/broken.jpg"],
    )
    session = FakeSession(html_response())  # article image returns HTML error body
    poster = main.FacebookPoster(config, image_extractor=fetcher.resolve_image_candidates)
    poster.session = session
    try:
        poster.post(article)
        check("strict mode refuses to post a broken-image article", False)
    except main.NoUsableImageError:
        check("strict mode refuses to post a broken-image article", True)
    check("no Facebook request was made", len(session.recorded) == 0)
    fetcher.close()
    storage.close()


def test_no_image_candidates_strict(config):
    print("Test 6d: strict mode: article with no image at all is not posted")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    article = main.Article(
        guid="46", title="t", summary="", content="c",
        link="https://www.humshehry.online/?p=46",
        image_candidates=[],  # no RSS/media/og image at all
    )
    session = FakeSession(image_response())
    poster = main.FacebookPoster(config, image_extractor=fetcher.resolve_image_candidates)
    poster.session = session
    try:
        poster.post(article)
        check("missing image fails publishing in strict mode", False)
    except main.NoUsableImageError:
        check("missing image fails publishing in strict mode", True)
    check("logo is NOT used as an automatic fallback", session.recorded == [])
    fetcher.close()
    storage.close()


def test_no_image_non_strict_posts_text(config):
    print("Test 6e: REQUIRE_ARTICLE_IMAGE=false -> text-only post (no logo image)")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    cfg = replace(config, require_article_image=False)
    article = main.Article(
        guid="47", title="عنوان", summary="", content="متن",
        link="https://www.humshehry.online/?p=47",
        image_candidates=[],
    )
    session = FakeSession(image_response())
    poster = main.FacebookPoster(cfg, image_extractor=fetcher.resolve_image_candidates)
    poster.session = session
    post_id = poster.post(article)
    check("text-only post succeeds", post_id == "12345_67890")
    rec = session.recorded[0]
    check("uses /feed endpoint (no photo)", rec["url"].endswith("/feed"))
    check("message is the exact article text", rec["data"].get("message") == "عنوان\n\nمتن")
    check("no logo image uploaded", rec["files"] is None)
    fetcher.close()
    storage.close()


def test_upload_failure(config):
    print("Test 6b: failed Facebook upload")
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    article = main.Article(
        guid="44", title="t", summary="", content="c",
        link="https://www.humshehry.online/?p=44",
        image_candidates=["https://cdn.example/img/ok.jpg"],
    )
    err_resp = FakeResponse(400, {
        "error": {"code": 190, "message": "Invalid OAuth access token"}
    })
    session = FakeSession(image_response(), post_resp=err_resp)
    poster = main.FacebookPoster(config, image_extractor=fetcher.resolve_image_candidates)
    poster.session = session
    try:
        poster.post(article)
        check("Graph API error propagates (no success)", False)
    except Exception as exc:
        check("Graph API error propagates (no success)", isinstance(exc, Exception) and "Graph API" in str(exc))
    check("article not marked posted (storage untouched)", not storage.is_posted("44"))
    fetcher.close()
    storage.close()


def test_scheduler_skips_no_image(config):
    print("Test 6c: scheduler skips a no-image article without crashing")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = replace(config, db_path=Path(tmp) / "test.db")
        storage = main.Storage(cfg)
        article = main.Article(
            guid="45", title="بدون تصویر", summary="", content="متن",
            link="https://www.humshehry.online/?p=45",
            image_candidates=[],
        )

        class StubFetcher:
            def fetch_new_articles(self, limit=10):
                return [article]

        class StubPoster:
            def __init__(self):
                self.session = FakeSession(image_response())
                self.image_extractor = None

            def post(self, a):
                raise main.NoUsableImageError("no image")

            def close(self):
                pass

        scheduler = main.Scheduler(cfg, StubFetcher(), StubPoster(), storage,
                                   no_delay=True, max_posts=0)
        count = scheduler.run_once(dry_run=False)
        check("article counted as processed", count == 1)
        check("no-image article marked as posted (skipped)", storage.is_posted("45"))
        storage.close()


def run():
    config = main.Config.from_env()

    test_rss_image()
    test_resolve_with_og_image(config)
    test_html_only_image()
    test_no_image()
    test_native_photo_post(config)
    test_caption_preserves_article_text()
    test_download_failure(config)
    test_no_image_candidates_strict(config)
    test_no_image_non_strict_posts_text(config)
    test_upload_failure(config)
    test_scheduler_skips_no_image(config)

    print()
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(run())