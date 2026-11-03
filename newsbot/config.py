"""Load config.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.yaml"
DEFAULT_MAX_AGE_HOURS = 72


@dataclass
class Category:
    id: str
    name: str
    emoji: str
    thread: int | None
    about: str


@dataclass
class Source:
    name: str
    category: str
    type: str = "rss"
    url: str = ""
    limit: int = 0
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS
    include: re.Pattern | None = None
    exclude: re.Pattern | None = None
    ai_hint: str = ""
    params: dict = field(default_factory=dict)  # type-specific (league, ...)

    def accepts_title(self, title: str) -> bool:
        if self.include and not self.include.search(title):
            return False
        return not (self.exclude and self.exclude.search(title))


@dataclass
class Config:
    site_title: str
    keep_days: int
    send_hours: tuple[int, int]
    utc_offset: int
    max_per_send: int
    min_score: int
    categories: dict[str, Category]
    sources: list[Source]
    block_publishers: list[str]
    block_keywords: list[str]

    def can_send(self, now: datetime) -> bool:
        start, end = self.send_hours
        local = now.astimezone(timezone(timedelta(hours=self.utc_offset)))
        return start <= local.hour < end

    @property
    def send_hours_text(self) -> str:
        return f"{self.send_hours[0]:02d}:00-{self.send_hours[1]:02d}:00 UTC{self.utc_offset:+d}"


KNOWN = {"name", "category", "type", "url", "limit", "max_age_hours", "include", "exclude", "ai_hint"}


def load_config(path: Path = CONFIG_FILE) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cats = {
        cid: Category(cid, c["name"], c.get("emoji", "📰"), c.get("thread"), c.get("about", ""))
        for cid, c in raw["categories"].items()
    }
    sources = []
    for s in raw["sources"]:
        if s["category"] not in cats:
            raise ValueError(f"source {s['name']!r}: unknown category {s['category']!r}")
        sources.append(Source(
            name=s["name"], category=s["category"], type=s.get("type", "rss"), url=s.get("url", ""),
            limit=int(s.get("limit", 0)),
            max_age_hours=int(s.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)),
            include=re.compile(s["include"], re.I) if s.get("include") else None,
            exclude=re.compile(s["exclude"], re.I) if s.get("exclude") else None,
            ai_hint=s.get("ai_hint", ""),
            params={k: v for k, v in s.items() if k not in KNOWN},
        ))
    tg = raw.get("telegram", {})
    block = raw.get("block", {})
    return Config(
        site_title=raw.get("site", {}).get("title", "News"),
        keep_days=int(raw.get("site", {}).get("keep_days", 180)),
        send_hours=tuple(tg.get("send_hours", [0, 24])),
        utc_offset=int(tg.get("utc_offset", 0)),
        max_per_send=int(tg.get("max_per_send", 10)),
        min_score=int(raw.get("filter", {}).get("min_score", 3)),
        categories=cats,
        sources=sources,
        block_publishers=[p.lower() for p in block.get("publishers", [])],
        block_keywords=[k.lower() for k in block.get("title_keywords", [])],
    )
