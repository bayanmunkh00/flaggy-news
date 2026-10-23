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


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "flaggy-news/1.0", **(headers or {})})
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


ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
RESULT_LOOKBACK_DAYS = 2


def espn_json(url):
    return json.loads(fetch(url))


def stat(team, name):
    for x in team.get("statistics", []):
        if x["name"] == name:
            return x["displayValue"]
    return "-"


def format_match(league_name, event, summary):
    comp = event["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    home, away = teams["home"], teams["away"]
    hn, an = home["team"]["displayName"], away["team"]["displayName"]
    status = event["status"]["type"].get("shortDetail", "FT")
    lines = [
        "⚽ #football #" + re.sub(r"\W", "", league_name.lower()),
        f"<b>{html.escape(hn)} {home['score']}–{away['score']} {html.escape(an)}</b>",
        f"{html.escape(league_name)} · {html.escape(status)}",
    ]
    names = {c["team"]["id"]: c["team"]["displayName"] for c in comp["competitors"]}
    goals = []
    for d in comp.get("details", []):
        if not d.get("scoringPlay"):
            continue
        who = (d.get("athletesInvolved") or [{}])[0].get("displayName", "?")
        tag = " (pen)" if d.get("penaltyKick") else " (og)" if d.get("ownGoal") else ""
        goals.append(f"{html.escape(who)} {d['clock']['displayValue']}{tag} [{html.escape(names.get(d['team']['id'], '?'))}]")
    if goals:
        lines.append("\nGoals: " + ", ".join(goals))
    if summary:
        box = {t["homeAway"]: t for t in summary.get("boxscore", {}).get("teams", [])}
        if "home" in box and "away" in box:
            h, a = box["home"], box["away"]
            def pair(n, fmt=lambda v: v):
                return f"{fmt(stat(h, n))} – {fmt(stat(a, n))}"
            pct = lambda v: v if v == "-" else f"{round(float(v))}%"
            lines += [
                "\nStats (home – away)",
                f"Possession: {pair('possessionPct', pct)}",
                f"Shots (on target): {stat(h, 'totalShots')} ({stat(h, 'shotsOnTarget')}) – {stat(a, 'totalShots')} ({stat(a, 'shotsOnTarget')})",
                f"Corners: {pair('wonCorners')}",
                f"Passes: {stat(h, 'accuratePasses')}/{stat(h, 'totalPasses')} – {stat(a, 'accuratePasses')}/{stat(a, 'totalPasses')}",
                f"Fouls: {pair('foulsCommitted')}",
                f"Yellow cards: {pair('yellowCards')} · Red: {pair('redCards')}",
            ]
    return "\n".join(lines)


def football_results(leagues, seen_urls):
    """Finished matches (with stats) from ESPN for the configured leagues."""
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc)
    days = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(RESULT_LOOKBACK_DAYS + 1)]
    out = []
    for slug, league_name in leagues.items():
        events = {}
        for day in days:
            try:
                for e in espn_json(f"{ESPN}/{slug}/scoreboard?dates={day}")["events"]:
                    events[e["id"]] = e
            except Exception as ex:
                print(f"! {league_name} {day}: {ex}", file=sys.stderr)
        for e in events.values():
            k = hashlib.sha1(f"match:{slug}:{e['id']}".encode()).hexdigest()[:16]
            if k in seen_urls or not e["status"]["type"].get("completed"):
                continue
            try:
                summary = espn_json(f"{ESPN}/{slug}/summary?event={e['id']}")
            except Exception as ex:
                print(f"! stats {league_name} {e['id']}: {ex}", file=sys.stderr)
                summary = None
            out.append({"k": k, "ts": parse_date(e["date"]), "category": "football",
                        "text": format_match(league_name, e, summary)})
    return out


PANDA = "https://api.pandascore.co/csgo"


def panda(path, token, **params):
    q = urllib.parse.urlencode(params)
    return json.loads(fetch(f"{PANDA}/{path}?{q}", {"Authorization": f"Bearer {token}"}))


def match_label(m):
    league = (m.get("league") or {}).get("name", "")
    serie = (m.get("serie") or {}).get("full_name", "")
    tour = (m.get("tournament") or {}).get("name", "")
    return " · ".join(x for x in (league, serie, tour) if x)


def format_cs2_result(m):
    opp = [o["opponent"] for o in m.get("opponents", [])]
    if len(opp) < 2:
        return None
    score = {r["team_id"]: r["score"] for r in m.get("results", [])}
    a, b = opp[0], opp[1]
    lines = [
        "🔫 #cs2 #esport",
        f"<b>{html.escape(a['name'])} {score.get(a['id'], '?')}–{score.get(b['id'], '?')} {html.escape(b['name'])}</b>",
        html.escape(match_label(m)),
    ]
    meta = []
    if m.get("number_of_games"):
        meta.append(f"Bo{m['number_of_games']}")
    if m.get("winner"):
        meta.append(f"Winner: {m['winner']['name']}")
    if m.get("forfeit"):
        meta.append("forfeit")
    if meta:
        lines.append(html.escape(" · ".join(meta)))
    names = {o["id"]: o["name"] for o in opp}
    maps = []
    for g in sorted(m.get("games", []), key=lambda g: g.get("position", 0)):
        if g.get("status") != "finished":
            continue
        winner = names.get((g.get("winner") or {}).get("id"), "?")
        mp = (g.get("map") or {}).get("name")
        maps.append(f"Map {g['position']}{' (' + mp + ')' if mp else ''}: {winner}")
    if maps:
        lines += ["\nMaps"] + [html.escape(x) for x in maps]
    return "\n".join(lines)


def format_cs2_upcoming(m):
    opp = [o["opponent"]["name"] for o in m.get("opponents", [])]
    if len(opp) < 2:
        return None
    when = m["begin_at"].replace("T", " ").replace(":00Z", " UTC")
    bo = f" · Bo{m['number_of_games']}" if m.get("number_of_games") else ""
    return "\n".join([
        "🔫 #cs2 #esport",
        f"Upcoming: <b>{html.escape(opp[0])} vs {html.escape(opp[1])}</b>",
        f"{html.escape(match_label(m))}{bo}",
        f"🕐 {html.escape(when)}",
    ])


def cs2_matches(team_names, seen_urls):
    token = os.environ.get("PANDASCORE_TOKEN")
    if not token or not team_names:
        if team_names:
            print("PANDASCORE_TOKEN not set, skipping CS2", file=sys.stderr)
        return []
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    ids = {}
    for name in team_names:
        try:
            for t in panda("teams", token, **{"search[name]": name}):
                if name.lower() in t["name"].lower():
                    ids[t["id"]] = t["name"]
        except Exception as ex:
            print(f"! CS2 team {name}: {ex}", file=sys.stderr)
    if not ids:
        return []
    idlist = ",".join(map(str, ids))
    out = []
    try:
        past = panda("matches/past", token, **{"filter[opponent_id]": idlist, "sort": "-end_at", "page[size]": 10})
        upcoming = panda("matches/upcoming", token, **{"filter[opponent_id]": idlist, "sort": "begin_at", "page[size]": 10})
    except Exception as ex:
        print(f"! CS2 matches: {ex}", file=sys.stderr)
        return []
    cutoff = now - timedelta(days=RESULT_LOOKBACK_DAYS + 1)
    for kind, matches in (("result", past), ("upcoming", upcoming)):
        for m in matches:
            k = hashlib.sha1(f"cs2:{kind}:{m['id']}".encode()).hexdigest()[:16]
            if k in seen_urls:
                continue
            if kind == "result":
                if m.get("status") != "finished" or parse_date(m.get("end_at") or m["begin_at"]) < cutoff.timestamp():
                    continue
                text, ts = format_cs2_result(m), parse_date(m.get("end_at") or m["begin_at"])
            else:
                if not m.get("begin_at") or parse_date(m["begin_at"]) > (now + timedelta(hours=24)).timestamp():
                    continue
                text, ts = format_cs2_upcoming(m), now.timestamp()
            if text:
                out.append({"k": k, "ts": ts, "category": "cs2", "text": text})
    return out


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
    seen_titles = [s["t"] for s in state if s["t"]]

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

    for m in football_results(cfg.get("football_leagues", {}), seen_urls) + cs2_matches(cfg.get("cs2_teams", []), seen_urls):
        seen_urls.add(m["k"])
        state.append({"k": m["k"], "t": "", "ts": m["ts"]})
        fresh.append(m)

    fresh.sort(key=lambda i: i["ts"])
    if first_run:
        print(f"First run: seeding {len(fresh)} items without posting.")
        fresh = []
    elif len(fresh) > MAX_POSTS_PER_RUN:
        print(f"{len(fresh)} new, posting newest {MAX_POSTS_PER_RUN}")
        fresh = fresh[-MAX_POSTS_PER_RUN:]

    for it in fresh:
        cat = it["category"]
        text = it.get("text") or (
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
