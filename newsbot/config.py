"""Load topics.yaml and sources.yaml."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .util import keyword_pattern

log = logging.getLogger("news-bot")

ROOT = Path(__file__).resolve().parent.parent
TOPICS_FILE = ROOT / "topics.yaml"
SOURCES_FILE = ROOT / "sources.yaml"
DEFAULT_MAX_AGE_HOURS = 72
PRERELEASE_RE = re.compile(r"(canary|alpha|beta|\brc|nightly|preview|experimental|insiders)", re.I)


@dataclass
class Section:
    id: str
    name: str
    emoji: str
    thread: int | None = None  # fixed Telegram topic id; None = the bot creates the topic
    subtopics: list["Subtopic"] = field(default_factory=list)


@dataclass
class Subtopic:
    id: str
    name: str
    hashtag: str
    section: Section
    about: str = ""
    pattern: re.Pattern | None = None
    dedicated: bool = False  # only sources that name it get it (the AI never picks it)


@dataclass
class Source:
    name: str
    url: str = ""
    type: str = "rss"
    subtopic: str | None = None
    section: str | None = None
    keywords: re.Pattern | None = None
    title_regex: re.Pattern | None = None
    exclude: re.Pattern | None = None
    skip_prereleases: bool = False
    limit: int = 0
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS
    ai_hint: str = ""
    block: bool = True
    params: dict = field(default_factory=dict)  # type-specific (league, ...)

    def accepts_title(self, title: str) -> bool:
        if self.skip_prereleases and PRERELEASE_RE.search(title):
            return False
        if self.title_regex and not self.title_regex.search(title):
            return False
        return not (self.exclude and self.exclude.search(title))


@dataclass
class Block:
    publishers: list[str] = field(default_factory=list)
    keywords: re.Pattern | None = None

    def reason(self, title: str, publisher: str = "", url: str = "") -> str:
        where = f"{publisher} {url}".lower()
        for p in self.publishers:
            if p in where:
                return f"publisher {p!r}"
        m = self.keywords.search(title) if self.keywords else None
        return f"keyword {m.group(0)!r}" if m else ""


@dataclass
class Config:
    site_title: str
    keep_days: int
    send_hours: tuple[int, int]
    utc_offset: int
    max_per_send: int
    min_score: int
    sections: dict[str, Section]
    subtopics: dict[str, Subtopic]
    sources: list[Source]
    block: Block

    def can_send(self, now: datetime) -> bool:
        start, end = self.send_hours
        local = now.astimezone(timezone(timedelta(hours=self.utc_offset)))
        return start <= local.hour < end

    @property
    def send_hours_text(self) -> str:
        return f"{self.send_hours[0]:02d}:00-{self.send_hours[1]:02d}:00 UTC{self.utc_offset:+d}"

    def classify(self, text: str, section: str | None = None) -> Subtopic | None:
        """Best keyword match (longest matched keyword wins, so React Native beats React)."""
        best, best_len = None, 0
        for sub in self.subtopics.values():
            if (section and sub.section.id != section) or not sub.pattern:
                continue
            for m in sub.pattern.finditer(text):
                if len(m.group(0)) > best_len:
                    best, best_len = sub, len(m.group(0))
        return best


KNOWN = {"name", "url", "type", "subtopic", "section", "keywords", "title_regex", "exclude",
         "skip_prereleases", "limit", "max_age_hours", "ai_hint", "block", "enabled"}


def load_config(topics_path: Path = TOPICS_FILE, sources_path: Path = SOURCES_FILE) -> Config:
    t = yaml.safe_load(topics_path.read_text(encoding="utf-8")) or {}
    sections: dict[str, Section] = {}
    subs: dict[str, Subtopic] = {}
    for s in t.get("sections", []):
        sec = Section(s["id"], s["name"], s.get("emoji", "📰"), s.get("thread"))
        for st in s.get("subtopics", []):
            if st["id"] in subs:
                raise ValueError(f"duplicate subtopic id: {st['id']}")
            sub = Subtopic(
                id=st["id"], name=st["name"],
                hashtag=re.sub(r"\W+", "", st.get("hashtag") or st["name"]), section=sec,
                about=st.get("about", ""), pattern=keyword_pattern(st.get("keywords") or []),
                dedicated=bool(st.get("dedicated")),
            )
            sec.subtopics.append(sub)
            subs[sub.id] = sub
        sections[sec.id] = sec

    raw = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    sources, names = [], set()
    for s in raw.get("sources", []):
        if not s.get("enabled", True):
            continue
        if s["name"] in names:
            raise ValueError(f"duplicate source name: {s['name']}")
        names.add(s["name"])
        if s.get("subtopic") and s["subtopic"] not in subs:
            raise ValueError(f"[{s['name']}] unknown subtopic {s['subtopic']!r}")
        if s.get("section") and s["section"] not in sections:
            raise ValueError(f"[{s['name']}] unknown section {s['section']!r}")
        sources.append(Source(
            name=s["name"], url=s.get("url", ""), type=s.get("type", "rss"),
            subtopic=s.get("subtopic"), section=s.get("section"),
            keywords=keyword_pattern(s.get("keywords") or []),
            title_regex=re.compile(s["title_regex"], re.I) if s.get("title_regex") else None,
            exclude=re.compile(s["exclude"], re.I) if s.get("exclude") else None,
            skip_prereleases=bool(s.get("skip_prereleases")),
            limit=max(0, int(s.get("limit") or 0)),
            max_age_hours=int(s.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)),
            ai_hint=str(s.get("ai_hint") or "").strip(),
            block=bool(s.get("block", True)),
            params={k: v for k, v in s.items() if k not in KNOWN},
        ))

    b = raw.get("block") or {}
    block = Block([str(p).lower().strip() for p in b.get("publishers") or [] if str(p).strip()],
                  keyword_pattern(b.get("keywords") or []))
    tg = t.get("telegram") or {}
    site = t.get("site") or {}
    return Config(
        site_title=site.get("title", "News"), keep_days=int(site.get("keep_days", 180)),
        send_hours=tuple(tg.get("send_hours", [0, 24])), utc_offset=int(tg.get("utc_offset", 0)),
        max_per_send=max(1, int(tg.get("max_per_send", 10))),
        min_score=int((t.get("filter") or {}).get("min_score", 3)),
        sections=sections, subtopics=subs, sources=sources, block=block,
    )
