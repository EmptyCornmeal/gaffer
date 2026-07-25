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
    ("Guardian", "https://www.theguardian.com/football/transferwindow/rss"),
]

# transfer-relevant keywords (word-ish boundaries to cut false positives)
_KW = re.compile(
    r"\b(sign(?:s|ed|ing)?|joins?|transfer|deal|loan|agree[ds]?|bid|medical|"
    r"unveil(?:ed)?|complete[sd]?|seal(?:s|ed)?|move(?:s)?|swoop|target|£\d)",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")

USER_AGENT = "Mozilla/5.0 (compatible; gaffer/0.1)"


def _clean(text: str | None) -> str:
    return _TAG.sub("", (text or "")).strip()


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
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0, follow_redirects=True) as c:
        for source, url in FEEDS:
            try:
                r = c.get(url)
                r.raise_for_status()
                items.extend(_parse_feed(source, r.text))
            except (httpx.HTTPError, Exception):
                continue
    seen: set[str] = set()
    deduped = []
    for it in items:
        key = it["title"].lower()
        if key in seen:
            continue
        if kws is not None:
            hay = f"{it['title']} {it['summary']}".lower()
            if not any(k in hay for k in kws):
                continue
        seen.add(key)
        deduped.append(it)
    return deduped[:limit]
