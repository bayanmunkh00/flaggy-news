"""Telegram Bot API client with forum-topic support."""

from __future__ import annotations

import html
import logging
import json
import re
import time
from pathlib import Path

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
    sec, sub = cfg.sections.get(art.get("section", "")), cfg.subtopics.get(art.get("subtopic", ""))
    header = f"{sec.emoji} <b>{_e(sec.name)}</b> · #{sub.hashtag}\n" if sec and sub else ""
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
    def __init__(self, token: str, chat_id: str, state: dict, dry_run: bool = False,
                 data_dir: Path | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.data_dir = data_dir
        self.session = requests.Session()
        # state = State.telegram: {"chat_id": ..., "forum": bool, "threads": {section_id: topic id}}
        if state.get("chat_id") != chat_id:
            state.clear()
            state["chat_id"] = chat_id
        state.setdefault("threads", {})
        self.state = state

    def call(self, method: str, payload: dict, file: tuple[str, bytes] | None = None) -> dict:
        for attempt in range(1, RETRIES + 1):
            try:
                if file:  # multipart upload: field name -> bytes, other fields as strings
                    resp = self.session.post(
                        self.base + method, timeout=TIMEOUT * 2,
                        data={k: v if isinstance(v, (str, int)) else json.dumps(v)
                              for k, v in payload.items()},
                        files={file[0]: ("card.png", file[1], "image/png")})
                else:
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

    def setup_topics(self, cfg: Config) -> None:
        """Create forum topics for sections that have no fixed `thread` in topics.yaml."""
        if self.dry_run:
            return
        chat = self.call("getChat", {"chat_id": self.chat_id})
        self.state["forum"] = bool(chat.get("is_forum"))
        if not self.state["forum"]:
            return
        for sec in cfg.sections.values():
            if sec.thread or sec.id in self.state["threads"]:
                continue
            try:
                topic = self.call("createForumTopic", {"chat_id": self.chat_id, "name": sec.name})
            except TelegramRejected as exc:
                log.warning("Could not create topic %r (the bot needs the Manage Topics right): %s",
                            sec.name, exc)
                continue
            self.state["threads"][sec.id] = topic["message_thread_id"]
            log.info("Created Telegram topic %r (thread %s)", sec.name, topic["message_thread_id"])

    def thread_for(self, section_id: str, cfg: Config) -> int | None:
        sec = cfg.sections.get(section_id)
        if sec and sec.thread:
            return sec.thread
        if not self.state.get("forum"):
            return None
        return self.state["threads"].get(section_id)

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
        thread = self.thread_for(art["section"], cfg)
        try:
            self._send_article(art, cfg, thread)
        except TelegramRejected as exc:
            if thread and "thread not found" in exc.description.lower():
                log.warning("Topic %s for %s is gone; posting to General", thread, art["section"])
                self.state["threads"].pop(art["section"], None)  # re-created next run
                self._send_article(art, cfg, None)
            else:
                raise

    def _card_bytes(self, art: dict) -> bytes | None:
        if not art.get("card") or not self.data_dir:
            return None
        try:
            return (self.data_dir / art["card"]).read_bytes()
        except OSError:
            return None

    def _send_article(self, art: dict, cfg: Config, thread: int | None) -> None:
        card = self._card_bytes(art)
        if card:
            caption = format_article(art, cfg, CAPTION_LIMIT)
            if self.dry_run:
                print("-" * 60, f"\n[thread={thread}] [score card {art['card']}]\n{caption}")
                return
            payload = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
            if thread:
                payload["message_thread_id"] = thread
            try:
                self.call("sendPhoto", payload, file=("photo", card))
                return
            except TelegramRejected as exc:
                if "thread not found" in exc.description.lower():
                    raise
                log.info("score card rejected (%s); sending as text", exc.description)
        elif art.get("image"):
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
