"""Fetch sources and select new items."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from .config import Config, Source
from .models import Item
from .store import State
from .structured import FETCHERS
from .util import USER_AGENT, link_key, short_key, strip_html

log = logging.getLogger("news-bot")

FETCH_TIMEOUT = 20


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def entry_time(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(attr)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def entry_image(entry) -> str:
    for m in entry.get("media_content") or []:
        if m.get("url") and (m.get("medium") == "image" or "image" in (m.get("type") or "")
                             or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", m["url"], re.I)):
            return m["url"]
    for m in entry.get("media_thumbnail") or []:
        if m.get("url"):
            return m["url"]
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and (link.get("type") or "").startswith("image"):
            return link.get("href", "")
    body = entry.get("summary") or ""
    for c in entry.get("content") or []:
        body += c.get("value", "")
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)", body, re.I)
    return m.group(1) if m else ""


def build_item(entry, src: Source) -> Item | None:
    link = (entry.get("link") or "").strip()
    title = strip_html(entry.get("title") or "")
    if not link or not title:
        return None
    publisher = ""
    if src.type == "google_news":
        publisher = (entry.get("source") or {}).get("title", "")
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
    lk = link_key(link)
    id_key = short_key(entry["id"]) if entry.get("id") else lk
    return Item(
        source=src, title=title, link=link,
        summary=strip_html(entry.get("summary") or entry.get("description") or ""),
        published=entry_time(entry), keys=list(dict.fromkeys([id_key, lk])),
        publisher=publisher, feed_image=entry_image(entry),
    )


def fetch_source(session: requests.Session, src: Source) -> list[Item]:
    if src.type in FETCHERS:
        return FETCHERS[src.type](session, src)
    resp = session.get(src.url, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"parse error: {feed.get('bozo_exception')!r}")
    return [i for i in (build_item(e, src) for e in feed.entries) if i]


def collect(cfg: Config, state: State, now: datetime, workers: int = 8) -> tuple[list[Item], dict]:
    """Fetch every source; return new candidate items and stats.

    Sources fetched for the first time (or a first run) are bootstrapped: their current
    items are marked seen without being sent.
    """
    session = make_session()
    today = now.date().isoformat()

    def fetch(src):
        try:
            return src, fetch_source(session, src), None
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
            return src, None, exc

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(fetch, cfg.sources))

    candidates: list[Item] = []
    run_keys: set[str] = set()
    stats = {"ok": [], "failed": [], "bootstrapped": 0}
    for src, items, exc in results:
        if exc is not None:
            log.warning("[%s] fetch failed: %s", src.name, exc)
            stats["failed"].append(src.name)
            continue
        stats["ok"].append(src.name)
        bootstrap = state.first_run or src.name not in state.sources
        cutoff = now - timedelta(hours=src.max_age_hours)
        # Newest first, so `limit` keeps the freshest items.
        items.sort(key=lambda i: i.published or now, reverse=True)
        fresh = skipped = 0
        for item in items:
            if state.is_seen(item.keys):
                if item.published is None:
                    state.mark(item.keys, today)  # undated: keep remembered while it is in the feed
                continue
            if item.published and item.published < cutoff:
                continue  # too old to post; the seen-TTL is far longer than any max age
            if not src.accepts_title(item.title):
                state.mark(item.keys, today)
                continue
            if any(k in run_keys for k in item.keys):
                continue
            run_keys.update(item.keys)
            if bootstrap:
                state.mark(item.keys, today)
                stats["bootstrapped"] += 1
            elif src.limit and fresh >= src.limit:
                state.mark(item.keys, today)
                skipped += 1
            else:
                candidates.append(item)
                fresh += 1
        log.info("[%s] %d entries, %d new%s%s", src.name, len(items), fresh,
                 " (bootstrap)" if bootstrap else "",
                 f", {skipped} over limit {src.limit}" if skipped else "")
        if src.name not in state.sources:
            state.sources.append(src.name)

    names = {s.name for s in cfg.sources}
    state.sources = [n for n in state.sources if n in names]
    log.info("Sources OK: %d, failed: %d %s", len(stats["ok"]), len(stats["failed"]),
             stats["failed"] or "")
    return candidates, stats
