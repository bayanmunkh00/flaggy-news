"""Small shared helpers."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|source$)", re.I)


def short_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_url(url: str) -> str:
    """Lowercase host, drop www, tracking params, fragment and trailing slash."""
    p = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    host = p.netloc.lower().removeprefix("www.")
    return urlunsplit((p.scheme.lower(), host, p.path.rstrip("/"), urlencode(query), ""))


def link_key(url: str) -> str:
    return short_key(normalize_url(url))


def strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return re.sub(r"\s*The post .{0,300}? appeared first on .{0,100}$", "", text)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


# Title similarity: the same story from several outlets. Deliberately strict (>= 4 shared
# words and >= 80% overlap) so different stories about one company are not merged; reworded
# duplicates are left to the AI `duplicate` flag.
_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "new", "how", "why", "what", "after",
    "over", "into", "now", "has", "have", "will", "can", "says", "said", "its", "are",
}


def title_tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[\w+.-]+", title.lower())
    return frozenset(w.strip(".-") for w in words if len(w) > 2 and w not in _STOP)


def similar_titles(a: frozenset[str], b: frozenset[str]) -> bool:
    inter = len(a & b)
    return inter >= 4 and inter / min(len(a), len(b)) >= 0.8


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
