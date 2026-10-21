#!/usr/bin/env python3
"""Fetch RSS/Atom feeds, drop duplicates, post new items to a Telegram channel."""
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.json"
STATE_FILE = ROOT / "state" / "seen.json"

MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "15"))
MAX_PER_FEED = 20
MAX_AGE_DAYS = 14
MAX_STATE_ITEMS = 3000
TITLE_SIMILARITY = 0.85
DRY_RUN = os.environ.get("DRY_RUN") == "1"

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|source$)", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "flaggy-news/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(data):
    """Return list of dicts (title, link, ts) from RSS 2.0 or Atom."""
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = link = date = ""
        for c in el:
            t = local(c.tag)
            if t == "title":
                title = (c.text or "").strip()
            elif t == "link":
                link = (c.get("href") or c.text or "").strip() if not link or c.get("rel") == "alternate" else link
            elif t in ("pubDate", "published", "updated", "date") and not date:
                date = (c.text or "").strip()
        if title and link:
            items.append({"title": html.unescape(title), "link": link, "ts": parse_date(date)})
    return items


def parse_date(s):
    if not s:
        return time.time()
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def canon_url(url):
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not TRACKING_PARAMS.match(k)]
    path = p.path.rstrip("/")
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower().removeprefix("www."), path, urllib.parse.urlencode(q), ""))


def norm_title(t):
    return re.sub(r"[^\w\s]", "", t.lower()).strip()


def url_key(url):
    return hashlib.sha1(canon_url(url).encode()).hexdigest()[:16]


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()), False
    return [], True


def save_state(state):
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    state = [s for s in state if s["ts"] >= cutoff][-MAX_STATE_ITEMS:]
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=0))


def send_telegram(token, target, text, thread_id=None):
    fields = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }
    if thread_id:
        fields["message_thread_id"] = thread_id
    data = urllib.parse.urlencode(fields).encode()
    for attempt in range(3):
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=20
            ).read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(json.loads(e.read()).get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            raise RuntimeError(f"Telegram error {e.code}: {e.read().decode()}")
    raise RuntimeError("Telegram: too many rate-limit retries")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not DRY_RUN and not (token and chat_id):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or DRY_RUN=1)")

    cfg = json.loads(FEEDS_FILE.read_text())
    feeds, categories = cfg["feeds"], cfg.get("categories", {})
    # topic (message_thread_id) per category in a forum-enabled group
    topics = {**cfg.get("topics", {}), **json.loads(os.environ.get("TELEGRAM_TOPICS") or "{}")}
    state, first_run = load_state()
    seen_urls = {s["k"] for s in state}
    seen_titles = [s["t"] for s in state]

    fresh = []
    for f in feeds:
        try:
            items = parse_feed(fetch(f["url"]))
        except Exception as e:
            print(f"! {f['name']}: {e}", file=sys.stderr)
            continue
        items = sorted(items, key=lambda i: i["ts"], reverse=True)[:MAX_PER_FEED]
        for it in items:
            k = url_key(it["link"])
            nt = norm_title(it["title"])
            if k in seen_urls:
                continue
            if any(difflib.SequenceMatcher(None, nt, st).ratio() >= TITLE_SIMILARITY for st in seen_titles):
                seen_urls.add(k)
                state.append({"k": k, "t": nt, "ts": it["ts"]})
                continue
            seen_urls.add(k)
            seen_titles.append(nt)
            state.append({"k": k, "t": nt, "ts": it["ts"]})
            fresh.append({**it, "source": f["name"], "category": f["category"]})

    fresh.sort(key=lambda i: i["ts"])
    if first_run:
        print(f"First run: seeding {len(fresh)} items without posting.")
        fresh = []
    elif len(fresh) > MAX_POSTS_PER_RUN:
        print(f"{len(fresh)} new, posting newest {MAX_POSTS_PER_RUN}")
        fresh = fresh[-MAX_POSTS_PER_RUN:]

    for it in fresh:
        cat = it["category"]
        text = (
            f"{categories.get(cat, '📰')} #{cat}\n"
            f"<b>{html.escape(it['title'])}</b>\n"
            f"{html.escape(it['source'])} · {html.escape(it['link'])}"
        )
        # optional per-category destination, e.g. TELEGRAM_CHAT_ID_DOTA
        target = os.environ.get(f"TELEGRAM_CHAT_ID_{cat.upper()}") or chat_id
        if DRY_RUN:
            print(text, "\n")
        else:
            send_telegram(token, target, text, topics.get(cat))
            time.sleep(1.1)
    print(f"Posted {len(fresh)} items.")

    if not DRY_RUN:
        save_state(state)


if __name__ == "__main__":
    main()
