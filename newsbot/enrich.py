"""Resolve real article URLs and pull the image and text from the article page."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from .feeds import make_session
from .models import Item
from .util import truncate

log = logging.getLogger("news-bot")

PAGE_TIMEOUT = 10
MAX_PAGE_BYTES = 2_000_000
MAX_TEXT = 3500  # characters of article text passed to the AI


def resolve_google_news(session: requests.Session, url: str) -> str | None:
    """news.google.com/rss/articles/<id> -> the publisher's URL (via Google's batchexecute API)."""
    m = re.search(r"news\.google\.com/(?:rss/)?articles/([^?/#]+)", url)
    if not m:
        return None
    gid = m.group(1)
    page = session.get(f"https://news.google.com/rss/articles/{gid}", timeout=PAGE_TIMEOUT)
    sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not (sig and ts):
        return None
    inner = ["garturlreq",
             [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
               None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
             gid, int(ts.group(1)), sig.group(1)]
    resp = session.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data={"f.req": json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])},
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        timeout=PAGE_TIMEOUT,
    )
    try:
        real = json.loads(json.loads(resp.text.split("\n\n")[1])[0][2])[1]
    except (IndexError, ValueError, TypeError):
        return None
    return real if isinstance(real, str) and real.startswith("http") else None


class _PageParser(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.paragraphs: list[str] = []
        self._skip = 0
        self._in_p = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "p" and not self._skip:
            self._in_p, self._buf = True, []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(text) > 40:
                self.paragraphs.append(text)

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)


def fetch_page(session: requests.Session, url: str) -> dict:
    resp = session.get(url, timeout=PAGE_TIMEOUT, stream=True)
    resp.raise_for_status()
    if "html" not in resp.headers.get("Content-Type", "html"):
        return {}
    raw = resp.raw.read(MAX_PAGE_BYTES, decode_content=True)
    resp.close()
    encoding = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else "utf-8"
    parser = _PageParser()
    try:
        parser.feed(raw.decode(encoding, errors="replace"))
    except Exception:  # noqa: BLE001 - malformed HTML: keep what we have
        pass
    meta = parser.meta
    image = (meta.get("og:image") or meta.get("og:image:url") or meta.get("twitter:image")
             or meta.get("twitter:image:src") or "")
    return {
        "image": urljoin(resp.url, image) if image else "",
        "description": meta.get("og:description") or meta.get("description") or "",
        "text": "\n".join(parser.paragraphs),
    }


def enrich_item(session: requests.Session, item: Item) -> None:
    """Fill item.extra with url, image, text. Never raises."""
    url = item.link
    if item.source.type == "google_news":
        try:
            url = resolve_google_news(session, url) or url
        except Exception as exc:  # noqa: BLE001
            log.info("Google News decode failed for %s: %s", item.title[:60], exc)
    page = {}
    if "news.google.com" not in url:
        try:
            page = fetch_page(session, url)
        except Exception as exc:  # noqa: BLE001
            log.info("Article fetch failed (%s): %s", url[:80], exc)
    image = page.get("image") or item.feed_image
    if image.startswith("//"):
        image = "https:" + image
    text = page.get("text") or ""
    if item.source.type == "github_release":
        text = item.summary  # the release notes in the feed beat the GitHub page text
    elif len(text) < 200:
        text = "\n".join(t for t in (page.get("description", ""), item.summary, text) if t)
    item.extra.update({
        "url": url,
        "image": image if image.startswith("http") else "",
        "text": truncate(text.strip(), MAX_TEXT),
        "description": page.get("description", ""),
    })


def enrich(items: list[Item], workers: int = 6) -> None:
    session = make_session()
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(lambda it: enrich_item(session, it), items))
