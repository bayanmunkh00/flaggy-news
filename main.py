#!/usr/bin/env python3
"""Telegram news bot with a static website.

Usage:
    python main.py prepare     # fetch sources, enrich, translate, save articles, fill the outbox
    python main.py send        # send the outbox to Telegram
    python main.py             # prepare + send
    python main.py --dry-run   # print what would happen; writes nothing, sends nothing

GitHub Actions runs `prepare`, builds/deploys the site, then `send`, so the
"Дэлгэрэнгүй" links already work when the messages arrive.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from newsbot import ai, enrich, feeds, store
from newsbot.config import Config, load_config
from newsbot.models import Item
from newsbot.telegram import SEND_DELAY, Telegram, TelegramFatalError, TelegramRejected
from newsbot.util import iso, link_key, similar_titles, title_tokens, truncate

log = logging.getLogger("news-bot")

MAX_PER_RUN = 20   # new items processed (enriched + translated + saved) per run
MAX_OUTBOX = 50    # stop processing while this many messages wait (e.g. Telegram is down)
RECENT_DAYS = 7    # articles this recent count for duplicate-story detection


def site_url() -> str:
    url = os.environ.get("SITE_URL", "").strip()
    if not url and os.environ.get("GITHUB_REPOSITORY"):
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
        url = f"https://{owner.lower()}.github.io/{repo}/"
    return url.rstrip("/") + "/" if url else ""


def to_article(item: Item, tr: dict | None, now: datetime) -> dict:
    return {
        "id": item.id,
        "category": item.source.category,
        "source": item.source.name,
        "source_label": item.publisher or item.source.name,
        "url": item.extra.get("url") or item.link,
        "image": item.extra.get("image", ""),
        "title": item.title,
        "summary_en": truncate(item.summary or item.extra.get("description", ""), 600),
        "lines": item.lines,
        "title_mn": (tr or {}).get("title_mn", ""),
        "summary_mn": (tr or {}).get("summary_mn", ""),
        "body_mn": (tr or {}).get("body_mn", ""),
        "published": iso(item.published) if item.published else iso(now),
        "added": iso(now),
        "ai": (tr or {}).get("ai", ""),
        "score": (tr or {}).get("score"),
    }


def blocked_reason(cfg: Config, item: Item) -> str | None:
    title = item.title.lower()
    host = (item.extra.get("url") or item.link).lower()
    who = f"{item.publisher.lower()} {host}"
    for kw in cfg.block_keywords:
        if kw in title:
            return f"title keyword {kw!r}"
    for pub in cfg.block_publishers:
        if pub in who:
            return f"publisher {pub!r}"
    return None


def drop_blocked(items: list[Item], cfg: Config, state: store.State, today: str) -> list[Item]:
    kept = []
    for it in items:
        why = None if it.ready else blocked_reason(cfg, it)
        if why:
            log.info("Blocked (%s), skipping: %s", why, it.title[:80])
            state.mark(it.keys, today)
        else:
            kept.append(it)
    return kept


def drop_duplicates(items: list[Item], recent: list[str], state: store.State, today: str) -> list[Item]:
    """Near-identical headline already published or earlier in this run: keep the first."""
    known = [title_tokens(t) for t in recent]
    kept = []
    for it in items:
        if it.ready:
            kept.append(it)
            continue
        toks = title_tokens(it.title)
        if any(similar_titles(toks, k) for k in known):
            log.info("Duplicate story, skipping: %s", it.title[:80])
            state.mark(it.keys, today)
            continue
        known.append(toks)
        kept.append(it)
    return kept


def prepare(state: store.State, cfg: Config, data_dir: Path, dry_run: bool) -> bool:
    """Returns True if new articles were added (the site needs a rebuild)."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    first_run = state.first_run

    candidates, stats = feeds.collect(cfg, state, now)

    if first_run:
        if not stats["ok"]:
            log.error("First run but every source failed; not initializing state")
            raise SystemExit(1)
        log.info("First run: marked %d items as seen, sending nothing", stats["bootstrapped"])
        state.outbox.append({"kind": "notice", "text": f"Bot started, tracking {len(cfg.sources)} sources"})
        return False

    room = max(0, min(MAX_PER_RUN, MAX_OUTBOX - len(state.outbox)))
    candidates.sort(key=lambda it: it.sort_time)
    recent = store.recent_titles(now, RECENT_DAYS, data_dir)
    candidates = drop_blocked(candidates, cfg, state, today)
    candidates = drop_duplicates(candidates, [t for _, t in recent], state, today)
    batch = candidates[:room]
    log.info("%d new items, processing %d (outbox has %d, room %d)",
             len(candidates), len(batch), len(state.outbox), room)
    if not batch:
        return False

    enrich.enrich([it for it in batch if not it.ready])
    batch = drop_blocked(batch, cfg, state, today)  # resolved Google News URL -> publisher host
    # Google News links resolve to publisher URLs: drop copies of items already seen elsewhere.
    unique, seen_urls = [], set()
    for it in batch:
        rk = link_key(it.extra.get("url") or it.link)
        if rk not in it.keys:
            if state.is_seen([rk]) or rk in seen_urls:
                log.info("Duplicate after URL resolve, skipping: %s", it.title[:70])
                state.mark(it.keys + [rk], today)
                continue
            it.keys.append(rk)
        seen_urls.add(rk)
        unique.append(it)

    translations = ai.translate([it for it in unique if not it.ready], cfg, recent)

    added = 0
    for it in unique:
        tr = translations.get(it.id)
        state.mark(it.keys, today)
        if tr and not tr["relevant"]:
            log.info("AI: not relevant, skipping: %s", it.title[:70])
            continue
        if tr and tr["duplicate"]:
            log.info("AI: duplicate story, skipping: %s", it.title[:70])
            continue
        if tr and tr["score"] < cfg.min_score:
            log.info("AI: low value (score %d), skipping: %s", tr["score"], it.title[:70])
            continue
        art = to_article(it, tr, now)
        if dry_run:
            print("=" * 60)
            print(f"[{art['category']}] {art['title_mn'] or art['title']}  ({art['ai'] or 'no AI'})")
            print(f"  url:   {art['url']}\n  image: {art['image'] or '-'}")
        else:
            store.save_article(art, data_dir)
        state.outbox.append({"kind": "article", "article": art})
        added += 1
    return added > 0


def send(state: store.State, cfg: Config, dry_run: bool, ignore_hours: bool = False) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not dry_run and (not token or not chat_id):
        if not state.outbox:
            return 0
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use --dry-run)")
        return 2
    if not state.outbox:
        log.info("Outbox empty, nothing to send")
        return 0
    if not ignore_hours and not cfg.can_send(datetime.now(timezone.utc)):
        log.info("Outside sending hours (%s), keeping %d messages for later",
                 cfg.send_hours_text, len(state.outbox))
        return 0

    tg = Telegram(token, chat_id, dry_run)
    base = site_url()
    sent, exit_code, done = 0, 0, 0
    remaining = list(state.outbox)
    try:
        while remaining and done < cfg.max_per_send:
            entry = remaining[0]
            if sent and not dry_run:
                time.sleep(SEND_DELAY)
            try:
                if entry["kind"] == "notice":
                    tg.send_text(entry["text"])
                else:
                    art = dict(entry["article"])
                    if base:
                        art["site_url"] = f"{base}a/{art['id']}.html"
                    tg.send_article(art, cfg)
                sent += 1
            except TelegramRejected as exc:
                # One bad message must not block the queue forever.
                log.error("Telegram rejected message, dropping it: %s", exc.description)
            remaining.pop(0)
            done += 1
    except TelegramFatalError as exc:
        log.error("Telegram error, stopping (%d left in outbox): %s", len(remaining), exc)
        exit_code = 1
    if not dry_run:
        state.outbox = remaining
    log.info("Sent %d messages, %d left for the next run", sent, len(remaining))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram news bot")
    parser.add_argument("phase", nargs="?", choices=["prepare", "send", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true",
                        help="print instead of sending; do not modify state/ or data/")
    parser.add_argument("--ignore-hours", action="store_true",
                        help="send even outside telegram.send_hours (config.yaml)")
    parser.add_argument("--state-dir", type=Path, default=store.STATE_DIR)
    parser.add_argument("--data-dir", type=Path, default=store.DATA_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)

    cfg = load_config()
    state = store.State(args.state_dir)
    now = datetime.now(timezone.utc)
    exit_code, changed = 0, False
    if args.phase in ("prepare", "all"):
        changed = prepare(state, cfg, args.data_dir, args.dry_run)
        pruned = state.prune(now)
        if not args.dry_run:
            pruned_articles = store.prune_articles(now, cfg.keep_days, args.data_dir)
            if pruned or pruned_articles:
                log.info("Pruned %d seen entries, %d articles", pruned, pruned_articles)
            changed = changed or pruned_articles > 0
    if args.phase in ("send", "all"):
        exit_code = send(state, cfg, args.dry_run, args.ignore_hours)

    if args.dry_run:
        log.info("Dry run: state/ and data/ not modified")
    else:
        state.save()
    if os.environ.get("GITHUB_OUTPUT") and args.phase != "send":
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
