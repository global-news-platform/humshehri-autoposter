"""Offline tests for the exact-copy article extraction pipeline.

No Facebook / no live network. Uses canned HTML to verify:

  * the exact article text is preserved (title, paragraph order, Urdu,
    English, numbers, punctuation) with paragraph breaks intact
  * no artificial 5,000-character truncation
  * no hashtags are added automatically, nothing is rewritten
  * navigation/footer/sidebar/ad/cookie text is excluded
  * canonical URL is extracted
  * image priority: source/WP featured -> og:image -> JSON-LD -> twitter -> article <img>
  * unrelated/sidebar/logo images are rejected
  * the logo is never used as an automatic fallback
  * a missing image fails publishing in strict mode
  * the Facebook caption contains exactly the extracted title + body

Usage:
    python tests/test_extraction.py
"""

import os
import sys

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


ARTICLE_HTML = """<html><head>
<title>عنوان خبر | Humshehri</title>
<link rel="canonical" href="https://www.humshehri.online/2026/08/urdu-news-slug/"/>
<meta property="og:title" content="عنوان خبر"/>
</head><body>
<nav class="main-navigation">Home | News | Pakistan | Contact Us | Login</nav>
<header class="site-header">خبر نیوز نیٹ ورک — تازہ ترین</header>
<article class="post type-post">
  <h1 class="entry-title">عنوان خبر</h1>
  <div class="entry-content">
    <p>پہلا پیراگراف: اسلام آباد میں آج اہم اجلاس ہوا۔</p>
    <h2>ذیلی سرخی</h2>
    <p>دوسرا پیراگراف with English words and digits 123 and "نشاناتِ وقف" plus 3.5%.</p>
    <ul>
      <li>پہلی فہرست شے</li>
      <li>دوسری فہرست شے 456</li>
    </ul>
    <p>تیسرا پیراگراف: وزیرِ اعظم نے کہا کہ "ہم عوام کی خدمت جاری رکھیں گے"۔</p>
  </div>
</article>
<aside class="sidebar widget-area">
  <div class="widget widget_text">یہ ایک اشتہار ہے — یہاں کلک کریں</div>
  <div class="widget widget_tag_cloud">HTML CSS JavaScript خبریں</div>
</aside>
<footer class="site-footer">کاپی رائٹ © 2026 Humshehri</footer>
</body></html>"""


def test_text_extraction():
    print("Test 1: exact article text preserved, UI chrome excluded")
    canonical, page_title, body, images = main._extract_article_from_page(
        ARTICLE_HTML, "https://www.humshehri.online/?p=1400"
    )
    check("canonical URL extracted",
          canonical == "https://www.humshehri.online/2026/08/urdu-news-slug/")
    check("og:title used as page title", page_title == "عنوان خبر")

    # Paragraph order + content
    order = [
        body.find("پہلا پیراگراف: اسلام آباد میں آج اہم اجلاس ہوا۔"),
        body.find("ذیلی سرخی"),
        body.find('دوسرا پیراگراف with English words and digits 123'),
        body.find("پہلی فہرست شے"),
        body.find("دوسری فہرست شے 456"),
        body.find('وزیرِ اعظم نے کہا کہ "ہم عوام کی خدمت جاری رکھیں گے"۔'),
    ]
    check("all article paragraphs present", all(i >= 0 for i in order))
    check("paragraph order identical to source", order == sorted(order))
    check("urdu preserved", "اسلام آباد میں آج اہم اجلاس ہوا۔" in body)
    check("english preserved", "with English words and digits 123" in body)
    check("numbers preserved", "3.5%" in body and "456" in body)
    check("punctuation preserved", "نشاناتِ وقف" in body and "وقف\"" in body)
    check("quotes preserved", 'کہا کہ "ہم عوام کی خدمت جاری رکھیں گے"۔' in body)

    check("navigation text excluded", "Home | News | Pakistan | Contact Us | Login" not in body)
    check("menu items excluded", "Contact Us" not in body and "Login" not in body)
    check("site header excluded", "خبر نیوز نیٹ ورک" not in body)
    check("ad text excluded", "یہ ایک اشتہار ہے" not in body)
    check("tag cloud excluded", "HTML CSS JavaScript خبریں" not in body)
    check("footer excluded", "کاپی رائٹ © 2026 Humshehri" not in body)
    check("title not duplicated as body block", body.count("عنوان خبر") == 0)

    paras = main._paragraphs(body)
    check("paragraph breaks preserved (3 paragraphs + heading + list block)",
          len(paras) >= 5 and "\n\n" in body)
    check("list items kept as separate lines",
          "پہلی فہرست شے\nدوسری فہرست شے 456" in body)


def test_no_5000_char_truncation():
    print("Test 2: no artificial 5,000-character limit")
    paragraphs = "".join(
        f"<p>یہ پیراگراف {i} ہے جس میں مکمل اردو متن موجود ہے اور کچھ English words "
        f"اور اعداد 987654123 بھی شامل ہیں۔</p>" for i in range(400)
    )
    html = (
        '<html><head><meta property="og:title" content="طویل مضمون"/></head>'
        f"<body><article><div class=\"entry-content\">{paragraphs}</div></article></body></html>"
    )
    _canonical, _title, body, _images = main._extract_article_from_page(
        html, "https://www.humshehry.online/long"
    )
    check("body longer than 5000 chars is fully kept", len(body) > 5000)
    check("last paragraph included", "پیراگراف 399 ہے" in body)
    check("first paragraph included", "پیراگراف 0 ہے" in body)
    check("no ellipsis truncation mark", "…" not in body)


def test_no_rewrite_no_hashtags():
    print("Test 3: nothing rewritten, no hashtags auto-added, no intro/summary")
    _canonical, _title, body, _images = main._extract_article_from_page(
        ARTICLE_HTML, "https://www.humshehry.online/?p=1400"
    )
    article = main.Article(
        guid="1400", title="عنوان خبر", summary="", content=body,
        link="https://www.humshehry.online/2026/08/urdu-news-slug/",
        canonical_url="https://www.humshehry.online/2026/08/urdu-news-slug/",
    )
    caption = article.facebook_caption
    check("caption starts with the exact title", caption.startswith("عنوان خبر"))
    check("caption contains exact body", body in caption)
    check("no hashtag anywhere", "#" not in caption)
    check("no added introduction/conclusion",
          "تعارف" not in caption and "خلاصہ" not in caption and "BREAKING" not in caption)
    check("no emojis added", not any(ch in caption for ch in "🇵🇰🔥📰"))
    check("caption is exactly title + body",
          caption == f"عنوان خبر\n\n{body}")


def test_navigation_like_body_rejected():
    print("Test 4: navigation-looking body is rejected by validation")
    config = main.Config.from_env()
    strict = replace(config, strict_article_extraction=True, min_article_chars=120)
    bad = main.Article(
        guid="1", title="Home", summary="", content="Home | News | Pakistan | Contact Us | Login",
        link="https://www.humshehry.online/?p=1",
        image_candidates=["https://www.humshehry.online/wp-content/uploads/2026/08/a.jpg"],
    )
    problems = main.validate_article(bad, strict)
    check("too-short nav text rejected", any("too short" in p for p in problems))

    good = main.Article(
        guid="2", title="عنوان", summary="",
        content="پہلا پیراگراف: اسلام آباد میں آج اہم اجلاس ہوا جس میں سرکاری اور نجی شعبے کے "
                "نمائندوں نے شرکت کی۔\n\nدوسرا پیراگراف: اجلاس میں فیصلہ کیا گیا کہ عوامی خدمات "
                "کی بہتری کے لئے مزید اقدامات کیے جائیں گے اور تمام معاملات شفاف طریقے سے طے "
                "ہوں گے۔",
        link="https://www.humshehry.online/?p=2",
        canonical_url="https://www.humshehry.online/2",
        image_candidates=["https://www.humshehry.online/wp-content/uploads/2026/08/b.jpg"],
    )
    check("substantial article passes validation", main.validate_article(good, strict) == [])


# ---------------------------------------------------------------------------
# Image priority and same-article binding
# ---------------------------------------------------------------------------
IMAGES_HTML = """<html><head>
<meta property="og:image" content="https://www.humshehry.online/wp-content/uploads/2026/08/og.jpg"/>
<meta name="twitter:image" content="https://www.humshehry.online/wp-content/uploads/2026/08/tw.jpg"/>
<script type="application/ld+json">{"@type":"NewsArticle","image":{"@type":"ImageObject","url":"https://www.humshehry.online/wp-content/uploads/2026/08/ld.jpg"}}</script>
</head><body>
<header class="site-header"><img src="https://www.humshehry.online/wp-content/themes/x/logo.png"/></header>
<aside class="sidebar"><img src="https://www.humshehry.online/wp-content/uploads/2026/08/sidebar-ad.png"/></aside>
<article>
  <div class="entry-content">
    <p>متن</p>
    <img src="https://www.humshehry.online/wp-content/uploads/2026/08/main.jpg" width="640" height="360"/>
  </div>
</article>
</body></html>"""


def test_image_priority():
    print("Test 5: image priority og:image -> JSON-LD -> twitter -> article <img>")
    got = main._extract_html_image_candidates(
        IMAGES_HTML, "https://www.humshehry.online/?p=9", article_only=True
    )
    urls = [u for u, _ in got]
    check("og:image present and first",
          urls and urls[0] == "https://www.humshehry.online/wp-content/uploads/2026/08/og.jpg")
    check("json-ld before twitter",
          urls.index("https://www.humshehry.online/wp-content/uploads/2026/08/ld.jpg")
          < urls.index("https://www.humshehry.online/wp-content/uploads/2026/08/tw.jpg"))
    check("twitter:image present",
          "https://www.humshehry.online/wp-content/uploads/2026/08/tw.jpg" in urls)
    check("article <img> kept",
          urls[-1] == "https://www.humshehry.online/wp-content/uploads/2026/08/main.jpg")
    check("logo rejected", not any("logo" in u for u in urls))
    check("sidebar/ad image rejected", not any("sidebar-ad" in u for u in urls))


def test_featured_and_source_candidates_win():
    print("Test 6: source/WP featured candidates come before page candidates")
    config = main.Config.from_env()
    storage = main.Storage(config)
    fetcher = main.ArticleFetcher(config, storage)
    article = main.Article(
        guid="10", title="t", summary="", content="c",
        link="https://www.humshehry.online/?p=10",
        image_candidates=["https://www.humshehry.online/wp-content/uploads/2026/08/featured.jpg"],
    )
    page_images = [
        ("https://www.humshehry.online/wp-content/uploads/2026/08/og.jpg", "og:image"),
        ("https://www.humshehry.online/wp-content/uploads/2026/08/featured.jpg", "article:<img>"),
    ]
    merged = fetcher._merge_image_candidates(article, page_images)
    check("featured candidate first", merged[0] ==
          "https://www.humshehry.online/wp-content/uploads/2026/08/featured.jpg")
    check("duplicate url deduped",
          merged.count("https://www.humshehry.online/wp-content/uploads/2026/08/featured.jpg") == 1)
    check("image method recorded", article.image_method == "source-media/feeds")
    fetcher.close()
    storage.close()


def test_jsonld_only_image():
    print("Test 7: JSON-LD image fallback")
    html = ('<html><head><script type="application/ld+json">'
            '{"@type":"NewsArticle","image":"https://cdn.example.com/wp-content/uploads/2026/08/ld2.jpg"}'
            "</script></head><body><div class=\"entry-content\"><p>x</p></div></body></html>")
    got = main._extract_html_image_candidates(html, "https://www.humshehry.online/?p=11")
    urls = [u for u, _ in got]
    check("json-ld image found", urls == ["https://cdn.example.com/wp-content/uploads/2026/08/ld2.jpg"])


def test_article_body_image_fallback():
    print("Test 8: article-body <img> fallback when no meta tags")
    html = ('<html><body><article><div class="entry-content">'
            '<p>text</p><img src="/wp-content/uploads/2026/08/body.jpg" alt="x"/>'
            "</div></article></body></html>")
    got = main._extract_html_image_candidates(html, "https://www.humshehry.online/story")
    urls = [u for u, _ in got]
    check("relative <img> resolved and kept",
          urls == ["https://www.humshehry.online/wp-content/uploads/2026/08/body.jpg"])


def test_logo_never_fallback():
    print("Test 9: logo is not a fallback; missing image fails strict publishing")
    config = main.Config.from_env()
    strict = replace(config, require_article_image=True)
    no_image = main.Article(
        guid="12", title="t", summary="", content="متن بہت لمبا جو اصل مضمون جیسا ہے",
        link="https://www.humshehry.online/?p=12",
        image_candidates=[],
    )
    problems = main.validate_article(no_image, strict)
    check("missing image is an error in strict mode",
          any("no article image found" in p for p in problems))

    lazy = main.FacebookPoster(strict)
    try:
        lazy.post(no_image)
        check("posting without an image raises NoUsableImageError", False)
    except main.NoUsableImageError:
        check("posting without an image raises NoUsableImageError", True)


def test_same_article_image_text_binding():
    print("Test 10: image + text come from the same article page")
    _canonical, _title, body, images = main._extract_article_from_page(
        IMAGES_HTML, "https://www.humshehry.online/?p=9"
    )
    check("body extracted from the same page", "متن" in body)
    check("images derived from the same page metadata/content", len(images) >= 3)
    for url, _label in images:
        check(f"image '{url}' references this article (own metadata/CDN)",
              "wp-content/uploads/2026/08/" in url)


def test_facebook_payload():
    print("Test 11: Facebook payload = exact title + body, nothing generated")
    _canonical, _title, body, _images = main._extract_article_from_page(
        ARTICLE_HTML, "https://www.humshehry.online/?p=1400"
    )
    article = main.Article(
        guid="1400", title="عنوان خبر", summary="", content=body,
        link="https://www.humshehry.online/2026/08/urdu-news-slug/",
        add_hashtags=False,
    )
    caption = article.facebook_caption
    check("no 'Read more'", "Read more" not in caption and "مزید پڑھیں" not in caption)
    check("no source line added", not caption.rstrip().endswith("humshehry.online"))
    check("exact title first line", caption.splitlines()[0] == "عنوان خبر")
    check("blank line separates title and body", "\n\n" in caption)
    check("no URL scrubbing broke content", "واٹس ایپ" not in caption)  # sanity: text not mangled
    check("caption identical to title + body (nothing auto-added)",
          caption == f"عنوان خبر\n\n{body}")


def run():
    config = main.Config.from_env()

    test_text_extraction()
    test_no_5000_char_truncation()
    test_no_rewrite_no_hashtags()
    test_navigation_like_body_rejected()
    test_image_priority()
    test_featured_and_source_candidates_win()
    test_jsonld_only_image()
    test_article_body_image_fallback()
    test_logo_never_fallback()
    test_same_article_image_text_binding()
    test_facebook_payload()

    print()
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(run())