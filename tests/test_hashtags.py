"""Offline tests for automatic, per-post Facebook hashtag generation.

Each article is scanned for Urdu + English keywords and matched to curated
topic rules, producing a mix of broad and specific hashtags (Facebook's
SEO/engagement sweet spot of ~3-7). Hashtags are only ever appended AFTER the
exact article text (title + body verbatim) as a separate layer; they never
modify, reorder or insert into the article. No Facebook / no live network
calls.

Usage:
    python tests/test_hashtags.py
"""

import os
import sys

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


def test_keyword_counting():
    print("Test 1: Urdu + English keyword matching with word boundaries")
    check("latin word matches", main._keyword_count("Pakistan beats India", "pakistan") == 1)
    check("latin substring rejected", main._keyword_count("Pakistanis live here", "pakistan") == 0)
    check("latin exact case-insensitive", main._keyword_count("PAKISTAN", "pakistan") == 1)
    check("urdu word matches", main._keyword_count("یہ پاکستان ہے", "پاکستان") == 1)
    check("urdu partial word rejected", main._keyword_count("پاکستانی عوام", "پاکستان") == 0)
    check("urdu followed by diacritic matches", main._keyword_count("کشمیرِ عظیم", "کشمیر") == 1)
    check("urdu multi-word keyword", main._keyword_count("ایشیا کپ کا فائنل", "ایشیا کپ") == 1)
    check("latin multi-word keyword", main._keyword_count("asia cup final", "asia cup") == 1)


def test_topic_hashtags():
    print("Test 2: keyword-driven hashtags per topic")
    cases = [
        ("ویمنز ایشیا کپ ٹرافی کی رونمائی، پاکستان اور بھارت 5 ستمبر کو مدمقابل ہوں گے",
         ["#Cricket", "#WomensCricket"]),
        ("گلوبل یونیورسٹیز رینکنگ 2026: یو اے ای ٹی پاکستان کی نجی جامعات میں چوٹھے نمبر پر",
         ["#Education"]),
        ("سانحہ پمز: سیکرٹری صحت معطل، محمد محمود کو وزارتِ صحت کا اضافی چارج مل گیا",
         ["#Health"]),
        ("نیپال اور تبت میں تباہ کن سیلاب، ہلاکتیں 359 ہوگئیں، ایک ہزار سے زائد افراد لاپتہ",
         ["#Floods"]),
        ("امریکا کا ایران سے مذاکرات سے انکار، تمام آپشنز کھلے رکھنے کا اعلان",
         ["#USA", "#Iran"]),
        ("سٹیٹ بینک نے مالی سال 2025-26 میں 1990 ارب روپے خالص منافع کمایا",
         ["#Economy"]),
        ("پشاور: سی ٹی ڈی کی کارروائی میں 4 دہشت گرد ہلاک، اسلحہ و بارودی مواد برآمد",
         ["#Security", "#CounterTerrorism"]),
        ("برطانیہ کے 3 بڑے ہوائی اڈوں پر سائبر حملہ، 87 لاکھ صارفین کا ڈیٹا متاثر",
         ["#Technology", "#CyberSecurity", "#UK"]),
        ("آزاد کشمیر کے نئے وزیراعظم کا انتخاب آج، نواز شریف نے افتخار گیلانی کو نامزد کردیا",
         ["#Kashmir", "#Politics"]),
        ("پنجاب حکومت نے اسمبلی میں نئے صوبوں کی قرارداد پیش کردی",
         ["#Punjab", "#Politics"]),
    ]
    for title, expected in cases:
        article = main.Article(guid="x", title=title, summary=title, content=title, link="https://x")
        tags = main.generate_hashtags(article).split()
        for tag in expected:
            check(f"'{tag}' found for: {title[:40]}", tag in tags)


def test_fallback_and_limits():
    print("Test 3: fallback, cap, dedupe, caption integration")
    article = main.Article(
        guid="x", title="رات کو بارش ہوئی اور درجہ حرارت گر گیا",
        summary="", content="بارش کا موسم", link="https://x",
    )
    tags = main.generate_hashtags(article).split()
    check("fallback #Humshehri always present when few matches", "#Humshehri" in tags)
    check("no duplicate hashtags", len(tags) == len(set(tags)))
    check("count respects 1-6 Facebook range", 1 <= len(tags) <= 6)

    capped = main.Article(guid="y", title="ویمنز ایشیا کپ کرکٹ میچ پاکستان",
                          summary="", content="", link="https://x", max_hashtags=4)
    capped_tags = main.generate_hashtags(capped).split()
    check("max_hashtags=4 respected", len(capped_tags) <= 4)

    # The Article-level default is still "no hashtags"; the Config-level default
    # (ADD_HASHTAGS in .env) is true. Hashtags are only ever appended by the
    # caption builder when add_hashtags is enabled.
    captioned = main.Article(
        guid="z", title="پاکستان", summary="", content="سیاست",
        link="https://x",
        source_hosts=["www.humshehry.online", "humshehry.online"],
        add_hashtags=True,
    )
    base = captioned.facebook_caption
    check("exact article caption never contains hashtags", "#" not in base)
    final = main.build_caption_with_hashtags(captioned)
    check("final caption ends with a hashtag line", final.strip().split()[-1].startswith("#"))
    check("final caption starts with the exact article", final.startswith(base + "\n\n#"))
    check("hashtags contain no website URL", "humshehry.online" not in final and "http" not in final)

    off = main.Article(guid="o", title="رات خوبصورت تھی", summary="", content="",
                       link="https://x", add_hashtags=False)
    check("add_hashtags=False adds no hashtags",
          main.build_caption_with_hashtags(off) == off.facebook_caption == "رات خوبصورت تھی")

    default_off = main.Article(guid="d", title="رات خوبصورت تھی", summary="", content="",
                               link="https://x")
    check("Article-level default is no hashtags",
          main.build_caption_with_hashtags(default_off).strip() == "رات خوبصورت تھی")


def test_config_flags():
    print("Config: ADD_HASHTAGS default is true; MAX_HASHTAGS default is 7")
    saved = dict(os.environ)
    try:
        os.environ.pop("ADD_HASHTAGS", None)
        os.environ.pop("MAX_HASHTAGS", None)
        cfg = main.Config.from_env()
        check("ADD_HASHTAGS defaults to true (> hashes generated)",
              cfg.add_hashtags is True)
        check("MAX_HASHTAGS defaults to 7", cfg.max_hashtags == 7)

        os.environ["ADD_HASHTAGS"] = "false"
        cfg_off = main.Config.from_env()
        check("ADD_HASHTAGS=false disables generation", cfg_off.add_hashtags is False)

        os.environ["ADD_HASHTAGS"] = "true"
        os.environ["MAX_HASHTAGS"] = "5"
        cfg_on = main.Config.from_env()
        check("ADD_HASHTAGS=true enables generation", cfg_on.add_hashtags is True)
        check("MAX_HASHTAGS=5 respected", cfg_on.max_hashtags == 5)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_order_and_article_integrity():
    print("Tests 3/4/10: hashtags AFTER the article; article never modified; "
          "no mid-article insertion")
    title = "پنجاب حکومت تعلیمی اداروں کی مانیٹرنگ کا فیصلہ"
    body = "لاہور: پنجاب حکومت نے تعلیم کے شعبے کا اہم فیصلہ کیا ہے۔\n\nاس میں مزید تفصیل شامل ہے۔"
    art = main.Article(guid="i1", title=title, summary="",
                       content=body, link="https://x",
                       add_hashtags=True, max_hashtags=7)
    base = art.facebook_caption
    check("base caption is exactly title + body",
          base == f"{title}\n\n{body}")
    final = main.build_caption_with_hashtags(art)
    check("final caption = exact article + blank line + hashtag block",
          final == f"{base}\n\n" + " ".join(main._select_hashtags(art)))
    check("first '#' appears only after the exact article text",
          final.rindex("#") >= len(base))
    check("hashtag block is the final line", "#" in final.split("\n\n")[-1])
    check("original article object untouched", art.facebook_caption == base)


def test_sanitize_dedupe():
    print("Tests 5/6: duplicate hashtags removed; malformed output sanitized")
    out = main._sanitize_hashtags(["#Pakistan", "#pakistan", "#Pakistan"])
    check("case-insensitive dedupe keeps the first casing",
          out == ["#Pakistan"])

    messy = main._sanitize_hashtags([
        "Here are some hashtags: #Pakistan, #News #Punjab",
        "* #Lahore",
        "1. #Education",
        "#URL http://example.com/x",
        "no hashtag here",
        "",
        "#too-long-" + ("a" * 500),
        "#ایک دو",
    ])
    check("prose text dropped, tags kept", "#Pakistan" in messy and "#News" in messy
          and "#Punjab" in messy and "#Lahore" in messy and "#Education" in messy)
    check("URL-bearing candidate dropped", not any("http" in t.lower() for t in messy))
    check("excessively long tag dropped", len(messy) == len([t for t in messy if len(t) <= 120]))
    check("space-containing tag rejected", "#ایک دو" not in messy)
    check("everything returned is a clean #tag",
          all(t.startswith("#") and " " not in t and "\n" not in t for t in messy))


def test_count_range():
    print("Test 7: only 3-7 hashtags are normally produced")
    rich = main.Article(guid="r1", title="پنجاب حکومت تعلیم ٹیکس",
                        summary="", content="لاہور پنجاب", link="https://x",
                        add_hashtags=True, max_hashtags=7)
    rich_tags = main.generate_hashtags(rich).split()
    check("rich article produces 3-7 tags", 3 <= len(rich_tags) <= 7)

    generic = main.Article(guid="g1", title="ایک عام مضمون", summary="",
                           content="یہ عام متن ہے جس میں کوئی خاص موضوع نہیں",
                           link="https://x", add_hashtags=True, max_hashtags=7)
    gen_tags = main.generate_hashtags(generic).split()
    check("low-match article still gets >= 3 fallback tags", len(gen_tags) >= 3)
    check("never more than MAX_HASHTAGS", len(gen_tags) <= 7)


def test_generation_failure_is_safe():
    print("Test 8: hashtag generation failure never modifies the article")
    art = main.Article(guid="f1", title="سیاست", summary="", content="حکومت",
                       link="https://x", add_hashtags=True)
    base = art.facebook_caption
    orig = main._select_hashtags

    def boom(*args, **kwargs):
        raise RuntimeError("simulated generation failure")

    try:
        main._select_hashtags = boom
        caption = main.build_caption_with_hashtags(art)
    finally:
        main._select_hashtags = orig

    check("exact article is posted, unchanged, without hashtags", caption == base)
    check("original caption still untouched", art.facebook_caption == base and "#" not in base)


def test_limit_reduction():
    print("Test 9: captions near the limit reduce hashtags, never the article")
    art = main.Article(guid="l1", title="پنجاب حکومت تعلیم",
                       summary="", content="لاہور پنجاب تعلیم",
                       link="https://x", add_hashtags=True, max_hashtags=7)
    base = art.facebook_caption
    full = main.generate_hashtags(art).split()
    check("precondition: several hashtags generated", len(full) >= 2)
    joined = " ".join(full)

    # Exactly one character tighter than the full hashtag block would fit.
    limit = len(base) + 2 + len(joined) - 1
    caption = main.build_caption_with_hashtags(art, limit=limit)
    check("final caption fits inside the limit", len(caption) <= limit)
    check("article not truncated (starts with exact text)", caption.startswith(base + "\n\n#"))
    tail_count = len(caption.split("\n\n")[-1].split())
    check("hashtags reduced by at least one", tail_count <= len(full) - 1)
    check("at least one hashtag survives", tail_count >= 1)

    # Article alone over the platform limit -> safe failure, no truncation.
    try:
        main.build_caption_with_hashtags(art, limit=len(base) - 1)
        check("oversized article raises OversizedCaptionError", False)
    except main.OversizedCaptionError:
        check("oversized article fails safely", True)
    check("article text untouched after fail-safe", art.facebook_caption == base)


def run():
    test_keyword_counting()
    test_topic_hashtags()
    test_fallback_and_limits()
    test_config_flags()
    test_order_and_article_integrity()
    test_sanitize_dedupe()
    test_count_range()
    test_generation_failure_is_safe()
    test_limit_reduction()

    print()
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(run())