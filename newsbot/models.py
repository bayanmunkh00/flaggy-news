from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Source


@dataclass
class Item:
    source: Source
    title: str
    link: str
    summary: str = ""                 # plain text from the feed
    published: datetime | None = None
    keys: list[str] = field(default_factory=list)  # dedup keys; keys[0] is the item id
    publisher: str = ""               # Google News: original outlet
    feed_image: str = ""
    lines: list[str] = field(default_factory=list)  # structured items: ready-made body lines
    extra: dict = field(default_factory=dict)       # filled by enrich: url, image, text

    @property
    def id(self) -> str:
        return self.keys[0]

    @property
    def ready(self) -> bool:
        """Structured items (match results, CVEs) need neither page fetch nor AI."""
        return bool(self.lines)

    @property
    def sort_time(self) -> datetime:
        return self.published or datetime.now(timezone.utc)
