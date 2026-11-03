"""Mongolian translation, relevance score and duplicate detection, with a provider fallback chain.

Default chain (override with the AI_PROVIDERS env var, comma separated):
    gemini:gemini-3.8-flash, gemini:gemini-3.5-flash-lite, claude:claude-haiku-5-5

Every run starts again from the first provider, so a provider that ran out of quota is retried
next run. Providers without an API key are skipped. If every provider fails, items are sent in
English and the score filter is not applied.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone

import requests

from .config import Config
from .models import Item
from .util import title_tokens

log = logging.getLogger("news-bot")

DEFAULT_PROVIDERS = "gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,claude:claude-haiku-5-5"
CHUNK = 8            # items per AI request
RELATED_TITLES = 60  # past headlines sharing a distinctive word with the chunk
LATEST_TITLES = 15   # plus the newest headlines, always sent
AI_TIMEOUT = 180

SYSTEM_TEMPLATE = """You are the editor of a Mongolian-language news channel. You receive news items as
JSON. For EVERY item return one object with the same "id":

- relevant: true if the item really belongs to its "category" (see the list below). false if it
  matched only by coincidence, is spam, or is clearly off-topic.
- duplicate: true if the item reports the same event, announcement or story as a headline in
  "already_published" ("date: headline", up to a week old) or as an EARLIER item in this batch.
  It IS a duplicate when another outlet reports it, when it appears days later, when the wording
  differs, or when it is a follow-up that adds no major new fact. It is NOT a duplicate if it is a
  different announcement, even by the same company on the same day, or a real new development.
- score: how valuable the item is for readers of that category (1-5):
  5 = major news or very useful (new model/release, important result, serious vulnerability,
      excellent technical writeup)
  4 = solid news or a good technical article
  3 = ordinary but relevant news
  2 = marginal: business chatter, opinion, listicles, memes, beginner tutorials, gossip
  1 = spam, ads, SEO, unrelated. Use 1 when relevant is false.
- title_mn: a natural Mongolian headline (Cyrillic), at most 110 characters.
- summary_mn: 2-3 sentences, at most 320 characters: the key news for a Telegram post.
- body_mn: 3-5 short paragraphs (150-300 words): what happened, the important details and numbers,
  and why it matters. Separate paragraphs with a blank line. Plain text, no markdown.
- editor_note (only on some items): follow it for that item.

Rules:
- Write fluent, natural Mongolian in Cyrillic script, as a Mongolian journalist would. If the
  source text is already Mongolian, polish it instead of translating.
- Keep names of products, companies, people, teams, libraries and versions in their original Latin
  spelling (OpenAI, CVE-2026-1234, The MongolZ, BC.Game).
- Use only facts from the provided text. Do not invent numbers, dates, quotes or features.
- If the text is thin (only a headline), keep body_mn to 1-2 paragraphs stating what is known.
- Respond with JSON only: {{"items": [{{"id": ..., "relevant": ..., "duplicate": ..., "score": ...,
  "title_mn": ..., "summary_mn": ..., "body_mn": ...}}]}}.

Categories:
{taxonomy}"""


class ProviderError(Exception):
    pass


class QuotaError(ProviderError):
    """Rate limit / quota exhausted: skip this provider for the rest of the run."""


def _taxonomy(cfg: Config) -> str:
    return "\n".join(f"- {c.id}: {c.name}. {c.about}" for c in cfg.categories.values())


def _related(items: list[Item], past: list[tuple[str, str]]) -> list[str]:
    """Past headlines ("date: title") that may be the same story as one of `items`.

    A week of headlines is too long to send whole, so pick those sharing a distinctive word
    (one that is rare in the week) with an item, plus the newest few.
    """
    if not past:
        return []
    toks = [title_tokens(t) for _, t in past]
    df = Counter(w for ts in toks for w in ts)
    rare = max(3, len(past) // 25)
    want = set()
    for it in items:
        want |= title_tokens(f"{it.title} {(it.extra.get('text') or it.summary)[:400]}")
    want = {w for w in want if df.get(w, 0) <= rare}
    scored = [(len(ts & want), i) for i, ts in enumerate(toks)]
    picked = {i for n, i in sorted(scored, reverse=True)[:RELATED_TITLES] if n}
    picked |= set(range(max(0, len(past) - LATEST_TITLES), len(past)))
    return [f"{past[i][0]}: {past[i][1]}" for i in sorted(picked)]


def _payload(items: list[Item], already: list[str]) -> str:
    rows = []
    for it in items:
        row = {
            "id": it.id, "category": it.source.category,
            "source": it.publisher or it.source.name,
            "title": it.title, "text": it.extra.get("text") or it.summary,
        }
        if it.source.ai_hint:
            row["editor_note"] = it.source.ai_hint
        rows.append(row)
    return json.dumps({"already_published": already, "items": rows}, ensure_ascii=False)


# --- providers ---------------------------------------------------------------------------------

def _gemini(model: str, system: str, user: str) -> str:
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"].strip(),
                 "Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 32768},
        },
        timeout=AI_TIMEOUT,
    )
    if resp.status_code == 429:
        raise QuotaError(f"HTTP 429: {resp.text[:300]}")
    if not resp.ok:
        raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    cands = data.get("candidates") or []
    if not cands:
        raise ProviderError(f"no candidates: {str(data.get('promptFeedback'))[:200]}")
    if cands[0].get("finishReason") not in (None, "STOP"):
        raise ProviderError(f"finishReason={cands[0].get('finishReason')}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _claude(model: str, system: str, user: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(),
                 "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 16000, "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=AI_TIMEOUT,
    )
    if resp.status_code in (429, 402) or "credit balance" in resp.text.lower():
        raise QuotaError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    if not resp.ok:
        raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if data.get("stop_reason") not in ("end_turn", "stop_sequence"):
        raise ProviderError(f"stop_reason={data.get('stop_reason')}")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


PROVIDERS = {"gemini": (_gemini, "GEMINI_API_KEY"), "claude": (_claude, "ANTHROPIC_API_KEY")}


def provider_chain() -> list[tuple[str, str]]:
    chain = []
    for part in (os.environ.get("AI_PROVIDERS", "").strip() or DEFAULT_PROVIDERS).split(","):
        if ":" not in part:
            continue
        name, model = (p.strip() for p in part.split(":", 1))
        if name not in PROVIDERS:
            log.warning("Unknown AI provider %r", name)
        elif os.environ.get(PROVIDERS[name][1], "").strip():
            chain.append((name, model))
    return chain


# --- public API --------------------------------------------------------------------------------

def _parse(text: str, expected: set[str]) -> dict[str, dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    out = {}
    for row in json.loads(text).get("items", []):
        rid = str(row.get("id", ""))
        if rid not in expected:
            continue
        if not all(isinstance(row.get(k), str) and row[k].strip()
                   for k in ("title_mn", "summary_mn", "body_mn")):
            continue
        row["relevant"] = row.get("relevant") is not False
        row["duplicate"] = row.get("duplicate") is True
        try:
            row["score"] = min(5, max(1, int(row.get("score"))))
        except (TypeError, ValueError):
            row["score"] = 3  # unknown: neither keep nor drop on its own
        out[rid] = row
    return out


def translate(items: list[Item], cfg: Config, recent: list[tuple[str, str]]) -> dict[str, dict]:
    """Return {item.id: {...mn fields, relevant, duplicate, score, "ai": provider}} for successes.

    `recent` = (date, title) of articles from the last days, oldest first.
    """
    chain = provider_chain()
    if not chain:
        log.warning("No AI key set (GEMINI_API_KEY / ANTHROPIC_API_KEY); sending in English")
        return {}
    system = SYSTEM_TEMPLATE.format(taxonomy=_taxonomy(cfg))
    kept: list[str] = []  # titles kept by earlier chunks of this run
    titles = {it.id: it.title for it in items}
    today = datetime.now(timezone.utc).date().isoformat()
    exhausted: set[tuple[str, str]] = set()
    results: dict[str, dict] = {}
    for start in range(0, len(items), CHUNK):
        pending = items[start:start + CHUNK]
        for name, model in chain:
            if not pending:
                break
            if (name, model) in exhausted:
                continue
            try:
                already = _related(pending, recent) + [f"{today}: {t}" for t in kept]
                text = PROVIDERS[name][0](model, system, _payload(pending, already))
                got = _parse(text, {it.id for it in pending})
            except QuotaError as exc:
                log.warning("AI %s:%s quota/rate limit, falling back: %s", name, model, exc)
                exhausted.add((name, model))
                continue
            except (ProviderError, ValueError, requests.RequestException) as exc:
                log.warning("AI %s:%s failed, falling back: %s", name, model, exc)
                continue
            for rid, row in got.items():
                row["ai"] = f"{name}:{model}"
                results[rid] = row
                if row["relevant"] and not row["duplicate"]:
                    kept.append(titles[rid])
            log.info("AI %s:%s translated %d/%d items", name, model, len(got), len(pending))
            pending = [it for it in pending if it.id not in got]
        if pending:
            log.warning("%d items left untranslated (English fallback)", len(pending))
    return results
