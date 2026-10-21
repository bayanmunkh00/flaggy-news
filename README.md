# flaggy-news

Pulls RSS/Atom feeds, removes duplicates, and posts new items to a Telegram channel. Runs every 30 min on GitHub Actions.

**Dedup:** canonical URL (tracking params, `www.`, fragments stripped) plus fuzzy title match (≥85%) across all sources. State lives in `state/seen.json`, committed back by the workflow. The first run only seeds state and posts nothing.

## Setup
1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. Create a channel, add the bot as an **admin** with permission to post.
3. Channel ID: `@your_channel_username` (public) or a numeric `-100…` id (private).
4. Push this folder to a GitHub repo. In Settings → Secrets and variables → Actions add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
5. Actions tab → "Post news" → Run workflow (first run seeds), then it runs on schedule.

## Local test
```
DRY_RUN=1 python3 news_bot.py      # prints, doesn't post or save state
TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=@chan python3 news_bot.py
```

Edit `feeds.json` to change sources. `MAX_POSTS_PER_RUN` (default 15) caps posts per run.
Note: GitHub disables scheduled workflows after 60 days without repo activity; the state commits keep it active.
