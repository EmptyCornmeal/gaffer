"""Fetch real-world football transfer news from free RSS feeds.

No API key, no scraping fragility — standard RSS 2.0 parsed with the stdlib.
Filtered to transfer-relevant items so the front-end shows a focused feed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    # The Guardian's transferwindow feed 404s out of window; the main football
    # feed is live year-round and we filter to transfer items anyway.
    ("Guardian", "https://www.theguardian.com/football/rss"),
]

# transfer-relevant keywords (word-ish boundaries to cut false positives)
_KW = re.compile(
    r"\b(sign(?:s|ed|ing)?|joins?|transfer|deal|loan|agree[ds]?|bid|medical|"
    r"unveil(?:ed)?|complete[sd]?|seal(?:s|ed)?|move(?:s)?|swoop|target|£\d)",
    re.IGNORECASE,
)
# exclude women's football (not relevant to men's FPL)
_EXCLUDE = re.compile(
    r"\b(women|women's|wsl|women’s super league|lioness(?:es)?|ladies|girls)\b",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
# RSS feeds concatenate the headline into the body with no space ("crowdDane
# Scarlett", "victory40,112"). Split a word→Word or word→number collision, but
# only when the lowercase run is ≥2 so real names ("McTominay") are untouched.
_CAMEL = re.compile(r"([a-z]{2,})([A-Z][a-z]{2,})")
_NUMSTICK = re.compile(r"([a-z]{3,})(\d)")
_SPAM = re.compile(r"(?:sign up now[!.]?\s*){2,}", re.IGNORECASE)
_WS = re.compile(r"\s{2,}")

USER_AGENT = "Mozilla/5.0 (compatible; gaffer/0.1)"


def _clean(text: str | None) -> str:
    t = _TAG.sub("", (text or ""))
    t = _SPAM.sub("", t)
    t = _CAMEL.sub(r"\1 \2", t)
    t = _NUMSTICK.sub(r"\1 \2", t)
    return _WS.sub(" ", t).strip()


def _parse_feed(source: str, xml_text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        desc = _clean(item.findtext("description"))
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        if _EXCLUDE.search(title) or _EXCLUDE.search(desc):
            continue  # skip women's football
        if _KW.search(title) or _KW.search(desc):
            out.append(
                {"source": source, "title": title, "summary": desc[:220],
                 "link": link, "published": pub}
            )
    return out


def fetch_transfer_news(
    limit: int = 24, club_keywords: list[str] | None = None
) -> list[dict[str, Any]]:
    """Fetch transfer stories, optionally filtered to a set of club keywords
    (lowercase). When provided, only items mentioning a current PL club are kept
    — so relegated/non-PL clubs (West Ham, Scottish sides, etc.) are dropped."""
    kws = [k.lower() for k in club_keywords] if club_keywords else None
    items: list[dict[str, Any]] = []
    failed: list[str] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0, follow_redirects=True) as c:
        for source, url in FEEDS:
            try:
                r = c.get(url)
                r.raise_for_status()
                items.extend(_parse_feed(source, r.text))
            except httpx.HTTPError:
                # one feed being down (or a seasonal 404) shouldn't sink the rest;
                # a genuine bug in parsing now surfaces instead of being swallowed
                failed.append(source)
                continue
    if failed and len(failed) == len(FEEDS):
        # every feed failed — surface it rather than silently returning nothing
        import logging

        logging.getLogger(__name__).warning("all news feeds failed: %s", ", ".join(failed))
    seen: set[str] = set()
    deduped = []
    for it in items:
        # Dedup by link first (two feeds/rewordings can share one story with
        # different titles — e.g. "X signs Y" vs "Y completes switch"), then title.
        key = (it["link"].strip().lower() or it["title"].lower())
        if key in seen or it["title"].lower() in seen:
            continue
        if kws is not None:
            hay = f"{it['title']} {it['summary']}".lower()
            if not any(k in hay for k in kws):
                continue
        seen.add(key)
        seen.add(it["title"].lower())
        deduped.append(it)
    return deduped[:limit]
