"""Telegram Bot API client with forum-topic support."""

from __future__ import annotations

import html
import logging
import re
import time

import requests

from .config import Config
from .util import truncate

log = logging.getLogger("news-bot")

SEND_DELAY = 3       # seconds between messages (group rate limit)
RETRIES = 3
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
TIMEOUT = 20


class TelegramFatalError(Exception):
    """Unrecoverable (bad token, wrong chat id, bot removed) or Telegram is down."""


class TelegramRejected(Exception):
    """This one message was rejected (e.g. bad image URL)."""

    def __init__(self, description: str):
        super().__init__(description)
        self.description = description


def _e(text: str) -> str:
    return html.escape(text or "", quote=False)


def _visible(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def format_article(art: dict, cfg: Config, limit: int) -> str:
    """HTML message; `limit` is the visible-character budget (caption 1024 / text 4096)."""
    cat = cfg.categories.get(art["category"])
    header = f"{cat.emoji} <b>{_e(cat.name)}</b> · #{art['category']}\n" if cat else ""
    title = art.get("title_mn") or art["title"]
    body = "\n".join(art["lines"]) if art.get("lines") else \
        (art.get("summary_mn") or art.get("summary_en") or "")
    links = []
    if art.get("site_url"):
        links.append(f'<a href="{html.escape(art["site_url"], quote=True)}">Дэлгэрэнгүй</a>')
    links.append(f'<a href="{html.escape(art["url"], quote=True)}">Эх сурвалж</a>')
    footer = " · ".join(links) + f" — {_e(art.get('source_label') or art['source'])}"

    def build(text: str) -> str:
        msg = f"{header}<b>{_e(title)}</b>"
        if text:
            msg += f"\n\n{_e(text)}"
        return f"{msg}\n\n{footer}"

    msg = build(body)
    if _visible(msg) > limit:
        budget = len(body) - (_visible(msg) - limit) - 2
        msg = build(truncate(body, budget) if budget > 20 else "")
    return msg


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.session = requests.Session()

    def call(self, method: str, payload: dict) -> dict:
        for attempt in range(1, RETRIES + 1):
            try:
                resp = self.session.post(self.base + method, json=payload, timeout=TIMEOUT)
            except requests.RequestException as exc:
                log.warning("Telegram %s failed (attempt %d): %s", method, attempt, exc)
                time.sleep(SEND_DELAY * attempt)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {"description": resp.text[:200]}
            if resp.ok and body.get("ok"):
                return body.get("result")
            desc = body.get("description", "")
            if resp.status_code == 429:
                wait = int((body.get("parameters") or {}).get("retry_after", 5)) + 1
                log.warning("Telegram 429, waiting %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403, 404) or "chat not found" in desc.lower():
                raise TelegramFatalError(f"HTTP {resp.status_code}: {desc}")
            if 400 <= resp.status_code < 500:
                raise TelegramRejected(desc)
            log.warning("Telegram HTTP %s (attempt %d): %s", resp.status_code, attempt, desc)
            time.sleep(SEND_DELAY * attempt)
        raise TelegramFatalError(f"Telegram {method} unavailable after retries")

    def send_text(self, text: str, thread: int | None = None, preview_url: str | None = None) -> None:
        if self.dry_run:
            print("-" * 60, f"\n[thread={thread}] {text}")
            return
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if preview_url:
            payload["link_preview_options"] = {"url": preview_url, "prefer_large_media": True}
        if thread:
            payload["message_thread_id"] = thread
        self.call("sendMessage", payload)

    def send_article(self, art: dict, cfg: Config) -> None:
        cat = cfg.categories.get(art["category"])
        thread = cat.thread if cat else None
        try:
            self._send_article(art, cfg, thread)
        except TelegramRejected as exc:
            if thread and "thread not found" in exc.description.lower():
                log.warning("Topic %s for %s is gone; posting to General", thread, art["category"])
                self._send_article(art, cfg, None)
            else:
                raise

    def _send_article(self, art: dict, cfg: Config, thread: int | None) -> None:
        if art.get("image"):
            caption = format_article(art, cfg, CAPTION_LIMIT)
            if self.dry_run:
                print("-" * 60, f"\n[thread={thread}] [photo {art['image'][:80]}]\n{caption}")
                return
            payload = {"chat_id": self.chat_id, "photo": art["image"], "caption": caption,
                       "parse_mode": "HTML"}
            if thread:
                payload["message_thread_id"] = thread
            try:
                self.call("sendPhoto", payload)
                return
            except TelegramRejected as exc:
                if "thread not found" in exc.description.lower():
                    raise
                log.info("sendPhoto rejected (%s); sending as text", exc.description)
        self.send_text(format_article(art, cfg, MESSAGE_LIMIT), thread,
                       preview_url=None if art.get("lines") else art["url"])
