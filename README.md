# flaggy-news

A Telegram news bot with a small website. It runs free on GitHub Actions every 30 minutes:

```
sources (config.yaml)
  → new items only (state/seen.json), noise filter, duplicate-story filter
  → read the article page (image + text; Google News links are resolved)
  → AI: Mongolian title/summary/body, relevance, usefulness score 1-5, duplicate check
  → data/articles/…json  (the "database")  → static site → GitHub Pages
  → Telegram forum topics (photo + caption, 08:00-21:00 Mongolia time)
```

Sections (= Telegram topics) and what feeds them. Structure and the developer sources follow
https://github.com/Bat3orig/ai-news-bot; the rest are flaggy-news's own.

| Section | Subtopics (hashtags) | Sources |
|---|---|---|
| AI | OpenAI, Gemini, Anthropic, xAI, Qwen, Kimi, AI DevTools, AI News | official blogs, Google News, GitHub releases, TechCrunch AI, Hugging Face |
| Software | React, React Native, Next.js, NestJS, Tauri, Electron, Node.js, Vercel/Cloud, Packages | blogs, GitHub releases, JS/Node/React/Frontend weekly newsletters |
| UI/UX | shadcn/ui, Tailwind, Motion, Design systems, CSS/Web | blogs, releases, Figma, Smashing, CSS-Tricks, web.dev |
| GitHub | TypeScript/JS, Python, Rust | daily trending repos |
| Vibe coding | Vibe coding | Simon Willison, Addy Osmani, Harper Reed, Builder.io, Cursor changelog |
| Football | Premier League, Champions League | results with stats (ESPN) |
| Hacking | CVE, Security news, Bug bounty | CISA KEV, NVD critical, security news and writeups |
| Dota 2 / CS2 | Dota2, CS2 | r/DotA2; headlines on The MongolZ and BC.Game results |
| Trend / Mongolia | Trending, Mongolia | Hacker News, Lobsters / ikon.mn |

## Layout

| Path | Role |
|---|---|
| `topics.yaml` | sections, subtopics (hashtags, keywords), Telegram topic ids, send hours, score threshold |
| `sources.yaml` | all sources and the block list |
| `main.py` | `prepare` (fetch/enrich/translate/save/outbox) and `send` (Telegram) |
| `build_site.py` | static site from `data/articles` → `public/` |
| `newsbot/` | `feeds`, `structured` (ESPN/CVE), `enrich`, `ai`, `telegram`, `store`, `config` |
| `state/seen.json` | remembered items; `state/outbox.json` = messages waiting to send |
| `data/articles/` | saved articles (deleted after `site.keep_days`) |

## Setup

1. **Bot and group:** create a bot with @BotFather, make a group, turn on Topics, add the bot as admin.
   Sections with a `thread:` in `topics.yaml` use that topic id; for the others the bot creates the
   topic itself (it needs the **Manage Topics** admin right). Get an id by copying a message link in the
   topic: `t.me/c/<group>/<topic>/<message>`. The chat id is `-100<group>`.
2. **Secrets** (repo → Settings → Secrets and variables → Actions):

   | Secret | Needed |
   |---|---|
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | yes |
   | `GEMINI_API_KEY` (free: aistudio.google.com/apikey) | recommended, for Mongolian translation |
   | `CLAUDE_API_TOKEN` | optional fallback |

   Without any AI key the bot still works: items are sent in English and the score filter is off.
   Change the model order with the repo **variable** `AI_PROVIDERS`
   (default `gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,claude:claude-haiku-5-5`).
3. **Website (optional):** Settings → Pages → Source: GitHub Actions. GitHub Pages on a
   **private** repo needs a paid plan; without Pages the deploy step is skipped and Telegram is
   unaffected (the "Дэлгэрэнгүй" link is then left out).
4. **Run:** Actions → Post news → Run workflow. The first run only records what exists and sends
   a "Bot started" message. Later runs send new items only.

## Behaviour worth knowing

- **New source:** its current items are recorded silently; only later items are sent.
- **Sending hours:** Telegram 08:00-21:00 UTC+8, at most 10 messages per run. Items found at night
  wait in `state/outbox.json`. Change in `topics.yaml → telegram`.
- **Noise:** `block:` in `sources.yaml` (publishers, title keywords), AI score below
  `filter.min_score`, near-identical headlines from the last 7 days, and the AI duplicate check.
- **Duplicates:** items are remembered 30 days by id and by normalized URL.
- GitHub's scheduled runs can be late or skipped; an external timer that calls
  `workflow_dispatch` (e.g. cron-job.org) makes it steadier.

## Local use

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --dry-run                       # prints; writes and sends nothing
.venv/bin/python main.py prepare --state-dir /tmp/s --data-dir /tmp/d   # real run, isolated
.venv/bin/python build_site.py --data-dir /tmp/d --out /tmp/site
```

Add a source: append to `sources:` in `sources.yaml` (`name`, `url`, `subtopic`; optional `type`,
`keywords`, `title_regex`, `exclude`, `skip_prereleases`, `limit`, `max_age_hours`, `ai_hint`, `block`).
A source without `subtopic` is classified by the subtopic keywords in `topics.yaml`; items that match
nothing are dropped. Add a subtopic in `topics.yaml`.

Design ideas (outbox, send hours, AI scoring, site) follow https://github.com/Bat3orig/ai-news-bot.
